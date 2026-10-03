"""Three families, one real PEP, one real `attendance` (packet I3, stage 3).

Stage 1's harness holds the door, `attendance` and the playpen. Stage 2 put
the reconciler beside them. Stage 3 adds the PEP, and with it the first call
that leaves one family's sandbox and lands in another's:

    bridge_driver.mjs (the chat sandbox's model, running the REAL bridge)
      │ POST /call {tool: invoke_agent}          contract 04 §7.1
      ▼
    the REAL PEP family app  ──grant file──► allow, mint a delegation id
      │ POST /delegate over attendance's Unix socket, Bearer door-delegate
      ▼
    the REAL attendance ──► job-<ulid> in the THIN family ──► one turn
      │ fake_sbx.py exec --env-file ──► node dist/playpen.js
      ▼                                             │
    {status, session_id, content}                   ▼
      │                                    playpen/test/fake-pi.mjs
      ▼
    {untrusted: true, source: "family:<target>", content: ...}

Three fakes, none of them under test: `FakeDriver`, because a Mac has no
`sbx`; `FakeLiteLLMKeys`, because no test may mint a key; and `fake-pi.mjs`,
the playpen package's own double. Everything from the family file to the
wrapped answer is the real code.

Why the bridge runs in its own process rather than inside `fake-pi.mjs`:
`buildTurnEnv` hardcodes `PEP_URL` at the host's LAN address, which is right
on the host and unreachable here, so a pi child launched by the real
playpen cannot reach a PEP on loopback. The driver stands in for the pi
process and carries the same five variables contract 03 §7 gives one. The
bridge and the PEP on the path are the real ones either way, which is what
packet I3 asks for.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import pytest
import yaml
from attendance.auth import Principal
from attendance.ids import SessionPrefix, new_ulid
from caregiver.apply import ApplyResult, apply_once
from caregiver.driver import FakeDriver
from caregiver.litellm_keys import FakeLiteLLMKeys
from fastapi import FastAPI
from stack import FAMILY, LOCK_BEAT_MS, Stack, fake_pi_script, repo_root

from caregiver import paths as caregiver_paths

#: The caller and the two delegates of `docs/rework/spec.md` §4.4, as the
#: fixture registry holds them. `chat` is `stack.FAMILY`, named again here so
#: a reader of this module sees all three in one place.
CHAT: Final = FAMILY
VAULT_ORACLE: Final = "vault-oracle"
CODE_SANDBOX: Final = "code-sandbox"
FAMILIES: Final = (CHAT, VAULT_ORACLE, CODE_SANDBOX)

#: The fixture registry, checked in beside this suite.
REGISTRY_ROOT: Final = repo_root() / "integration" / "fixtures" / "stage3-registry"

#: An obvious fixture, never resolved: contract 06 owns digest selection.
IMAGE: Final = "sha256:" + "0" * 63 + "1"

#: The one alias `FakeLiteLLMKeys` serves, and the alias every fixture family
#: names. `fast` is the real thin-family alias on the host
#: (`docs/rework/spec.md` §4.4).
MODEL_ALIAS: Final = "agent-router"

#: Contract 02 §3 rule 5's one exception. The PEP runs as another user on
#: the host, so this file alone carries the group-read bit.
DELEGATE_TOKEN_MODE: Final = 0o640

#: The bridge bundle and the driver that plays pi around it. The driver is
#: `tests_manager`'s, unchanged: a second copy could drift from the one
#: packet C2's seam 2 proves the PEP against.
BRIDGE_DRIVER: Final = repo_root() / "integration" / "tests_manager" / "node" / "bridge_driver.mjs"
BRIDGE_BUNDLE: Final = repo_root() / "playpen" / "dist" / "pep-bridge.js"

#: A driver run is one PEP call plus one job turn. The PEP's own limit is 120
#: seconds (contract 04 §7.5), so this has to outlast it to observe it.
DRIVER_TIMEOUT_S: Final = 180.0

#: What each pi process leaves in its working directory, plus its own pid. A
#: later job of the same chat finds it; a job of another chat does not.
LEFT_BY_PREFIX: Final = "left-by-"

#: argv, cwd, listing. A shorter row is a shim that did not finish writing.
_PI_LOG_FIELDS: Final = 3

#: What `sbx exec` would leave in a child's environment, plus the three seams
#: this harness needs. The playpen mount paths are NOT here: they travel in
#: `supervisor.env` through `--env-file`, as on the host (`AGENTS.md` 15).
_IMAGE_SEAMS: Final = ("AGENT_PI_BIN", "AGENT_LOCK_BEAT_MS", "AGENT_CODE_SANDBOX_ROOT")

#: `tests_manager/pep_harness.py` builds the real PEP through its real entry
#: point. Importing it by path beats a second copy of `main()`'s plumbing
#: here. `tests_manager/conftest.py` reaches `attendance/tests` the same way.
sys.path.insert(0, str(repo_root() / "integration" / "tests_manager"))


def bridge_bundle_missing() -> bool:
    """The PEP bridge is built by the same `pnpm build` as the playpen."""
    return not BRIDGE_BUNDLE.is_file()


@dataclass(frozen=True)
class ToolCall:
    """What the bridge reported for one tool call, as the model would see it.

    `text` is the rendered untrusted block on success. `error` is the text a
    denial throws, which pi shows the model as a tool error.
    """

    ok: bool
    text: str
    error: str
    registered: tuple[str, ...]


class Stage3:
    """The stage 1 stack, with two thin families and the PEP beside it.

    `caregiver` publishes all three families from the fixture registry, so
    every status document, grant file and `supervisor.env` on the path is
    the one the real reconciler writes (`AGENTS.md` 12).
    """

    def __init__(self, stack: Stack) -> None:
        self.stack = stack
        self.driver = FakeDriver()
        self.litellm = FakeLiteLLMKeys()
        self.pep_url = ""
        self.work_root = stack.work_root
        self._pi_log = stack.work_root.parent / "pi-cwd.log"

    # ------------------------------------------------------------ the fleet

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
        """A copy of the fixture registry with one field of one family
        changed, for a scenario that has to drive the FILE.

        The fixture files are read-only inputs checked into the repo, so a
        scenario that needs a different `job.timeout` writes its own copy
        rather than editing them under the next test.
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

    # ------------------------------------------------------- what to assert on

    def pep_token(self, family: str) -> str:
        """The family token `caregiver` minted, as the sandbox holds it."""
        body = json.loads(
            (caregiver_paths.creds_path(self.stack.state_root, family)).read_text(encoding="utf-8")
        )
        return str(body["pep_token"])

    def audit_lines(self) -> list[dict[str, Any]]:
        """Every audit v2 line the PEP wrote, in file order (contract 04 §6)."""
        lines: list[dict[str, Any]] = []
        for path in sorted((self.stack.state_root / "audit").glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                lines.append(json.loads(line))

        return lines

    def owner_dir(self, owner_session: str) -> Path:
        """The per-chat `code-sandbox` directory (contract 02 §12.1)."""
        return self.work_root / CODE_SANDBOX / owner_session

    def link_path(self, owner_session: str) -> Path:
        """The path a job sees, the one the operator named (`docs/rework/spec.md` §7.2)."""
        return Path(f"/tmp/code-sandbox-{owner_session}")

    def sessions_of(self, family: str) -> list[str]:
        """Every session directory that family still has on disk."""
        directory = self.stack.sessions_root / family
        if not directory.is_dir():
            return []

        return sorted(one.name for one in directory.iterdir() if one.is_dir())

    def job_cwds(self) -> list[str]:
        """The working directory of every pi process this harness started.

        The turn's cwd is the whole of contract 03 §7.2 rule 3, and no
        protocol line carries it, so the shim reports it from inside.
        """
        return [row[1] for row in self._pi_rows()]

    def job_findings(self) -> list[tuple[str, ...]]:
        """What each pi process FOUND in its working directory at start.

        One row per process, in start order. This is the only way to prove
        that a second job of one chat can read what the first left: a job
        that looked at its own directory from the outside would prove
        nothing about what the sandbox sees.
        """
        return [
            tuple(sorted(name for name in row[2].split(" ") if name)) for row in self._pi_rows()
        ]

    def _pi_rows(self) -> list[list[str]]:
        """One tab-separated record per pi process: argv, cwd, listing."""
        if not self._pi_log.exists():
            return []

        rows = [
            line.split("\t")
            for line in self._pi_log.read_text(encoding="utf-8").splitlines()
            if "\t" in line
        ]

        return [row for row in rows if len(row) >= _PI_LOG_FIELDS]

    # ------------------------------------------------------------- the model

    def job_turn_files(self, family: str) -> list[Path]:
        """Every live job's turn file in one thin family's control mount.

        The playpen writes it at `start_turn` and removes it with the
        session (contract 03 §7.4), so a reader sees one only while a job
        still holds its pi process.
        """
        sessions = caregiver_paths.control_dir(self.stack.state_root, family, f"{family}-s1")

        return sorted(sessions.glob(f"sessions/{SessionPrefix.JOB.value}*/turn.json"))

    # ------------------------------------------------------------- the model

    async def call_tool(
        self,
        tool: str,
        args: dict[str, Any],
        *,
        session: str,
        family: str = CHAT,
        delegation: str | None = None,
    ) -> ToolCall:
        """One tool call from one family's sandbox, through the real bridge.

        The five variables are the ones `buildTurnEnv` sets for a pi child
        (contract 03 §7), with `PEP_URL` pointed at this harness's PEP. The
        turn file is the playpen's own per-turn record (§7.4), written
        into that family's control mount. `delegation` is the id a DELEGATED
        sandbox runs inside, which the bridge sends as `X-Delegation-Id`.
        """
        report = self.stack.log_dir / f"bridge-{tool}-{os.urandom(4).hex()}.json"
        turn = _new_turn_id()
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
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "PEP_URL": self.pep_url,
                "PEP_TOKEN": self.pep_token(family),
                "AGENT_SESSION": session,
                "AGENT_TURN": turn,
                "AGENT_TURN_FILE": str(self._write_turn_file(family, session, turn, delegation)),
            },
        )
        _, errors = await asyncio.wait_for(child.communicate(), timeout=DRIVER_TIMEOUT_S)

        if child.returncode != 0:
            raise AssertionError(f"bridge_driver exited {child.returncode}: {errors.decode()}")

        return _read_report(report)

    def _write_turn_file(
        self, family: str, session: str, turn: str, delegation: str | None
    ) -> Path:
        """Contract 03 §7.4. The playpen writes this per turn; the driver
        stands in for the playpen as well as for pi."""
        sandbox = f"{family}-s1"
        directory = (
            caregiver_paths.control_dir(self.stack.state_root, family, sandbox)
            / "sessions"
            / session
        )
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "turn.json"
        path.write_text(
            json.dumps({"session": session, "turn": turn, "delegation": delegation}) + "\n",
            encoding="utf-8",
        )

        return path

    # ---------------------------------------------------------------- set-up

    def write_pi_shim(self) -> Path:
        """A pi shim that also reports the two things the channel never says.

        Stage 1's shim logs argv and execs the fake pi. This one logs two
        more fields first: the process's working directory, which is
        contract 03 §7.2 rule 3's whole outcome, and what that directory
        already held, which is how a scenario proves the next job of the
        same chat reads what this one left. Then it leaves a file of its own
        behind, named after this process, so the next job has something to
        find.

        Two jobs at once start two shims at once. One record is over 1 KiB,
        which the shell writes in more than one piece, so two appends ran
        into each other under load and one row was lost. A lock directory
        makes the shims take turns.
        """
        path = self.stack.work_root.parent / "pi3"
        lock = f"{self._pi_log}.lock"
        path.write_text(
            "#!/bin/sh\n"
            "n=0\n"
            f'until mkdir "{lock}" 2>/dev/null || [ "$n" -ge 500 ]; '
            "do n=$((n + 1)); sleep 0.01; done\n"
            f'printf "%s\\t%s\\t%s\\n" "$*" "$PWD" '
            '"$(ls -A "$PWD" 2>/dev/null | tr "\\n" " ")" '
            f'>> "{self._pi_log}"\n'
            f'rmdir "{lock}"\n'
            f': > "$PWD/{LEFT_BY_PREFIX}$$"\n'
            f'. "{self.stack.work_root.parent / "pi-env.sh"}"\n'
            f'exec node "{fake_pi_script()}" "$@"\n',
            encoding="utf-8",
        )
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        return path

    def sandbox_environ(self) -> dict[str, str]:
        """This process's own environment, holding no mount path.

        `SESSIOND_SANDBOX_SESSIONS_MOUNT` is deliberately absent, unlike
        stage 1's. It names ONE sessions root for every family, so with
        three families it would run every thin job in `chat`'s directory.
        Its default is already the host path this harness uses,
        `<sessions root>/<family>/`, so there is nothing to override.
        """
        return {
            # Stage 1's number, imported rather than re-chosen: the three lock
            # values move together (`stack.py`).
            "AGENT_PI_BIN": str(self.write_pi_shim()),
            "AGENT_LOCK_BEAT_MS": str(LOCK_BEAT_MS),
            # Contract 03 §7.1 rule 6's seam for the work root. It must name
            # the directory `attendance` creates under its own `work_root`, or
            # the chat and the job would work in two different places.
            "AGENT_CODE_SANDBOX_ROOT": str(self.work_root / CODE_SANDBOX),
            "FAKE_SBX_IMAGE_ENV": ",".join(_IMAGE_SEAMS),
        }

    def open_delegate_door(self) -> Path:
        """Put `door-delegate.token` at the mode the host deploys it with.

        Contract 02 §3 rule 5's exception: the PEP runs as user `pep` and
        `attendance` owns the file, so group read is what makes the door
        reachable at all. 0600 would be readable here — one user owns
        everything in a test — which is why the scenario for it asserts on
        the two readers' code rather than on a failed open.
        """
        path = self.stack.state_root / "tokens" / f"{Principal.DOOR_DELEGATE.value}.token"
        path.chmod(DELEGATE_TOKEN_MODE)

        return path


