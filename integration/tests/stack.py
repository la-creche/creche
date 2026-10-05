"""One running stack: the real door, the real `attendance`, the real playpen.

    httpx  ──http/uds──►  door-owui  ──http/uds──►  attendance
                                                       │ stdio (contract 03)
                                                       ▼
                                        fake_sbx.py exec --env-file ...
                                                       │ (execs)
                                                       ▼
                                          node dist/playpen.js
                                                       │ stdio (pi rpc)
                                                       ▼
                                           playpen/test/fake-pi.mjs

Nothing here is a mock of a thing under test. Both Python services run under
their own uvicorn listener on a Unix socket, so the door speaks to `attendance`
over the transport it uses on the host. Two stand-ins: `fake-pi.mjs`, the
playpen package's own double for `pi --mode rpc`, reached through the
`AGENT_PI_BIN` seam `playpen/src/launcher.ts` already provides, and
`fake_sbx.py`, which reproduces what `sbx exec --env-file` does with an
environment and nothing else.

Temp directories stand in for the sandbox mounts, the sessions root and the
state root, because a Mac has no /srv. The playpen learns where they are
the same way it does on the host: from the `supervisor.env` `caregiver` writes,
handed to the channel command as `--env-file`.
`SESSIOND_SANDBOX_SESSIONS_MOUNT` is the one remaining host-side seam.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import stat
import sys
import tempfile
from collections.abc import Callable, Generator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import uvicorn
from agent_door_owui.app import create_app
from agent_door_owui.attendance import HttpAttendance
from agent_door_owui.config import DoorConfig
from agent_door_owui.families import StatusFiles
from attendance.api import build_app
from attendance.auth import Principal, TokenBook
from attendance.config import Bind, Config
from attendance.service import SessionService
from caregiver.playpen_env import write_playpen_env

from caregiver import paths as caregiver_paths

FAMILY = "chat"
SANDBOX = "chat-s1"
CONFIG_REV = "reg-c1-integration"
CRED_EPOCH = 7
MODEL = f"agent:{FAMILY}"

# Obvious fixtures, never credentials (invariant 13). They exist so a test can
# prove a value crossed a boundary, and each is long enough to pass the 32-byte
# floor contract 02 §3 rule 7 puts on a token file.
DOOR_KEY = "fixture-door-key-" + "d" * 32
FIXTURE_LITELLM_KEY = "FIXTURE-LITELLM-KEY"
FIXTURE_PEP_TOKEN = "FIXTURE-PEP-TOKEN"

# macOS caps a Unix socket path at 104 bytes, so the sockets sit at the root of
# a short temp directory rather than under the per-test tree.
MAX_SOCKET_PATH = 100

# Contract 03 §11.1 rule 3 and §11.4 rule 4, shrunk so scenario 9 is measured
# in tenths of a second rather than in the 20 seconds the defaults would cost.
# All three move together: `lock_stale_s` is four beat windows, and shrinking
# one alone would run this suite at a ratio the contract forbids.
LOCK_BEAT_MS = 150
LOCK_STALE_S = 0.6
LOCK_POLL_S = 0.05

_TOKEN_MODE = 0o600
_START_TIMEOUT_S = 10.0
_STOP_GRACE_S = 2.0
_POLL_S = 0.01


def repo_root() -> Path:
    """The checkout this file lives in. `integration/tests/stack.py` is 3 deep."""
    return Path(__file__).resolve().parents[2]


def playpen_bundle() -> Path:
    """The built playpen. `pnpm build` in `playpen/` writes it."""
    return repo_root() / "playpen" / "dist" / "playpen.js"


def fake_pi_script() -> Path:
    """The playpen package's own double for `pi --mode rpc`."""
    return repo_root() / "playpen" / "test" / "fake-pi.mjs"


def fake_sbx_script() -> Path:
    """The stand-in for `sbx exec --env-file`. See its own docstring."""
    return Path(__file__).resolve().parent / "fake_sbx.py"


