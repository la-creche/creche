"""Start a program as a child process, wait for it, and end all of it.

Nothing here knows what a service is. `Supervisor` takes the words of a
command and an environment, and gives back a `Child`:

1. The child leads its own process group, so one signal reaches every
   process it started, the way a unit's control group does on the host.
2. Its stdout and its stderr each go to a file. A failed test puts both in
   its report (`conftest.py`).
3. `wait_ready` polls an address until an HTTP answer arrives, the child
   exits, or the deadline passes.
4. `free_port` gives a loopback port and holds it for the test, so two runs
   of this suite on one machine never take the same port.
5. `stop_all` ends every group and says which one it had to kill.

The registry at the bottom is the suite's check at session end: a group that
no teardown confirmed gone is a leak.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import signal
import socket
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

#: How long a start may take. A cold Python import of a service is about one
#: second on a laptop, and a loaded CI machine is several times slower.
READY_DEADLINE_S: Final = 30.0

#: How long a command that runs to its end may take.
RUN_DEADLINE_S: Final = 30.0

#: How long a group has after SIGTERM before SIGKILL. An idle service stops
#: in well under a second. The shortest `TimeoutStopSec` of a unit is 20.
STOP_GRACE_S: Final = 20.0

#: How long a killed group has to leave the process table.
REAP_DEADLINE_S: Final = 10.0

_POLL_S: Final = 0.02
_PROBE_TIMEOUT_S: Final = 1.0
_PROBE_REQUEST: Final = b"GET / HTTP/1.1\r\nHost: probe\r\nConnection: close\r\n\r\n"
_HTTP_ANSWER: Final = b"HTTP/"

#: How much of one output file a failure report carries.
_TAIL_BYTES: Final = 16_384

#: Ports for a loopback listener. Below every platform's ephemeral range, so
#: no client connection of a parallel test takes one between the check here
#: and the bind in the child.
_PORT_FLOOR: Final = 20_000
_PORT_CEILING: Final = 32_000
_PORT_ATTEMPTS: Final = 50
LOOPBACK: Final = "127.0.0.1"

#: One file per port, shared by every run of this suite that one user starts
#: on one machine. A run holds a port by a lock on its file. The system drops
#: the lock when the process ends, so a killed run leaves no port held. This
#: directory is the one thing a test touches outside its root.
_PORT_LOCK_DIR: Final = Path(tempfile.gettempdir()) / f"creche-proc-ports-{os.getuid()}"
_LOCK_FILE_MODE: Final = 0o600


class ProcError(Exception):
    """A child did not do what the harness needed. The message holds its output."""


@dataclass(frozen=True, slots=True)
class UnixAddress:
    """A Unix socket path."""

    path: Path


@dataclass(frozen=True, slots=True)
class TcpAddress:
    """A loopback port. Never another host and never every interface."""

    port: int
    host: str = LOOPBACK


Address = UnixAddress | TcpAddress


@dataclass(frozen=True, slots=True)
class Finished:
    """One command that ran to its end."""

    exit_code: int
    stdout: str
    stderr: str


@dataclass(slots=True)
class Child:
    """One running command and the files that hold what it wrote."""

    name: str
    words: tuple[str, ...]
    popen: subprocess.Popen[bytes]
    stdout_path: Path
    stderr_path: Path

    @property
    def pgid(self) -> int:
        """The process group. The child leads it, so it is the child's pid."""
        return self.popen.pid

    def exit_code(self) -> int | None:
        """The exit code, or None while the child runs."""
        return self.popen.poll()

    def wait(self, deadline_s: float) -> int:
        """Wait for the child to exit by itself. Raises when it does not."""
        try:
            return self.popen.wait(timeout=deadline_s)
        except subprocess.TimeoutExpired as error:
            raise ProcError(
                f"{self.name} did not exit in {deadline_s} s\n{self.output()}"
            ) from error

    def send(self, signum: signal.Signals) -> None:
        """One signal to the child alone, as `systemctl kill --kill-whom=main`."""
        self.popen.send_signal(signum)

    def output(self) -> str:
        """Both streams, each under a title, for a report or an error."""
        return (
            f"--- {self.name} stdout ---\n{_tail(self.stdout_path)}\n"
            f"--- {self.name} stderr ---\n{_tail(self.stderr_path)}"
        )