def build_pep_app(
    stage: Stage3, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, socket_path: Path
) -> FastAPI:
    """The real PEP, with contract 04 §7's two settings filled in.

    `pep_harness.build_pep` runs `agent_pep.__main__.main()` and stops it one
    statement before it serves, so the config comes from the environment
    exactly as it does in the unit file.
    """
    # Imported here, not at the top: the name resolves only after the
    # `sys.path.insert` above, and an import block runs before any statement.
    from pep_harness import build_pep

    monkeypatch.setenv("PEP_SESSIOND_SOCKET", str(socket_path))
    monkeypatch.setenv("PEP_DELEGATE_TOKEN_FILE", str(stage.open_delegate_door()))

    return build_pep(monkeypatch, tmp_path, stage.stack.state_root)


@contextmanager
def serving_pep(stage: Stage3, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Stage3]:
    """Put the real PEP on a loopback port and point the stage at it.

    Loopback, never the LAN address: a test binds nothing another host can
    reach. The PEP serves in its own thread with its
    own event loop, so `attendance` keeps serving on this test's loop while a
    delegate call is in flight on the PEP's.
    """
    from pep_harness import free_port, serving

    socket_path = stage.stack.attendance_socket
    if socket_path is None:
        raise AssertionError("the stack is not serving yet")

    app = build_pep_app(stage, monkeypatch, tmp_path, socket_path)

    with serving(app, free_port()) as url:
        stage.pep_url = url
        yield stage


def _read_report(path: Path) -> ToolCall:
    body: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    call = body.get("call") or {}

    return ToolCall(
        ok=bool(call.get("ok")),
        text=str(call.get("text", "")),
        error=str(call.get("error", "")),
        registered=tuple(str(one["name"]) for one in body.get("tools", [])),
    )


def _new_turn_id() -> str:
    """A ULID in contract 04 §4.1's alphabet, so the header is not dropped."""
    return new_ulid()
