"""One session in two UIs, and the platform fence (packet I4, stage 4).

Stage 1's harness holds the Open WebUI door, `attendance` and the playpen.
Stage 4 puts the REAL TUI door beside them on the same session, and a fake
Open WebUI chat API behind `attendance`, so the operator's stage 4 test runs off the
host:

```
  httpx (the phone)          agent-tui (the terminal)
    │ /v1/chat/completions      │ TuiDoor -> HttpAttendance
    ▼                           ▼
  door-owui  ───uds──►  attendance  ──uds──►  (its own writer lease)
                           │  │ stdio, contract 03
                           │  ▼
                           │  fake_sbx.py ─► playpen.js ─► fake-pi.mjs
                           │
                           └─http──►  FakeOwui, on loopback
                                      POST /api/v1/chats/new
                                      POST /api/v1/chats/{id}
                                      POST /api/v1/chats/{id}/folder
```

Nothing under test is faked. The TUI door is `agent_door_tui`'s own code,
built the way `__main__` builds it. `attendance` reaches the fake Open WebUI
through its own `HttpChatApi`, over real HTTP, because that class holds both
the response reader and the 10-second timeout stage 4 has questions about.

Three stand-ins, none under test: `fake_sbx.py` and `fake-pi.mjs`, which the
gate already owns, and `FakeDriver`/`FakeLiteLLMKeys`, because a Mac has no
`sbx` and no test may mint a key.

Two notes a reader needs.

1. **A terminal's own turns never reach this journal.** On the host the TUI
   hands its tty to pi inside the sandbox (contract 03 §7.6), which writes
   the pi store directly and journals nothing. So a scenario that needs a
   turn UNDER the TUI's lease runs it through `attendance` as `door-tui`. That
   is the only TUI-door turn a journal can hold, and it is what proves the
   lease and the ordering. `Stack.attendance_as` is the same argument packet
   CS makes for `caregiver`: no door sits in front of the call.
2. **The fake Open WebUI serves on its own thread.** `HttpChatApi` is
   synchronous, and `attendance` runs it on a worker thread (contract 02 §10.4
   rule 5). A fake on this test's event loop could not answer while such a
   call was in flight, so the scenario that holds a write open to measure
   the loop would deadlock instead of measuring it. That scenario is what
   put rule 5's "off the turn's path" where it is: before packet I4 the call
   was made on the loop itself, and one settled turn stood the whole process
   still for six seconds.
"""

from __future__ import annotations

import asyncio
import json
import stat
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Final

import pytest
import yaml
from agent_door_owui.headers import PARENT_ID_HEADER, USER_MESSAGE_ID_HEADER
from agent_door_tui.app import TuiDoor
from agent_door_tui.attendance import HttpAttendance as TuiAttendance
from agent_door_tui.config import TuiConfig
from agent_door_tui.launch import TerminalRunner
from agent_door_tui.picker import ScriptedTerminal
from agent_door_tui.status import StatusFiles
from attendance.api import DOOR_INSTANCE_HEADER
from attendance.auth import Principal
from attendance.config import Config
from caregiver.apply import ApplyResult, apply_once
from caregiver.driver import FakeDriver
from caregiver.litellm_keys import FakeLiteLLMKeys
from conftest import chat_body, owui_headers
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from stack import FAMILY, LOCK_BEAT_MS, Stack, fake_pi_script, repo_root

from caregiver import paths as caregiver_paths

#: The two attended families stage 4 asks about, plus the delegate contract
#: 01 §5.3 makes them name. `chat` is the stack's own family and is not in
#: this registry: scenarios 1 to 6 drive the doors, not the reconciler.
CODE: Final = "code"
AGENT_CONTROL: Final = "agent-control"
VAULT_ORACLE: Final = "vault-oracle"
PLATFORM_FAMILIES: Final = (CODE, AGENT_CONTROL)

#: Every family the fixture registry holds. A rewritten copy carries all
#: three, because contract 01 §5.3 checks that each delegate exists.
REGISTRY_FAMILIES: Final = (*PLATFORM_FAMILIES, VAULT_ORACLE)

#: The fixture registry, checked in beside this suite.
REGISTRY_ROOT: Final = repo_root() / "integration" / "fixtures" / "stage4-registry"

#: An obvious fixture, never resolved: contract 06 owns digest selection.
IMAGE: Final = "sha256:" + "0" * 63 + "2"

