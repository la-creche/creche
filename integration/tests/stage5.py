"""One autonomous family, run by a trigger and gated by a phone (packet I5).

Stage 1 put the door, `attendance` and the playpen in one process. Stage 2
added the reconciler and stage 3 the PEP. Stage 5 adds the two things nothing
has ever run together: the trigger door that starts an autonomous job, and the
PEP approval that holds a tool call open until a phone answers.

    agent-trigger fire / POST /triggers/<family>/<name>   the REAL door
      │ contract 02 §5.1 then §5.4, wait=accepted
      ▼
    the REAL attendance ──► auto-<ulid> ──► one turn, queued past the limit
      │ fake_sbx.py exec --env-file ──► node dist/playpen.js
      ▼                                            │
    the job's model calls a GATED tool             ▼
      │ POST /call, the REAL bridge       playpen/test/fake-pi.mjs
      ▼
    the REAL PEP family app ──► gate opened ──► the fake approval transport
      │      │                                            │
      │      `── audit/<day>.jsonl (decision: pending) ──┐ │ tap
      │                                                  │ ▼
      │   attendance's AuditTail ──► waiting-approval  ◄────┘ POST /approval/<gate>
      ▼
    execute, or deny ──► the turn settles ──► outcomes/<family>/<ulid>.json

Four fakes, none of them under test:

1. `FakeDriver`, because a Mac has no `sbx`.
2. `FakeLiteLLMKeys`, because no test may mint a key.
3. `fake-pi.mjs`, the playpen package's own double.
4. `ApprovalTransport`, this module's stand-in for Node-RED and Home
   Assistant. It receives what the PEP's own `HttpApprovalNotifier` sends and
   answers the PEP's return leg the way contract 04 §8.4 rules 3 and 4 say the
   real flow does: it builds the phone's action string, splits it on the
   underscore, and posts the decision back under Node-RED's own bearer.

Two stand-ins that are NOT fakes of anything under test:

1. The clock. `door-trigger` has no timer loop of its own: `caregiver` writes
   systemd timers that run `agent-trigger fire`, so systemd is the clock on
   the host and this harness is the clock here. The firing itself is the real
   door's own `fire_trigger`.
2. The model's tool call. `buildTurnEnv` hardcodes `PEP_URL` at
   the host's LAN address (right on the host, unreachable here), so a pi
   child launched by the real playpen cannot reach a PEP on loopback.
   `call_tool` runs the REAL bridge bundle in its own process instead, under
   the running job's own session and turn ids, which is what makes the PEP's
   audit record name a turn the host itself started. Stage 3 set this
   precedent for the same reason.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import httpx
import pytest
import yaml
from agent_door_trigger.attendance import HttpAttendance
from agent_door_trigger.cli import execute_fire
from agent_door_trigger.config import AttendanceTarget, ServeConfig
from agent_door_trigger.errors import ExitCode
from agent_door_trigger.families import StatusFiles
from agent_door_trigger.fire import FireOutcome, Firing, TriggerKind, fire_trigger
from agent_door_trigger.routes import RouteTable
from agent_door_trigger.webhooks import create_app as create_webhook_app
from agent_pep.gatekeeper import Gatekeeper, HttpApprovalNotifier
from attendance.auth import Principal
from attendance.config import Bind, Config
from attendance.ids import new_ulid
from attendance.service import SessionService
from caregiver.apply import ApplyResult, apply_once
from caregiver.driver import FakeDriver
from caregiver.litellm_keys import FakeLiteLLMKeys
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from stack import FAMILY, LOCK_POLL_S, LOCK_STALE_S, Stack, repo_root
from stage3 import (
    BRIDGE_BUNDLE,
    BRIDGE_DRIVER,
    DRIVER_TIMEOUT_S,
    ToolCall,
    _read_report,
)

from caregiver import paths as caregiver_paths

#: `tests_manager/pep_harness.py` builds the real PEP through its real entry
#: point. `stage3` puts that directory on the path when it is imported; this
#: repeats it so a reader of this module sees where `build_pep` comes from,
#: and so an import order change cannot quietly break it.
sys.path.insert(0, str(repo_root() / "integration" / "tests_manager"))

#: The three families of the fixture registry. `ha-review` is the one stage 5
#: is about; the other two exist for the two rules that say what an autonomous
#: family does NOT share (contract 02 §13 rule 5, §3.1).
HA_REVIEW: Final = "ha-review"
CHAT: Final = FAMILY
VAULT_ORACLE: Final = "vault-oracle"
FAMILIES: Final = (HA_REVIEW, CHAT, VAULT_ORACLE)

#: The fixture registry, checked in beside this suite.
REGISTRY_ROOT: Final = repo_root() / "integration" / "fixtures" / "stage5-registry"

#: An obvious fixture, never resolved: contract 06 owns digest selection.
IMAGE: Final = "sha256:" + "0" * 63 + "5"

#: The two trigger forms `ha-review` declares (contract 01 §3.13).
WEBHOOK_NAME: Final = "boiler-alert"
CRON_TRIGGER: Final = "@hourly"

#: The one gated action, and arguments the family file's allow list admits.
GATED_TOOL: Final = "ha_call"
GATED_ARGS: Final[dict[str, Any]] = {
    "domain": "notify",
    "service": "mobile_app_example_phone",
    "data": {"message": "the boiler is cold"},
}

#: Contract 04 §8.4's action string, which Node-RED builds and splits. Neither
#: field may hold an underscore, so composing and splitting it here is what
#: proves the format still carries a real family name and a real gate id.
ACTION_APPROVE: Final = "AGENT_APPROVE"
ACTION_DENY: Final = "AGENT_DENY"

#: Where the fake transport listens, and the two bearers of §8.4. Obvious
#: fixtures: invariant 13 forbids a real secret in a test fixture.
HOOK_PATH: Final = "/node-red/agent-approval"
FIXTURE_HOOK_TOKEN: Final = "FIXTURE-APPROVAL-HOOK-TOKEN"
FIXTURE_CALLBACK_TOKEN: Final = "FIXTURE-APPROVAL-CALLBACK-TOKEN"

#: One webhook bearer, over contract 02 §3 rule 7's 32 byte floor, which
#: `tokens.py` applies to a webhook's own credential too.
WEBHOOK_TOKEN_MODE: Final = 0o600

#: Contract 04 §8.5's limit is 15 minutes. A scenario that watches it expire
#: cannot wait that long, so the real `Gatekeeper` is built with a short one,
#: the way `stack.py` shrinks contract 03 §11's three lock numbers. Nothing
#: else about the gate changes: the same object, the same notifier, the same
#: deadline arithmetic.
#:
#: The default has to outlast `attendance`'s own one-second gate poll twice
#: over, because the scenario that MEASURES that lag must not race the
#: deadline it is measuring under. `IMPATIENT_LIMIT_S` is the short one, for
#: the scenario that watches the gate run out.
APPROVAL_LIMIT_S: Final = 30.0
IMPATIENT_LIMIT_S: Final = 1.5

#: How often a held gate re-checks its grant (contract 04 §1.5.3). The real
#: number is 5 seconds, which outlasts this harness's whole approval limit.
REVOKE_POLL_S: Final = 0.1

#: The return leg is one loopback POST that resolves an `asyncio.Event`.
CALLBACK_TIMEOUT_S: Final = 10.0

#: Contract 02 §3 rule 5's one exception, as stage 3 names it.
DELEGATE_TOKEN_MODE: Final = 0o640

#: The restart sweep opens no channel: `_recover_turns` and `_sweep_jobs` read
#: state and write records. A command that cannot run makes an unexpected dial
#: a loud failure instead of a quiet pass.
NO_CHANNEL_COMMAND: Final = '"/nonexistent/sbx" exec --env-file {env_file} {sandbox}'

#: What an attended turn asks. `fake-pi.mjs` slices a prompt at 24
#: characters before echoing it, so a fixture prompt stays under that
#: (`AGENTS.md` 19).
ATTENDED_PROMPT: Final = "is the boiler ok"

HTTP_OK: Final = 200
HTTP_CREATED: Final = 201
HTTP_ACCEPTED: Final = 202
HTTP_UNAUTHORIZED: Final = 401
HTTP_UNAVAILABLE: Final = 503


def bridge_bundle_missing() -> bool:
    """The PEP bridge is built by the same `pnpm build` as the playpen."""
    return not BRIDGE_BUNDLE.is_file()


@dataclass(frozen=True)
class GateNotice:
    """One push, as the fake transport received it (contract 04 §8.4 rule 1).

    `at` is when this process saw it, on the monotonic clock. Scenario 4
    measures from here to the moment `attendance` shows `waiting-approval`,
    which is the lag a PEP-to-`attendance` message would close.
    """

    family: str
    gate: str
    summary: str
    kind: str
    at: float


class ApprovalTransport:
    """Node-RED and Home Assistant, as far as the PEP can tell.

    It does the two legs contract 04 §8.4 names and nothing else:

    1. It receives the PEP's push on its own bearer, and records it. A
       transport set `undeliverable` answers 503, which is §8.4's last rule:
       a gate nobody can see denies at once rather than stalling 15 minutes.
    2. It answers by building the phone's action string, splitting it on the
       underscore the way the real flow's parser does, and posting the
       decision to `POST /approval/<gate>` under the callback bearer.

    It holds no policy. A test decides when the phone is tapped, which is the
    one thing this harness cannot get from a real phone.
    """

    def __init__(self) -> None:
        self.notices: list[GateNotice] = []
        self.undeliverable = False
        self.pep_url = ""
        self.app = self._build()

    # ------------------------------------------------------------- the push leg

    def _build(self) -> FastAPI:
        app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

        @app.post(HOOK_PATH)
        async def hook(request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
            if _bearer_of(request) != FIXTURE_HOOK_TOKEN:
                return JSONResponse(status_code=HTTP_UNAUTHORIZED, content={"ok": False})

            body: dict[str, Any] = await request.json()
            self.notices.append(
                GateNotice(
                    family=str(body.get("family", "")),
                    gate=str(body.get("gate", "")),
                    summary=str(body.get("summary", "")),
                    kind=str(body.get("kind", "")),
                    at=time.monotonic(),
                )
            )

            if self.undeliverable:
                return JSONResponse(status_code=HTTP_UNAVAILABLE, content={"ok": False})

            return JSONResponse({"ok": True})

        return app

    def last(self, family: str) -> GateNotice | None:
        """The most recent push for one family, or None while none has come."""
        for notice in reversed(self.notices):
            if notice.family == family:
                return notice

        return None

    def gates(self, family: str) -> list[str]:
        """Every gate id pushed for one family, in push order."""
        return [notice.gate for notice in self.notices if notice.family == family]

    # ----------------------------------------------------------- the return leg

    async def approve(self, family: str, gate: str) -> httpx.Response:
        return await self._tap(f"{ACTION_APPROVE}_{family}_{gate}")

    async def deny(self, family: str, gate: str) -> httpx.Response:
        return await self._tap(f"{ACTION_DENY}_{family}_{gate}")

    async def replay(self, action: str) -> httpx.Response:
        """A tap on an action string this transport built earlier. A replay,
        a stale gate and a gate id for another call all arrive this way."""
        return await self._tap(action)

    async def _tap(self, action: str) -> httpx.Response:
        """Contract 04 §8.4 rules 3 and 4, as the real flow performs them.

        The action string is what rides `mobile_app_notification_action`, and
        Node-RED splits it on the underscore. Splitting it here rather than
        posting remembered values is the only way this harness notices a
        family name or a gate id that the format cannot carry.
        """
        verb, family, gate = _split_action(action)
        decision = "approve" if verb == ACTION_APPROVE else "deny"

        if not family or not gate:
            raise AssertionError(f"action {action!r} does not split into a family and a gate")

        return await self.post_decision(gate, decision)

    async def post_decision(self, gate: str, decision: str) -> httpx.Response:
        """The return leg on its own, for a body no phone could have built."""
        async with httpx.AsyncClient() as client:
            return await client.post(
                f"{self.pep_url}/approval/{gate}",
                json={"decision": decision},
                headers={"Authorization": f"Bearer {FIXTURE_CALLBACK_TOKEN}"},
                timeout=CALLBACK_TIMEOUT_S,
            )


class Stage5:
    """The stage 1 stack, with the real reconciler, the real PEP with a real
    approval gate, and the real trigger door in front of both.

    `caregiver` publishes all three families from the fixture registry, so
    every status document, grant file and `supervisor.env` on the path is the
    one the real reconciler writes (`AGENTS.md` 12 and 21).
    """

    def __init__(self, stack: Stack) -> None:
        self.stack = stack
        self.driver = FakeDriver()
        self.litellm = FakeLiteLLMKeys()
        self.transport = ApprovalTransport()
        self.pep_url = ""
        self.webhook_url = ""

    # ------------------------------------------------------------------ the fleet

    def apply_all(self) -> tuple[ApplyResult, ...]:
        """One `apply_once` per family, in the order a cold host would."""
        return tuple(self.apply_one(name) for name in FAMILIES)

    def apply_one(self, family: str) -> ApplyResult:
        """The real manager, against the fixture registry on disk.

        The two fakes are held on this object rather than made per call: both
        stand in for something that outlives one apply, and a second
        `FakeLiteLLMKeys` would have forgotten the key the first one minted.
        """
        return apply_once(
            REGISTRY_ROOT,
            family,
            state_root=self.stack.state_root,
            image=IMAGE,
            driver=self.driver,
            litellm=self.litellm,
        )

    def rewrite_family(self, family: str, registry_root: Path, **overrides: object) -> Path:
        """A copy of the fixture registry with one field of one family changed.

        The fixture files are read-only inputs checked into the repo, so a
        scenario that removes a granted verb writes its own copy rather than
        editing them under the next test (`AGENTS.md` 21).
        """
        for name in FAMILIES:
            source = REGISTRY_ROOT / "families" / name
            target = registry_root / "families" / name
            target.mkdir(parents=True, exist_ok=True)
            body: dict[str, Any] = yaml.safe_load(
                (source / "family.yaml").read_text(encoding="utf-8")
            )

            if name == family:
                body.update(overrides)

            (target / "family.yaml").write_text(yaml.safe_dump(body), encoding="utf-8")
            (target / "instructions.md").write_text(
                (source / "instructions.md").read_text(encoding="utf-8"), encoding="utf-8"
            )

        return registry_root

    def apply_from(self, registry_root: Path, family: str) -> ApplyResult:
        """`apply_once` against a rewritten registry, same fakes, same state."""
        return apply_once(
            registry_root,
            family,
            state_root=self.stack.state_root,
            image=IMAGE,
            driver=self.driver,
            litellm=self.litellm,
        )

    # --------------------------------------------------------- what to assert on

    def pep_token(self, family: str) -> str:
        """The family token `caregiver` minted, as the sandbox holds it."""
        body = json.loads(
            caregiver_paths.creds_path(self.stack.state_root, family).read_text(encoding="utf-8")
        )
        return str(body["pep_token"])

    def audit_lines(self) -> list[dict[str, Any]]:
        """Every audit v2 line the PEP wrote, in file order (contract 04 §6)."""
        lines: list[dict[str, Any]] = []
        for path in sorted((self.stack.state_root / "audit").glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                lines.append(json.loads(line))

        return lines

    def gate_lines(self, gate: str) -> list[dict[str, Any]]:
        """The audit records for one gate, in order (contract 04 §6.4's two)."""
        return [line for line in self.audit_lines() if line.get("gate") == gate]

    def outcomes(self, family: str) -> list[dict[str, Any]]:
        """Every outcome record one family has left (contract 02 §13.1)."""
        directory = self.stack.state_root / "outcomes" / family
        if not directory.is_dir():
            return []

        return [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(directory.glob("*.json"))
        ]

    def outcome_of(self, family: str, session: str) -> dict[str, Any] | None:
        """The record one job left, found by the session it names."""
        for record in self.outcomes(family):
            if record.get("session") == session:
                return record

        return None

    def sessions_of(self, family: str) -> list[str]:
        """Every session directory that family still has on disk."""
        directory = self.stack.sessions_root / family
        if not directory.is_dir():
            return []

        return sorted(one.name for one in directory.iterdir() if one.is_dir())

    def scratch_of(self, family: str, session: str) -> Path:
        """One session's own directory, which invariant 15 deletes with it."""
        return self.stack.sessions_root / family / session

    def journal_lines(self, family: str, session: str) -> list[dict[str, Any]]:
        """One session's journal, as contract 02 §8 wrote it.

        `Stack.journal_lines` reads `chat`'s directory only, and stage 5's
        sessions live under `ha-review`.
        """
        path = self.scratch_of(family, session) / "journal.ndjson"
        if not path.is_file():
            return []

        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def live_turn(self, family: str, session: str) -> str | None:
        """The turn the REAL playpen is running, from its own turn file.

        Contract 03 §7.4: the playpen rewrites this at every `start_turn`,
        so it names the current turn of a held-open process. A tool call from
        inside the job has to carry that id, or the PEP's audit record names
        a turn the host never started and `attendance` drops it (contract 04
        §8.6, `attendance/approvals.py` rule 1).
        """
        path = (
            caregiver_paths.control_dir(self.stack.state_root, family, f"{family}-s1")
            / "sessions"
            / session
            / "turn.json"
        )
        if not path.is_file():
            return None

        body: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))

        return str(body.get("turn", "")) or None

    async def session_state(self, family: str, session: str) -> str:
        """Contract 02 §4.2's session state, over the real API.

        `view-ro` is the principal the noticeboard uses, and it is the reader
        that contract 04 §8.6 is written for.
        """
        client = self.stack.attendance_as(Principal.VIEW_RO)
        reply = await client.get(f"/v1/sessions/{family}/{session}")

        if reply.status_code != HTTP_OK:
            return ""

        body: dict[str, Any] = reply.json()

        return str(body.get("state", ""))

    async def session_exists(self, family: str, session: str) -> bool:
        client = self.stack.attendance_as(Principal.VIEW_RO)
        reply = await client.get(f"/v1/sessions/{family}/{session}")

        return reply.status_code == HTTP_OK

    # --------------------------------------------------------------- the model

    async def call_tool(
        self,
        tool: str,
        args: dict[str, Any],
        *,
        session: str,
        turn: str,
        family: str = HA_REVIEW,
    ) -> ToolCall:
        """One tool call from inside a running job, through the REAL bridge.

        The four variables are the ones `buildTurnEnv` gives a pi child
        (contract 03 §7), with `PEP_URL` pointed at this harness's PEP. No
        `AGENT_TURN_FILE`: the playpen owns that file for a live session,
        and `turn-context.ts` falls back to `AGENT_TURN` when it is absent,
        which is the same turn id the host started.
        """
        report = self.stack.log_dir / f"bridge-{tool}-{new_ulid()}.json"
        argv = [
            "node",
            str(BRIDGE_DRIVER),
            str(BRIDGE_BUNDLE),
            str(report),
            tool,
            json.dumps(args),
        ]
        child = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={
                "PATH": _host_path(),
                "PEP_URL": self.pep_url,
                "PEP_TOKEN": self.pep_token(family),
                "AGENT_SESSION": session,
                "AGENT_TURN": turn,
            },
        )
        _, errors = await asyncio.wait_for(child.communicate(), timeout=DRIVER_TIMEOUT_S)

        if child.returncode != 0:
            raise AssertionError(f"bridge_driver exited {child.returncode}: {errors.decode()}")

        return _read_report(report)

    # -------------------------------------------------------------- the trigger

    async def accepted_turn(self, family: str, session: str) -> dict[str, Any]:
        """Contract 02 §5.1 then §5.4, as a door makes them.

        `Stack.door_to_attendance` is the second client at the second boundary
        (`AGENTS.md` 8): the Open WebUI door does create-or-find and runs a
        turn inside one request, so nothing else can read back the `state`
        an `accepted` turn was admitted with.
        """
        client = self.stack.door_to_attendance
        if client is None:
            raise AssertionError("the stack is not serving yet")

        created = await client.post("/v1/sessions", json={"family": family, "session": session})
        if created.status_code not in {HTTP_OK, HTTP_CREATED}:
            raise AssertionError(f"create-or-find refused: {created.status_code} {created.text}")

        ran = await client.post(
            f"/v1/sessions/{family}/{session}/turns",
            json={
                "prompt": ATTENDED_PROMPT,
                "idempotency_key": new_ulid(),
                "wait": "accepted",
            },
        )
        if ran.status_code != HTTP_ACCEPTED:
            raise AssertionError(f"run turn refused: {ran.status_code} {ran.text}")

        body: dict[str, Any] = ran.json()

        return body

    # -------------------------------------------------------------- the trigger

    def trigger_client(self) -> HttpAttendance:
        """The door's own `attendance` client, built from its own config type.

        Synchronous on purpose (`door-trigger/attendance.py`), so every call
        through it runs on a worker thread: `attendance` serves on this test's
        event loop and a blocking call on that loop would deadlock it.
        """
        socket = self.stack.attendance_socket
        if socket is None:
            raise AssertionError("the stack is not serving yet")

        token = (
            (self.stack.state_root / "tokens" / f"{Principal.DOOR_TRIGGER.value}.token")
            .read_text(encoding="utf-8")
            .strip()
        )

        return HttpAttendance(AttendanceTarget(url="http://sessiond", socket=socket, token=token))

    async def fire(
        self,
        family: str,
        *,
        trigger: str | None = None,
        payload: str | None = None,
    ) -> FireOutcome:
        """One firing through the door's own `fire_trigger`.

        This is the clock. `door-trigger` has no timer loop: `caregiver` writes
        a systemd timer per cron entry and the timer runs `agent-trigger
        fire`, so a harness that decides when to call this decides when the
        timer fired.
        """
        kind = TriggerKind.WEBHOOK if trigger else TriggerKind.TIMER
        firing = Firing(family=family, kind=kind, name=trigger, payload=payload)
        client = self.trigger_client()

        try:
            return await asyncio.to_thread(fire_trigger, client, firing)
        finally:
            await asyncio.to_thread(client.close)

    async def fire_from_cli(
        self, family: str, *, trigger: str | None = None, payload: str | None = None
    ) -> ExitCode:
        """The same firing through `agent-trigger fire`'s own body.

        `execute_fire` is what the CLI runs, so a refusal here is the exit
        code systemd records and the journal line the operator reads.
        """
        client = self.trigger_client()

        try:
            return await asyncio.to_thread(execute_fire, client, family, trigger, payload)
        finally:
            await asyncio.to_thread(client.close)

    def webhook_token(self, family: str, name: str) -> str:
        """The bearer `caregiver` minted for one declared webhook.

        Contract 05 §6.4, packet S5F item C: nothing here writes this file
        any more. `apply_all()` mints it from the family file alone, which
        is what invariant 16 asks for and what an operator now gets on a
        fresh host.
        """
        path = caregiver_paths.webhook_token_path(self.stack.state_root, family, name)
        if not path.is_file():
            raise AssertionError(f"caregiver minted no webhook token at {path}")

        return path.read_text(encoding="utf-8").strip()

    @property
    def webhooks_dir(self) -> Path:
        return self.stack.state_root / "triggers" / "webhooks"

    def serve_config(self) -> ServeConfig:
        """The webhook listener's own config, built from its own type.

        The bind fields are never used: `serving()` puts the app on loopback,
        because a test binds nothing another host can reach. Everything else
        is what the unit file sets on the host.
        """
        socket = self.stack.attendance_socket
        if socket is None:
            raise AssertionError("the stack is not serving yet")

        token = (
            (self.stack.state_root / "tokens" / f"{Principal.DOOR_TRIGGER.value}.token")
            .read_text(encoding="utf-8")
            .strip()
        )

        return ServeConfig(
            attendance=AttendanceTarget(url="http://sessiond", socket=socket, token=token),
            bind_host="127.0.0.1",
            bind_port=0,
            families_dir=self.stack.families_dir,
            registry_root=REGISTRY_ROOT,
            webhooks_dir=self.webhooks_dir,
            refresh_s=1.0,
        )

    async def post_webhook(
        self, family: str, name: str, body: bytes, *, token: str | None = None
    ) -> httpx.Response:
        """One external call on the real listener, exactly as Node-RED makes it.

        With no `token`, it presents what `caregiver` minted, which is what
        Home Assistant would hold after the operator pasted the file's contents in.
        """
        token = token if token is not None else self.webhook_token(family, WEBHOOK_NAME)
        async with httpx.AsyncClient() as client:
            return await client.post(
                f"{self.webhook_url}/triggers/{family}/{name}",
                content=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                timeout=30.0,
            )

    # ----------------------------------------------------------------- set-up

    def sandbox_environ(self) -> dict[str, str]:
        """Stage 1's sandbox environment, minus the one seam three families
        must not share.

        `SESSIOND_SANDBOX_SESSIONS_MOUNT` names ONE sessions root for every
        family, so three families would run every job in `chat`'s directory
        (`AGENTS.md` 17). Its default is already `<sessions root>/<family>/`,
        which is what this harness uses, so there is nothing to override.
        """
        kept = dict(self.stack.sandbox_environ())
        kept.pop("SESSIOND_SANDBOX_SESSIONS_MOUNT", None)

        return kept

    def open_delegate_door(self) -> Path:
        """Put `door-delegate.token` at the mode the host deploys it with
        (contract 02 §3 rule 5's one exception)."""
        path = self.stack.state_root / "tokens" / f"{Principal.DOOR_DELEGATE.value}.token"
        path.chmod(DELEGATE_TOKEN_MODE)

        return path

    def restart_service(self) -> SessionService:
        """A second `attendance` over the same state, as a restart leaves it.

        Contract 02 §13.3: the queue lives in memory, so a fresh process ends
        every `queued` turn it finds and starts with an empty one. The caller
        closes the first service before this one starts, because two services
        on one state root is not a restart.
        """
        socket = self.stack.attendance_socket
        if socket is None:
            raise AssertionError("the stack never served")

        service = SessionService(
            Config(
                sessions_root=self.stack.sessions_root,
                state_root=self.stack.state_root,
                work_root=self.stack.work_root,
                socket_path=socket,
                bind=Bind.SOCKET_ONLY,
                lan_address="192.0.2.10",
                lan_port=8350,
                channel_command=NO_CHANNEL_COMMAND,
                log_dir=self.stack.log_dir,
                lock_stale_s=LOCK_STALE_S,
                lock_poll_s=LOCK_POLL_S,
            )
        )
        service.start()

        return service