@dataclass(frozen=True)
class Mounts:
    """The four sandbox mounts of contract 03 §7.1, as host directories.

    Three are the family's and both sandboxes of a switch share them. The
    control directory is per sandbox, because §11.1's lock, §7.4's turn
    files and §7.5's process records all belong to one VM.
    """

    sessions: Path
    creds: Path
    config: Path
    control: Path


class Stack:
    """Every process and directory one scenario needs, built once."""

    def __init__(self, root: Path, socket_dir: Path) -> None:
        self._root = root
        self._socket_dir = socket_dir
        self.sessions_root = root / "sessions"
        self.state_root = root / "state"
        self.work_root = root / "work"
        self.log_dir = root / "log"
        self.pi_spawn_log = root / "pi-spawns.log"
        self.client: httpx.AsyncClient | None = None
        self.door_to_attendance: httpx.AsyncClient | None = None
        #: Where `attendance` listens, for a scenario that dials it with a
        #: client of its own — `caregiver`'s switch client, for one.
        self.attendance_socket: Path | None = None
        self._door_to_attendance: HttpAttendance | None = None
        self._service: SessionService | None = None
        self._servers: list[_Listener] = []
        self._clients: list[httpx.AsyncClient] = []

    # ------------------------------------------------------------------ paths

    @property
    def families_dir(self) -> Path:
        return self.state_root / "families"

    def mounts_of(self, sandbox: str) -> Mounts:
        """The four host directories `caregiver` would mount for one sandbox.

        Each is built with `caregiver`'s own path functions, so the fixture
        and the file `write_playpen_env` writes cannot disagree.
        """
        return Mounts(
            sessions=self.sessions_root / FAMILY,
            creds=caregiver_paths.creds_dir(self.state_root, FAMILY),
            config=caregiver_paths.config_dir(self.state_root, FAMILY),
            control=caregiver_paths.control_dir(self.state_root, FAMILY, sandbox),
        )

    @property
    def mounts(self) -> Mounts:
        """The first sandbox's mounts. Most scenarios never see a second."""
        return self.mounts_of(SANDBOX)

    def playpen_env_of(self, sandbox: str) -> Path:
        """The env file `sbx exec --env-file` is handed (contract 03 §7.1).

        One per sandbox: it names `AGENT_SANDBOX` and that sandbox's own
        control directory, so the two live sandboxes of a switch cannot
        share it.
        """
        return caregiver_paths.playpen_env_path(self.state_root, FAMILY, sandbox)

    @property
    def playpen_env(self) -> Path:
        return self.playpen_env_of(SANDBOX)

    def session_dir(self, session: str) -> Path:
        return self.sessions_root / FAMILY / session

    def journal_lines(self, session: str) -> list[dict[str, Any]]:
        """The host journal of one session, as contract 02 §8 wrote it."""
        path = self.session_dir(session) / "journal.ndjson"
        if not path.exists():
            return []

        raw = path.read_bytes().split(b"\n")

        return [json.loads(line) for line in raw if line.strip()]

    def pi_starts(self) -> list[str]:
        """One line per pi process the playpen actually started."""
        if not self.pi_spawn_log.exists():
            return []

        return [line for line in self.pi_spawn_log.read_text(encoding="utf-8").split("\n") if line]

    # ------------------------------------------------------------------ setup

    def build_fixture(self) -> None:
        """Write everything `caregiver` would publish for the `chat` family.

        Packet B1 is not merged, so the status document (contract 05 §2) and
        the three control-mount files are written by hand from the contracts.
        """
        mounts = self.mounts
        for directory in (mounts.sessions, mounts.creds, mounts.config, mounts.control):
            directory.mkdir(parents=True, exist_ok=True)

        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.work_root.mkdir(parents=True, exist_ok=True)

        self.write_status()
        self.add_sandbox(SANDBOX)
        self._write_creds()
        self._write_runtime()
        self._write_tokens()
        self._write_door_key()
        self._write_pi_shim()

    def write_status(
        self,
        *,
        sandbox_state: str = "ready",
        state: str = "in_sync",
        sandboxes: tuple[tuple[str, str], ...] = ((SANDBOX, "ready"),),
    ) -> None:
        """One status document, exactly as contract 05 §2 shapes it.

        `sandboxes` carries the whole list, which a switch needs: contract
        05 §5 has `caregiver` publish the replacement before it calls.
        `sandbox_state` sets the state of the one default row.
        """
        rows = sandboxes if sandboxes != ((SANDBOX, "ready"),) else ((SANDBOX, sandbox_state),)
        for box, _ in rows:
            self.add_sandbox(box)

        document: dict[str, Any] = {
            "family": FAMILY,
            "kind": "attended",
            "state": state,
            "written_at": _rfc3339(),
            "config_rev": CONFIG_REV,
            "validation": {"never_valid": False},
            "faults": [],
            "sandboxes": [
                {
                    "id": box,
                    "state": box_state,
                    "supervisor_env": str(self.playpen_env_of(box)),
                }
                for box, box_state in rows
            ],
            "credentials": {"epoch": CRED_EPOCH},
            "limits": {"job_timeout_s": 120},
        }
        path = self.families_dir / FAMILY / "status.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document), encoding="utf-8")

    def add_sandbox(self, sandbox: str) -> None:
        """Everything `caregiver`'s own create leaves behind for one sandbox:
        an empty control directory and that sandbox's `supervisor.env`.

        The env file comes from `caregiver`'s own writer, not a hand-made
        copy of its format. A second implementation here could drift from
        the one that runs on the host, and the drift would look like a
        passing test.

        It is idempotent, and it never empties a directory that already
        exists: a scenario writes the status document more than once and a
        live playpen's lock must survive that.
        """
        self.mounts_of(sandbox).control.mkdir(parents=True, exist_ok=True)

        if self.playpen_env_of(sandbox).exists():
            return

        write_playpen_env(
            self.playpen_env_of(sandbox),
            state_root=self.state_root,
            family=FAMILY,
            sandbox=sandbox,
        )

    def _write_creds(self) -> None:
        """Contract 03 §12's credential file, read at every pi process start."""
        document = {
            "epoch": CRED_EPOCH,
            "litellm_key": FIXTURE_LITELLM_KEY,
            "pep_token": FIXTURE_PEP_TOKEN,
            "written_at": _rfc3339(),
        }
        (self.mounts.creds / "creds.json").write_text(json.dumps(document) + "\n", encoding="utf-8")

    def _write_runtime(self) -> None:
        """Contract 01 §6.1's `runtime.json`, the family config mount."""
        (self.mounts.config / "runtime.json").write_text(
            json.dumps({"model_alias": "agent-router"}) + "\n", encoding="utf-8"
        )

    def _write_tokens(self) -> None:
        """One token file per principal, at the mode contract 02 §3 names."""
        for principal in Principal:
            path = self.state_root / "tokens" / f"{principal.value}.token"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{principal.value}-{'x' * 32}\n", encoding="utf-8")
            path.chmod(_TOKEN_MODE)

    def _write_door_key(self) -> None:
        path = self.state_root / "door-owui.key"
        path.write_text(DOOR_KEY + "\n", encoding="utf-8")
        path.chmod(_TOKEN_MODE)

    def _write_pi_shim(self) -> None:
        """`AGENT_PI_BIN` needs one executable. `fake-pi.mjs` is not one.

        The shim does three things and nothing else.

        1. Appends one line per start. That is the process record scenario 7
           reads: a held-open process means a second message adds no line.
        2. Sources `pi-env.sh`, because `buildTurnEnv` passes five names
           through and no more, so `FAKE_PI_*` cannot reach the child any
           other way. The playpen's own harness adds the same variables
           the same way, outside the production environment logic.
        3. `exec`s, so this stays ONE process and stdin EOF and every signal
           reach the fake pi unchanged.

        Two sessions can start two shims at once. The shell may write a long
        line in more than one piece, so the append sits behind a lock
        directory: the stage 3 shim lost a row that way under load.
        """
        self.set_pi_env()
        path = self._root / "pi"
        lock = f"{self.pi_spawn_log}.lock"
        path.write_text(
            "#!/bin/sh\n"
            "n=0\n"
            f'until mkdir "{lock}" 2>/dev/null || [ "$n" -ge 500 ]; '
            "do n=$((n + 1)); sleep 0.01; done\n"
            f'printf "%s\\n" "$*" >> "{self.pi_spawn_log}"\n'
            f'rmdir "{lock}"\n'
            f'. "{self._pi_env_file}"\n'
            f'exec node "{fake_pi_script()}" "$@"\n',
            encoding="utf-8",
        )
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    @property
    def _pi_env_file(self) -> Path:
        return self._root / "pi-env.sh"

    def set_pi_env(self, **values: int) -> None:
        """Retune the fake pi for the next process it starts.

        `FAKE_PI_EVENTS` is the delta count and `FAKE_PI_DELAY_MS` the gap
        between them. A scenario that has to act mid-turn makes the turn long
        enough to act inside.
        """
        lines = [f"export FAKE_PI_{name.upper()}={number}" for name, number in values.items()]
        self._pi_env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def sandbox_environ(self) -> dict[str, str]:
        """This process's own environment, holding no mount path.

        The three mount paths are NOT here. They travel in `supervisor.env`,
        through `--env-file`, exactly as on the host: an `attendance` that built
        its command without that flag would start a playpen with none of
        them, and every turn would fail (contract 03 §7.1).

        `AGENT_PI_BIN` and `AGENT_LOCK_BEAT_MS` stand in for what the sandbox
        image supplies, so `fake_sbx.py` forwards them by name rather than
        the child reading them off the host.
        """
        return {
            "AGENT_PI_BIN": str(self._root / "pi"),
            "AGENT_LOCK_BEAT_MS": str(LOCK_BEAT_MS),
            "FAKE_SBX_IMAGE_ENV": "AGENT_PI_BIN,AGENT_LOCK_BEAT_MS",
            "SESSIOND_SANDBOX_SESSIONS_MOUNT": str(self.mounts.sessions),
        }

    def playpen_lock(self, sandbox: str = SANDBOX) -> Path:
        """The lease a killed playpen leaves behind (contract 03 §11.1).

        Per sandbox, because the control directory is (§7.1).
        """
        return self.mounts_of(sandbox).control / "supervisor.lock"

    # ------------------------------------------------------------------ serve

    async def serve(self) -> httpx.AsyncClient:
        """Start both services and return a client that plays Open WebUI."""
        attendance_socket = self._socket_path("s.sock")
        door_socket = self._socket_path("d.sock")
        self.attendance_socket = attendance_socket

        self._service = SessionService(self._attendance_config(attendance_socket))
        self._service.start()
        self._service.start_upkeep()

        tokens = TokenBook(self.state_root)
        tokens.load()
        await self._listen(build_app(self._service, tokens), attendance_socket)

        door_config = self._door_config(attendance_socket)
        # Kept, because the door's own client to `attendance` holds a keep-alive
        # connection for the life of the app. An unclosed one keeps the
        # `attendance` listener's `wait_closed()` waiting for ever at teardown.
        self._door_to_attendance = HttpAttendance(door_config)
        door = create_app(
            door_config, self._door_to_attendance, StatusFiles(door_config.families_dir)
        )
        await self._listen(door, door_socket)

        self.client = self._client(door_socket)
        self.door_to_attendance = self._attendance_client(attendance_socket)

        return self.client

    def attendance_as(self, principal: Principal) -> httpx.AsyncClient:
        """A client to `attendance` holding one principal's token.

        `caregiver`'s one call has no door in front of it (contract 05 §5),
        so a scenario that switches a sandbox speaks to `attendance` directly.
        """
        path = self.attendance_socket

        if path is None:
            raise AssertionError("the stack is not serving yet")

        return self._attendance_client(path, principal)

    def _attendance_client(
        self, path: Path, principal: Principal = Principal.DOOR_OWUI
    ) -> httpx.AsyncClient:
        """A door's own client to `attendance`, for the calls the door makes first.

        Contract 02 §5.1's create-or-find is its own HTTP call. The Open WebUI
        door makes it and then runs a turn in one request, so this is the only
        way to observe what happens between the two.
        """
        token = (self.state_root / "tokens" / f"{principal.value}.token").read_text(
            encoding="utf-8"
        )
        client = httpx.AsyncClient(
            base_url="http://sessiond",
            transport=httpx.AsyncHTTPTransport(uds=str(path)),
            headers={"Authorization": f"Bearer {token.strip()}"},
            timeout=httpx.Timeout(5.0, read=60.0),
        )
        self._clients.append(client)

        return client

    def _client(self, path: Path) -> httpx.AsyncClient:
        """An Open WebUI stand-in: bearer key, base url, real Unix socket."""
        client = httpx.AsyncClient(
            base_url="http://door",
            transport=httpx.AsyncHTTPTransport(uds=str(path)),
            headers={"Authorization": f"Bearer {DOOR_KEY}"},
            timeout=httpx.Timeout(5.0, read=60.0),
        )
        self._clients.append(client)

        return client

    def _attendance_config(self, socket_path: Path) -> Config:
        # The SHAPE of the command on the host, with `sbx` replaced and
        # nothing else: the two template fields, their order and the
        # `--sandbox` flag are the ones `exec_channel.DEFAULT_COMMAND` uses.
        command = (
            f'"{sys.executable}" "{fake_sbx_script()}" exec '
            "--env-file {env_file} {sandbox} -- "
            f'node "{playpen_bundle()}" --sandbox {{sandbox}}'
        )

        return Config(
            sessions_root=self.sessions_root,
            state_root=self.state_root,
            work_root=self.work_root,
            socket_path=socket_path,
            bind=Bind.SOCKET_ONLY,
            lan_address="192.0.2.10",
            lan_port=8310,
            channel_command=command,
            log_dir=self.log_dir,
            lock_stale_s=LOCK_STALE_S,
            lock_poll_s=LOCK_POLL_S,
        )

    def _door_config(self, attendance_socket: Path) -> DoorConfig:
        token = (self.state_root / "tokens" / "door-owui.token").read_text(encoding="utf-8")

        return DoorConfig(
            bind_host="127.0.0.1",
            bind_port=8320,
            door_key=DOOR_KEY,
            attendance_token=token.strip(),
            attendance_url="http://sessiond",
            attendance_socket=attendance_socket,
            families_dir=self.families_dir,
        )

    def _socket_path(self, name: str) -> Path:
        path = self._socket_dir / name
        if len(str(path)) > MAX_SOCKET_PATH:
            raise RuntimeError(f"socket path {path} is too long for this platform")

        return path

    async def _listen(self, app: object, path: Path) -> None:
        listener = _Listener(app, path)
        self._servers.append(listener)
        await listener.start()

    # ---------------------------------------------------------------- teardown

    async def close(self) -> None:
        """Take the stack down in the order that cannot block.

        Clients first, so no connection is still reading. The service next,
        which ends every channel, so a listener has no in-flight streaming
        response left to wait for. Then the children are reaped, because an
        `asyncio` child that outlives its loop warns from another test's
        setup and hides whatever really failed.
        """
        children = self._channel_processes()

        for client in self._clients:
            await client.aclose()

        if self._door_to_attendance is not None:
            await self._door_to_attendance.aclose()
            self._door_to_attendance = None

        if self._service is not None:
            await self._service.close()

        await _reap(children)

        for listener in self._servers:
            await listener.stop()

    def channel_pids(self) -> list[int]:
        """The playpen processes `attendance` is holding open, for a kill test."""
        return [process.pid for process in self._channel_processes()]

    def channel_argv(self) -> list[list[str]]:
        """The argv of every channel `attendance` opened, in dial order.

        No contract puts this on a wire, so reading it back is a test
        reaching into one process it hosts. It is what proves the two flags
        of contract 03 §7.1 are in the command and not only in a default.
        """
        service = self._service

        return [] if service is None else _live_channel_argv(service)

    def _channel_processes(self) -> list[asyncio.subprocess.Process]:
        service = self._service

        return [] if service is None else _live_channel_procs(service)