#: The platform root (`docs/rework/spec.md` §4.4, invariant 10). A `code`
#: sandbox spec that names any path under it has lost the file half of the
#: fence.
PLATFORM_ROOT: Final = "/srv/agents/work/platform"

#: An obvious fixture, never a credential (invariant 13). It is long enough
#: to look like a key and it is written into a 0600 file, because that is how
#: `attendance` reads one (contract 02 §10.4).
FIXTURE_OWUI_KEY: Final = "fixture-owui-api-key-" + "o" * 32

#: Contract 02 §10.4 step 2's folder. Any id: Open WebUI owns the value and
#: `attendance` only forwards it.
FIXTURE_FOLDER_ID: Final = "fixture-folder-0001"

#: Probe 0c's three paths, measured on the host against Open WebUI v0.11.3.
CHATS_NEW: Final = "/api/v1/chats/new"
CHAT_PATH: Final = "/api/v1/chats/{chat}"
CHAT_FOLDER_PATH: Final = "/api/v1/chats/{chat}/folder"

#: Contract 02 §3 rule 5's one exception. The PEP runs as another user on
#: the host, so this file alone carries the group-read bit.
DELEGATE_TOKEN_MODE: Final = 0o640

_TOKEN_MODE: Final = 0o600
_HTTP_OK: Final = 200
_HTTP_SERVER_ERROR: Final = 500

#: `tests_manager/chaperone_harness.py` builds the real PEP through its real entry
#: point, and holds the two helpers that put a FastAPI app on a loopback
#: port. `stage3.py` reaches it the same way.
sys.path.insert(0, str(repo_root() / "integration" / "tests_manager"))


class OwuiMood(Enum):
    """What the fake Open WebUI does with the next write."""

    ANSWER = "answer"
    REFUSE = "refuse"
    DAWDLE = "dawdle"


@dataclass
class OwuiCall:
    """One request the fake recorded, as a scenario reads it back.

    `ok` is false until the fake answers it. A refused write is recorded
    too, so a scenario can see what it CARRIED, but only an answered one
    reached the chat.
    """

    path: str
    body: dict[str, Any]
    ok: bool = False

    def messages(self) -> dict[str, Any]:
        """The `history.messages` map of a create or an append."""
        chat = self.body.get("chat")
        history = chat.get("history") if isinstance(chat, dict) else None

        return history.get("messages", {}) if isinstance(history, dict) else {}

    def current_id(self) -> str:
        chat = self.body.get("chat")
        history = chat.get("history") if isinstance(chat, dict) else None

        return str(history.get("currentId", "")) if isinstance(history, dict) else ""