@dataclass(slots=True)
class Supervisor:
    """Every child of one test."""

    log_dir: Path
    children: list[Child] = field(default_factory=list[Child])
    _held_ports: dict[int, int] = field(default_factory=dict[int, int])

    def free_port(self) -> int:
        """A loopback port nothing holds now, held for this test until `stop_all`.

        A second run of this suite on the same machine cannot take the port
        between the check here and the bind in the child.
        """
        for _ in range(_PORT_ATTEMPTS):
            span = _PORT_CEILING - _PORT_FLOOR
            port = _PORT_FLOOR + int.from_bytes(os.urandom(2), "big") % span
            lock = hold_port(port)

            if lock is None:
                continue

            if port_is_free(port):
                self._held_ports[port] = lock

                return port

            os.close(lock)

        raise ProcError(f"no free loopback port in {_PORT_ATTEMPTS} attempts")

    def spawn(self, name: str, words: Sequence[str], env: Mapping[str, str], cwd: Path) -> Child:
        """Start one command in its own process group.

        `env` is the whole environment. Nothing of this process's own
        environment reaches the child unless the caller put it there.
        """
        self.log_dir.mkdir(parents=True, exist_ok=True)
        serial = len(self.children)
        stdout_path = self.log_dir / f"{serial:02d}-{name}.stdout"
        stderr_path = self.log_dir / f"{serial:02d}-{name}.stderr"

        with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
            try:
                popen = subprocess.Popen(
                    list(words),
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=err,
                    env=dict(env),
                    cwd=cwd,
                    start_new_session=True,
                )
            except OSError as error:
                raise ProcError(f"cannot start {name}: {error}") from error

        child = Child(name, tuple(words), popen, stdout_path, stderr_path)
        self.children.append(child)
        _note_group(child)

        return child

    def run(
        self,
        name: str,
        words: Sequence[str],
        env: Mapping[str, str],
        cwd: Path,
        deadline_s: float = RUN_DEADLINE_S,
    ) -> Finished:
        """Run one command to its end and return what it left."""
        child = self.spawn(name, words, env, cwd)
        exit_code = child.wait(deadline_s)

        return Finished(
            exit_code=exit_code,
            stdout=child.stdout_path.read_text(encoding="utf-8", errors="replace"),
            stderr=child.stderr_path.read_text(encoding="utf-8", errors="replace"),
        )

    def wait_ready(
        self, child: Child, address: Address, deadline_s: float = READY_DEADLINE_S
    ) -> None:
        """Return when `address` answers HTTP. Raise when the child cannot."""
        deadline = time.monotonic() + deadline_s

        while not answers_http(address):
            code = child.exit_code()

            if code is not None:
                raise ProcError(f"{child.name} exited {code} before it was ready\n{child.output()}")

            if time.monotonic() > deadline:
                raise ProcError(f"{child.name} was not ready in {deadline_s} s\n{child.output()}")

            time.sleep(_POLL_S)

    def stop_all(self, grace_s: float = STOP_GRACE_S) -> list[str]:
        """End every group, newest first. Returns one line per problem.

        SIGTERM goes to the whole group, as a unit's stop does with the
        default `KillMode=control-group`. A group that outlives the grace is
        killed, and that is a problem to report: on the host it would be a
        stop that ran into `TimeoutStopSec`.
        """
        problems: list[str] = []

        for child in reversed(self.children):
            _signal_group(child.pgid, signal.SIGTERM)

        deadline = time.monotonic() + grace_s

        for child in reversed(self.children):
            if not _exits_by(child, deadline):
                problems.append(f"{child.name} ignored SIGTERM for {grace_s} s and was killed")

        for child in reversed(self.children):
            _signal_group(child.pgid, signal.SIGKILL)
            child.popen.wait()

            if _group_gone_by(child.pgid, time.monotonic() + REAP_DEADLINE_S):
                _note_gone(child.pgid)
                continue

            problems.append(f"the process group of {child.name} outlived SIGKILL")

        for lock in self._held_ports.values():
            os.close(lock)

        self._held_ports.clear()

        return problems

    def output(self) -> str:
        """What every child wrote, for a failure report."""
        return "\n".join(child.output() for child in self.children)