class _Listener:
    """One uvicorn server on a Unix socket, inside this test's event loop."""

    def __init__(self, app: object, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        config = uvicorn.Config(
            app,  # pyright: ignore[reportArgumentType]
            uds=str(path),
            log_level="warning",
            access_log=False,
            timeout_graceful_shutdown=int(_STOP_GRACE_S),
        )
        self._server = _QuietServer(config)
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._server.serve())
        await until(lambda: self._server.started, "the listener to bind")

    async def stop(self) -> None:
        """Ask, then insist. A connection a test abandoned never closes itself."""
        self._server.should_exit = True
        task = self._task
        self._task = None

        if task is None:
            return

        done, _ = await asyncio.wait({task}, timeout=_START_TIMEOUT_S)
        if done:
            return

        # A connection a test abandoned can outlive even `force_exit`, and a
        # listener that will not stop must not become the failure a reader
        # sees instead of the real one.
        self._server.force_exit = True
        task.cancel()

        with contextlib.suppress(asyncio.CancelledError):
            await task


class _QuietServer(uvicorn.Server):
    """Signals belong to pytest, not to a listener inside one test.

    `serve` of uvicorn enters `capture_signals` and sets its own handlers for
    SIGINT and SIGTERM there. This override sets none. Each signal then keeps
    the handler that it had before the test.
    """

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        yield