def build_gatekeeper(transport_url: str, limit_s: float = APPROVAL_LIMIT_S) -> Gatekeeper:
    """Contract 04 §8's real gate, with a limit a test can outlive.

    The object, the notifier and the deadline arithmetic are the PEP's own.
    Only the two numbers move, the way `stack.py` moves contract 03 §11's
    three lock numbers so scenario 9 runs in tenths of a second.
    """
    return Gatekeeper(
        HttpApprovalNotifier(transport_url, FIXTURE_HOOK_TOKEN),
        limit_s=limit_s,
        poll_s=REVOKE_POLL_S,
    )


@contextmanager
def serving_stage5(
    stage: Stage5,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    limit_s: float = APPROVAL_LIMIT_S,
) -> Iterator[Stage5]:
    """The fake transport, the real PEP and the real webhook listener.

    Each server is started before the next port is taken, so a port this
    process already listens on cannot be handed out twice. Loopback only: a
    test binds nothing another host can reach.

    `limit_s` is contract 04 §8.5's 15 minutes, shortened. A scenario that
    watches the gate EXPIRE passes a small number. Every other scenario
    wants one it cannot lose a race against, because `attendance` reads the
    gate off the audit file on a one-second loop.
    """
    from pep_harness import build_pep, free_port, serving

    socket = stage.stack.attendance_socket
    if socket is None:
        raise AssertionError("the stack is not serving yet")

    with ExitStack() as running:
        transport_url = running.enter_context(serving(stage.transport.app, free_port()))
        hook_url = f"{transport_url}{HOOK_PATH}"

        monkeypatch.setenv("PEP_SESSIOND_SOCKET", str(socket))
        monkeypatch.setenv("PEP_DELEGATE_TOKEN_FILE", str(stage.open_delegate_door()))
        # Set even though the gatekeeper is passed in: `main()` then builds
        # the same config a real unit builds, and `build_gatekeeper` would
        # have produced the same object from it.
        monkeypatch.setenv("PEP_APPROVAL_URL", hook_url)
        app = build_pep(
            monkeypatch,
            tmp_path,
            stage.stack.state_root,
            secrets={
                "approval_hook_token": FIXTURE_HOOK_TOKEN,
                "approval_callback_token": FIXTURE_CALLBACK_TOKEN,
            },
            gatekeeper=build_gatekeeper(hook_url, limit_s),
        )

        stage.pep_url = running.enter_context(serving(app, free_port()))
        stage.transport.pep_url = stage.pep_url

        listener = create_webhook_app(
            stage.serve_config(),
            stage.trigger_client(),
            RouteTable(
                registry_root=REGISTRY_ROOT,
                webhooks_dir=stage.webhooks_dir,
                families=StatusFiles(stage.stack.families_dir),
            ),
        )
        stage.webhook_url = running.enter_context(serving(listener, free_port()))

        yield stage


def _bearer_of(request: Request) -> str:
    header = request.headers.get("authorization", "")
    prefix = "Bearer "

    return header[len(prefix) :].strip() if header.startswith(prefix) else ""


def _split_action(action: str) -> tuple[str, str, str]:
    """`AGENT_APPROVE_<family>_<gate>` back into its three parts.

    The verb holds one underscore of its own and the gate holds none, so the
    verb is the first two segments and the gate is whatever follows the last
    underscore. Contract 04 §8.4 forbids an underscore in either field, which
    is what makes this reversible at all.
    """
    head, _, tail = action.partition("_")
    word, _, rest = tail.partition("_")
    family, _, gate = rest.rpartition("_")

    return f"{head}_{word}", family, gate


def _host_path() -> str:
    """`PATH`, so the driver finds `node`. The child gets nothing else."""
    return os.environ.get("PATH", "/usr/bin:/bin")