def answers_http(address: Address) -> bool:
    """True when the address accepts a connection and answers one request.

    Any status counts. The probe carries no token, so a service that is up
    refuses it, and a refusal is an answer.
    """
    try:
        with _connect(address) as conn:
            conn.sendall(_PROBE_REQUEST)

            return conn.recv(len(_HTTP_ANSWER)) == _HTTP_ANSWER
    except OSError:
        return False


def is_listening(address: Address) -> bool:
    """True when something accepts a connection at the address."""
    try:
        with _connect(address):
            return True
    except OSError:
        return False


def hold_port(port: int) -> int | None:
    """Take the lock of one port. Returns the lock, or None when another holds it.

    The lock is an open file. Close it to give the port back.
    """
    _PORT_LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lock = os.open(_PORT_LOCK_DIR / str(port), os.O_CREAT | os.O_RDWR, _LOCK_FILE_MODE)

    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(lock)

        return None

    return lock


def port_is_free(port: int) -> bool:
    """True when this process can bind the loopback port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((LOOPBACK, port))
        except OSError:
            return False

    return True


def pid_is_alive(pid: int) -> bool:
    """True while the process table holds `pid`."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

    return True


def pids_gone_by(pids: Sequence[int], deadline: float) -> list[int]:
    """Wait for every pid to leave. Returns the ones that stayed."""
    while True:
        alive = [pid for pid in pids if pid_is_alive(pid)]

        if not alive or time.monotonic() > deadline:
            return alive

        time.sleep(_POLL_S)


def kill_pid(pid: int) -> None:
    """SIGKILL one process. A process that is gone already is not an error."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGKILL)


def _connect(address: Address) -> socket.socket:
    if isinstance(address, UnixAddress):
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        target: str | tuple[str, int] = str(address.path)
    else:
        conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        target = (address.host, address.port)

    conn.settimeout(_PROBE_TIMEOUT_S)

    try:
        conn.connect(target)
    except OSError:
        conn.close()
        raise

    return conn


def _exits_by(child: Child, deadline: float) -> bool:
    remaining = max(deadline - time.monotonic(), 0.0)

    try:
        child.popen.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        return False

    return True


def _signal_group(pgid: int, signum: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signum)


def _group_is_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

    return True


def _group_gone_by(pgid: int, deadline: float) -> bool:
    while _group_is_alive(pgid):
        if time.monotonic() > deadline:
            return False

        time.sleep(_POLL_S)

    return True


def _tail(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except OSError as error:
        return f"<cannot read {path}: {error.strerror}>"

    text = raw[-_TAIL_BYTES:].decode("utf-8", errors="replace")

    return text if text else "<empty>"


# ------------------------------------------------------------ the registry

#: Every child this pytest process started and no teardown confirmed gone, by
#: process group. A group leaves this map only in `stop_all`.
_open_groups: dict[int, Child] = {}


def _note_group(child: Child) -> None:
    _open_groups[child.pgid] = child


def _note_gone(pgid: int) -> None:
    _open_groups.pop(pgid, None)


def end_leaked_groups() -> list[str]:
    """Report every group no teardown ended. Returns one line per group.

    The check at session end. An empty list is the only passing answer.

    A group is killed only while its leader still runs as a child of this
    process: the system gives the pid of such a leader to no other program.
    A leader that ended earlier may have lost its pid to another program, so
    its group is reported and never signalled.
    """
    leaked: list[str] = []

    for pgid in sorted(_open_groups):
        child = _open_groups[pgid]

        if child.exit_code() is not None:
            leaked.append(f"{child.name} (process group {pgid}) was never stopped by its test")
            continue

        _signal_group(pgid, signal.SIGKILL)
        child.popen.wait()
        leaked.append(f"{child.name} (process group {pgid}) was still running")

    _open_groups.clear()

    return leaked