class FakeOwui:
    """Open WebUI's three write paths, as probe 0c measured them.

    The chat id sits at the TOP level of the answer and the chat body under
    `.chat`. That is what the probe read back (`probes/0c-owui-writes/run.sh`
    steps 2 and 3), and it is the whole reason this is an HTTP server rather
    than a stub behind `ChatApi`: the shape is what `attendance` parses.

    `mood` is a scenario's one knob. `DAWDLE` holds each write for
    `dawdle_s`, which is how the turn-settle path's cost is measured from the
    outside.
    """

    def __init__(self) -> None:
        self.calls: list[OwuiCall] = []
        self.mood = OwuiMood.ANSWER
        self.dawdle_s = 0.0
        #: How many writes are inside the server right now. A scenario reads
        #: it to prove something else finished DURING a slow write.
        self.in_flight = 0
        self._chats = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------- the app

    def app(self) -> FastAPI:
        """The three paths, and nothing else. An unknown path 404s."""
        app = FastAPI()

        @app.post(CHATS_NEW)
        async def _new(request: Request) -> Any:  # pyright: ignore[reportUnusedFunction]
            body = await _body_of(request)
            return await self._answer(CHATS_NEW, body, self._create(body))

        @app.post(CHAT_PATH.format(chat="{chat}"))
        async def _append(chat: str, request: Request) -> Any:  # pyright: ignore[reportUnusedFunction]
            body = await _body_of(request)
            return await self._answer(CHAT_PATH.format(chat=chat), body, {"id": chat})

        @app.post(CHAT_FOLDER_PATH.format(chat="{chat}"))
        async def _file(chat: str, request: Request) -> Any:  # pyright: ignore[reportUnusedFunction]
            body = await _body_of(request)
            path = CHAT_FOLDER_PATH.format(chat=chat)
            return await self._answer(path, body, {"id": chat, "folder_id": body.get("folder_id")})

        return app

    async def _answer(self, path: str, body: dict[str, Any], head: dict[str, Any]) -> Any:
        """Record, then behave as `mood` says.

        The record goes in before the mood is applied, so a scenario can see
        what a refused write CARRIED as well as that it was refused.
        """
        with self._lock:
            call = OwuiCall(path, body)
            self.calls.append(call)
            self.in_flight += 1

        try:
            if self.mood is OwuiMood.DAWDLE:
                # The response is withheld for `dawdle_s`, which is what a
                # hung Open WebUI does to its caller. The wait yields this
                # server's loop, so a second write still gets in: a blocking
                # sleep here would make the FAKE the bottleneck and the
                # scenario would measure the wrong process.
                await asyncio.sleep(self.dawdle_s)

            if self.mood is OwuiMood.REFUSE:
                return _refusal()

            call.ok = True

            # Probe 0c step 2: `.id` and `.title` at the top level, the chat
            # body under `.chat`, which has no id of its own.
            return {**head, "chat": body.get("chat", {}), "updated_at": int(time.time())}
        finally:
            with self._lock:
                self.in_flight -= 1

    def _create(self, body: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._chats += 1
            made = f"owui-fake-chat-{self._chats}"

        chat = body.get("chat")
        title = chat.get("title", "") if isinstance(chat, dict) else ""

        return {"id": made, "title": title, "folder_id": body.get("folder_id")}

    # ---------------------------------------------------------- assertions

    def paths(self) -> list[str]:
        with self._lock:
            return [call.path for call in self.calls]

    def answered(self) -> list[OwuiCall]:
        """Every write that reached the chat, in arrival order.

        Creates, appends and filings together: one list in one order is
        what proves a session's turns arrived oldest first.
        """
        with self._lock:
            return [call for call in self.calls if call.ok]

    def creates(self) -> list[OwuiCall]:
        return self._of(CHATS_NEW)

    def appends(self) -> list[OwuiCall]:
        """Every append, which is every chat write that is not a create."""
        with self._lock:
            return [
                call
                for call in self.calls
                if call.path != CHATS_NEW and not call.path.endswith("/folder")
            ]

    def filings(self) -> list[OwuiCall]:
        with self._lock:
            return [call for call in self.calls if call.path.endswith("/folder")]

    def _of(self, path: str) -> list[OwuiCall]:
        with self._lock:
            return [call for call in self.calls if call.path == path]


class Stage4:
    """The stage 1 stack, the real TUI door, and the fake Open WebUI."""

    def __init__(self, stack: Stack, owui: FakeOwui) -> None:
        self.stack = stack
        self.owui = owui
        self.driver = FakeDriver()
        self.litellm = FakeLiteLLMKeys()
        self._fork_dir = stack.state_root.parent / "pi-forks"

    # ------------------------------------------------------- the TUI door

    def tui_config(self, instance: str, sbx: Path | str = "/nonexistent/sbx") -> TuiConfig:
        """The config `agent_door_tui.__main__` builds, on this stack."""
        socket = self.stack.attendance_socket

        if socket is None:
            raise AssertionError("the stack is not serving yet")

        token = (self.stack.state_root / "tokens" / f"{Principal.DOOR_TUI.value}.token").read_text(
            encoding="utf-8"
        )

        return TuiConfig(
            attendance_token=token.strip(),
            attendance_url="http://sessiond",
            attendance_socket=socket,
            families_dir=self.stack.families_dir,
            sbx=str(sbx),
            pi_launch=LAUNCHER,
            door_instance=instance,
        )

    def tui_client(self, instance: str) -> TuiAttendance:
        """The TUI door's own client, for a scenario that drives one call."""
        return TuiAttendance(self.tui_config(instance))

    def tui_door(
        self,
        instance: str,
        sbx: Path,
        runner: TerminalRunner | None = None,
        answers: list[str] | None = None,
    ) -> tuple[TuiDoor, TuiAttendance, ScriptedTerminal]:
        """One whole `agent-tui` run, assembled as `__main__` assembles it."""
        config = self.tui_config(instance, sbx)
        client = TuiAttendance(config)
        screen = ScriptedTerminal(answers if answers is not None else [])
        door = TuiDoor(
            client,
            StatusFiles(config.families_dir),
            screen,
            runner if runner is not None else RecordingTerminal(),
            config.door_instance,
            config.sbx,
            config.pi_launch,
        )

        return door, client, screen

    # ------------------------------------------------------- the two doors

    async def owui_turn(
        self,
        chat: str,
        message: str,
        text: str = "hello",
        *,
        user_message: str | None = None,
        parent: str | None = None,
    ) -> Any:
        """One non-streamed turn through the Open WebUI door, as the phone
        sends it.

        `message` is the assistant message Open WebUI is about to fill,
        `user_message` the user message beside it, and `parent` the message
        the user message hangs from. Contract 02 §10.1 puts two rows per
        settled turn in `owui_map` from the first two, and §10.2 reads the
        third to decide whether this turn continues or branches. A scenario
        that sends only `message` maps half of each turn, which is enough
        to continue and not enough to fork.
        """
        client = self.stack.client

        if client is None:
            raise AssertionError("the stack is not serving yet")

        extra: dict[str, str] = {}

        if user_message is not None:
            extra[USER_MESSAGE_ID_HEADER] = user_message

        if parent is not None:
            extra[PARENT_ID_HEADER] = parent

        return await client.post(
            OWUI_CHAT_PATH,
            headers=owui_headers(chat, message, **extra),
            json=chat_body(text, stream=False),
        )

    async def tui_turn(self, session: str, prompt: str, instance: str) -> dict[str, Any]:
        """One turn under the TUI door's lease.

        See this module's note 1: a terminal's own turns never reach the
        journal, so the only TUI-door turn a scenario can observe is this
        one, run as `door-tui` with the header that names the terminal.
        """
        client = self.stack.attendance_as(Principal.DOOR_TUI)

        try:
            answer = await client.post(
                f"/v1/sessions/{FAMILY}/{session}/turns",
                headers={DOOR_INSTANCE_HEADER: instance},
                json={"prompt": prompt, "wait": "settled"},
            )
        finally:
            await client.aclose()

        if answer.status_code != _HTTP_OK:
            raise AssertionError(f"the tui turn was refused: {answer.text}")

        body: dict[str, Any] = answer.json()

        return body

    # ---------------------------------------------------- what to assert on

    def lines_of(self, session: str, *kinds: str) -> list[dict[str, Any]]:
        """The journal of one session, filtered to the kinds a scenario reads."""
        wanted = set(kinds)

        return [line for line in self.stack.journal_lines(session) if line["kind"] in wanted]

    def holders(self, session: str) -> list[tuple[str, str]]:
        """Every writer change, as (holder, reason), in journal order."""
        return [
            (str(line["body"]["holder"]), str(line["body"]["reason"]))
            for line in self.lines_of(session, "writer_changed")
        ]

    def forks(self) -> list[dict[str, Any]]:
        """Every `fork` the fake pi answered, in call order.

        One file per pi process, because two shells appending to one file
        lose a row under load. The files sort by
        pid, so within one process the order holds and that is the only
        order a fork scenario asks about.
        """
        if not self._fork_dir.is_dir():
            return []

        found: list[dict[str, Any]] = []
        for path in sorted(self._fork_dir.iterdir()):
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    found.append(json.loads(line))

        return found

    # ------------------------------------------------------------ the fleet

    def apply_platform(self) -> tuple[ApplyResult, ...]:
        """`apply_once` for `code` and `agent-control`, off the fixture files."""
        return tuple(self.apply_one(REGISTRY_ROOT, name) for name in PLATFORM_FAMILIES)

    def apply_one(self, registry_root: Path, family: str) -> ApplyResult:
        """The real manager. Both fakes are held on this object rather than
        made per call, because both stand in for something that outlives one
        apply."""
        return apply_once(
            registry_root,
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
        scenario that adds a forbidden mount writes its own copy.
        """
        for name in REGISTRY_FAMILIES:
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

        _copy_servers(registry_root)

        return registry_root

    def mounts_of(self, family: str) -> tuple[str, ...]:
        """Every path `caregiver` told the driver to mount for one family.

        This is contract 05 §4.3's own argument list, which is as close to
        `sbx create` as a Mac reaches. What only the host can prove is that
        the VM then holds exactly these and nothing else.
        """
        found: list[str] = []
        for call in self.driver.calls:
            if call.op != "create":
                continue

            spec = call.args[0]
            if getattr(spec, "name", "").startswith(f"{family}-"):
                found.extend(mount.path for mount in spec.mounts)

        return tuple(found)

    def family_token(self, family: str) -> str:
        """The family token `caregiver` minted, as the sandbox holds it."""
        body = json.loads(
            caregiver_paths.creds_path(self.stack.state_root, family).read_text(encoding="utf-8")
        )

        return str(body["pep_token"])

    # ---------------------------------------------------------------- set-up

    def write_pi_shim(self) -> Path:
        """Stage 1's shim, plus one thing it has no reason to carry.

        `FAKE_PI_FORK_DIR` is how a branch scenario sees the `fork` call the
        playpen makes: no channel message reports it, and `buildTurnEnv`
        passes five names through, so the shim is the only way in. One file
        per pi process, so two shells never append to one file.
        """
        self._fork_dir.mkdir(parents=True, exist_ok=True)
        path = self.stack.state_root.parent / "pi4"
        lock = f"{self.stack.pi_spawn_log}.lock"
        path.write_text(
            "#!/bin/sh\n"
            "n=0\n"
            f'until mkdir "{lock}" 2>/dev/null || [ "$n" -ge 500 ]; '
            "do n=$((n + 1)); sleep 0.01; done\n"
            f'printf "%s\\n" "$*" >> "{self.stack.pi_spawn_log}"\n'
            f'rmdir "{lock}"\n'
            f'FAKE_PI_FORK_LOG="{self._fork_dir}/$$.log"\n'
            "export FAKE_PI_FORK_LOG\n"
            f'. "{self.stack.state_root.parent / "pi-env.sh"}"\n'
            f'exec node "{fake_pi_script()}" "$@"\n',
            encoding="utf-8",
        )
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        return path

    def sandbox_environ(self) -> dict[str, str]:
        """Stage 1's environment, with this stage's own pi shim.

        No mount path is here. The three travel in `supervisor.env` through
        `--env-file`, exactly as on the host (`AGENTS.md` 15).
        """
        return {
            "AGENT_PI_BIN": str(self.write_pi_shim()),
            "AGENT_LOCK_BEAT_MS": str(LOCK_BEAT_MS),
            "FAKE_SBX_IMAGE_ENV": "AGENT_PI_BIN,AGENT_LOCK_BEAT_MS",
            "SESSIOND_SANDBOX_SESSIONS_MOUNT": str(self.stack.mounts.sessions),
        }

    def open_delegate_door(self) -> Path:
        """Contract 02 §3 rule 5's one exception, at the host's own mode.

        The PEP runs as user `chaperone` and `attendance` owns the file, so group
        read is what makes the delegate door reachable at all. Stage 3
        proves that rule; stage 4 only needs the PEP to start.
        """
        path = self.stack.state_root / "tokens" / f"{Principal.DOOR_DELEGATE.value}.token"
        path.chmod(DELEGATE_TOKEN_MODE)

        return path

    def write_owui_key(self) -> Path:
        """Contract 02 §10.4: the key is in a FILE, never in the environment."""
        path = self.stack.state_root / "owui-api.key"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(FIXTURE_OWUI_KEY + "\n", encoding="utf-8")
        path.chmod(_TOKEN_MODE)

        return path


@dataclass
class RecordingTerminal:
    """A `TerminalRunner` that records the argv and exits 0.

    The real one boots a VM and hands it the tty. What a stage 4 scenario
    asks of it is the argv, and packet CV already proves the launcher itself
    against the built bundle.
    """

    argv: list[list[str]] = field(default_factory=list[list[str]])
    code: int = 0

    def run(self, argv: list[str]) -> int:
        self.argv.append(list(argv))

        return self.code

    def last(self) -> list[str]:
        if not self.argv:
            raise AssertionError("the door never handed the terminal over")

        return self.argv[-1]


class HeldTerminal:
    """A `TerminalRunner` that runs a callback instead of `sbx`.

    A scenario that has to act WHILE a terminal holds the lease needs the
    door to stay inside `run`, because that is the only window in which the
    lease is held and no turn is in flight.
    """

    def __init__(self, run: Callable[[list[str]], int]) -> None:
        self._run = run

    def run(self, argv: list[str]) -> int:
        return self._run(argv)


#: Contract 03 §7.6's launcher, at the path the sandbox image puts it.
LAUNCHER: Final = "/opt/agent-supervisor/agent-pi-launch.js"

#: The one route the Open WebUI door serves.
OWUI_CHAT_PATH: Final = "/v1/chat/completions"


def recording_sbx(root: Path, name: str = "s4-sbx") -> tuple[Path, Path]:
    """An `sbx` that records its argv and exits 0.

    The real one boots a VM and runs pi's interactive UI in it, which no Mac
    does. The argv is what a scenario asserts on.
    """
    log = root / f"{name}-argv.txt"
    path = root / name
    path.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" >> "{log}"\nexit 0\n', encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)

    return path, log


@contextmanager
def serving_pep(stage: Stage4, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[str]:
    """The REAL PEP on a loopback port, reading this stage's grant files.

    `chaperone_harness.build_pep` runs `chaperone.__main__.main()` and stops it
    one statement before it serves, so the config comes from the
    environment exactly as the unit file supplies it. The fence question
    scenario 7 asks is a decision, and this is where the decision is made.
    """
    from chaperone_harness import build_pep, free_port, serving

    socket_path = stage.stack.attendance_socket

    if socket_path is None:
        raise AssertionError("the stack is not serving yet")

    monkeypatch.setenv("PEP_SESSIOND_SOCKET", str(socket_path))
    monkeypatch.setenv("PEP_DELEGATE_TOKEN_FILE", str(stage.open_delegate_door()))
    app = build_pep(monkeypatch, tmp_path, stage.stack.state_root)

    with serving(app, free_port()) as url:
        yield url


@contextmanager
def serving_owui(owui: FakeOwui) -> Iterator[str]:
    """Put the fake Open WebUI on a loopback port, on its own thread.

    Loopback, never the LAN address: a test binds nothing another host can
    reach. Its own thread, because `attendance` calls
    it synchronously from the turn-settle path — a server on this test's loop
    could not answer while that call was in flight.
    """
    from chaperone_harness import free_port, serving

    with serving(owui.app(), free_port()) as url:
        yield url


def owui_config(stage: Stage4, url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Teach the stack's own `Config` the three fields of contract 02 §10.4.

    `stack.py` builds the `Config` and has no reason to know about Open
    WebUI, so this wraps the name it calls rather than copying its field
    list, its channel command or its two lock numbers.
    """
    key_file = stage.write_owui_key()
    built = Config

    def with_owui(**fields: Any) -> Config:
        return built(
            **fields, owui_url=url, owui_key_file=key_file, owui_folder_id=FIXTURE_FOLDER_ID
        )

    monkeypatch.setattr("stack.Config", with_owui)


async def settle(check: Callable[[], bool], what: str, timeout: float = 10.0) -> None:
    """Spin until a condition holds, or say what never happened.

    Contract 02 §8.2: the door's stream ends at pi's `agent_settled` and
    `attendance` writes `turn_settled` after that. A reader that looks the
    instant a stream ends sees the turn still in flight, so every read of the
    journal's tail goes through here.
    """
    deadline = asyncio.get_running_loop().time() + timeout

    while not check():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out waiting for {what}")

        await asyncio.sleep(0.02)


async def in_thread[Answer](work: Callable[[], Answer]) -> Answer:
    """Run one synchronous door call without blocking the stack's loop."""
    return await asyncio.to_thread(work)


async def _body_of(request: Request) -> dict[str, Any]:
    try:
        parsed: object = await request.json()
    except ValueError:
        return {}

    return parsed if isinstance(parsed, dict) else {}


def _refusal() -> Any:
    return JSONResponse(status_code=_HTTP_SERVER_ERROR, content={"detail": "fixture refusal"})


def _copy_servers(registry_root: Path) -> None:
    """Carry the `mcp/` half of the fixture registry into a rewritten copy.

    Contract 01 §5.2 checks every server a family names, so a copy that held
    only `families/` would fail for a reason no scenario is about.
    """
    for source in sorted((REGISTRY_ROOT / "mcp").iterdir()):
        target = registry_root / "mcp" / source.name
        target.mkdir(parents=True, exist_ok=True)
        (target / "server.yaml").write_text(
            (source / "server.yaml").read_text(encoding="utf-8"), encoding="utf-8"
        )