def _live_channel_argv(service: SessionService) -> list[list[str]]:
    """Every open channel's argv. `_live_channel_procs` says why this reaches
    into the service rather than asking it."""
    found: list[list[str]] = []
    links: dict[str, Any] = getattr(service, "_links", {})

    for link in links.values():
        channel = getattr(link, "_channel", None)
        argv = getattr(channel, "argv", None)

        if isinstance(argv, list):
            found.append(cast("list[str]", argv))

    return found


def _live_channel_procs(service: SessionService) -> list[asyncio.subprocess.Process]:
    """Reach into the service for the playpen children it opened.

    A test that kills the playpen needs the pid, and teardown needs to wait
    for the child. No contract puts either on the wire. Reading the private
    link map is honest about that: it is a test reaching into one process it
    hosts, not a new interface.
    """
    found: list[asyncio.subprocess.Process] = []
    links: dict[str, Any] = getattr(service, "_links", {})

    for link in links.values():
        channel = getattr(link, "_channel", None)
        process = getattr(channel, "_process", None)

        if isinstance(process, asyncio.subprocess.Process):
            found.append(process)

    return found


async def _reap(children: list[asyncio.subprocess.Process]) -> None:
    """End every playpen child before the event loop goes."""
    for child in children:
        if child.returncode is not None:
            continue

        with contextlib.suppress(ProcessLookupError):
            child.kill()

    for child in children:
        with contextlib.suppress(TimeoutError, ProcessLookupError):
            await asyncio.wait_for(child.wait(), timeout=_STOP_GRACE_S)


async def until(check: Callable[[], bool], what: str, timeout: float = _START_TIMEOUT_S) -> None:
    """Spin until a condition holds, or say what never happened."""
    deadline = asyncio.get_running_loop().time() + timeout

    while not check():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out waiting for {what}")

        await asyncio.sleep(_POLL_S)


def _rfc3339() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def short_temp_dir() -> tempfile.TemporaryDirectory[str]:
    """A directory short enough to hold a Unix socket path on macOS."""
    return tempfile.TemporaryDirectory(prefix="agi")


def socket_dir_is_usable(path: Path) -> bool:
    """A cheap check that the platform accepts a socket at this path."""
    probe = path / "probe.sock"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.bind(str(probe))
    except OSError:
        return False
    finally:
        probe.unlink(missing_ok=True)

    return True
