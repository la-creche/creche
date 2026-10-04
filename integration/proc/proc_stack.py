"""What every topology of this suite shares: `attendance` and the stand-ins.

    attendance     one process, started through `Service.ATTENDANCE`
      | stdio, the channel protocol
      v
    sbx exec --env-file <env file> <sandbox> -- node playpen.js --sandbox <id>
      |                 the `sbx` stand-in program, found through PATH
      v
    node playpen/dist/playpen.js      the real bundle
      | stdio, pi rpc
      v
    the `pi` stand-in program         found through AGENT_PI_BIN

A topology adds the service in front: a door in `proc_owui.py`, the chaperone
in `proc_delegate.py`. A test reaches each service through a socket and
through the files under the root. It holds no object of a service.
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final

import httpx
from proc_harness import (
    LOOPBACK,
    Child,
    ProcError,
    Supervisor,
    TcpAddress,
    UnixAddress,
    free_port,
    kill_pid,
    port_is_free,
)
from proc_services import Service, command_of, env_of
from proc_standins import (
    LOCK_POLL_S,
    LOCK_STALE_S,
    PI,
    SBX,
    Call,
    calls_of,
    install_pi,
    install_sbx,
)
from proc_tree import LAN_ADDRESS, Tree, build_tree, playpen_bundle, token_of

#: A Unix socket has no host. An HTTP client still needs one for the request
#: line, and nothing resolves this name.
_UDS_BASE_URL: Final = "http://attendance"

#: How many ports one start may try. A port can be taken between the check
#: in this process and the bind in the child.
_BIND_ATTEMPTS: Final = 3

CLIENT_TIMEOUT: Final = httpx.Timeout(5.0, read=60.0)

#: What a unit gives every service: a PATH, a home, and nothing of the
#: operator's shell. `LANG` and `TMPDIR` pass through when this process has
#: them, because `node` and the stand-ins read them.
_PASSED_THROUGH: Final = ("LANG", "TMPDIR")


def base_env(tree: Tree) -> dict[str, str]:
    """The environment every service starts from.

    The root's `bin` is first on PATH, so a service that runs `sbx` finds the
    stand-in. No variable of a service and no variable of a stand-in is here.
    """
    env = {
        "PATH": f"{tree.bin_dir}{os.pathsep}{os.environ.get('PATH', os.defpath)}",
        "HOME": str(tree.home),
        "XDG_CONFIG_HOME": str(tree.home / ".config"),
    }

    for name in _PASSED_THROUGH:
        if name in os.environ:
            env[name] = os.environ[name]

    return env


def attendance_env(tree: Tree) -> dict[str, str]:
    """The variables `creche-attendance.service` reads from its two files.

    The channel command keeps the shape of the default: the same two
    template fields, in the same order, with the `--sandbox` flag. Only the
    path of the bundle differs, because no sandbox image exists here.
    """
    playpen = f"node {shlex.quote(str(playpen_bundle()))} --sandbox {{sandbox}}"
    command = f"{SBX} exec --env-file {{env_file}} {{sandbox}} -- {playpen}"

    return {
        "AGENT_LAN_ADDRESS": LAN_ADDRESS,
        "SESSIOND_SESSIONS_ROOT": str(tree.sessions_root),
        "SESSIOND_STATE_ROOT": str(tree.state_root),
        "SESSIOND_WORK_ROOT": str(tree.work_root),
        "SESSIOND_SOCKET": str(tree.attendance_socket),
        "SESSIOND_LOG_DIR": str(tree.log_dir),
        "SESSIOND_CHANNEL_COMMAND": command,
        "SESSIOND_LOCK_STALE_S": str(LOCK_STALE_S),
        "SESSIOND_LOCK_POLL_S": str(LOCK_POLL_S),
    }


@dataclass(slots=True)
class Stack:
    """`attendance`, the stand-ins, and what a test may touch of them."""

    tree: Tree
    supervisor: Supervisor
    attendance: Child | None = None
    _killed: set[int] = field(default_factory=set[int])

    # ------------------------------------------------------------------ start

    def prepare(self) -> None:
        """Write the tree and both stand-in programs. Start nothing."""
        build_tree(self.tree)
        install_sbx(self.tree)
        install_pi(self.tree)

    def spawn(self, service: Service, env: dict[str, str], *args: str) -> Child:
        """Start one service with the base environment under its own."""
        command = command_of(service)
        whole = base_env(self.tree) | env_of(command) | env

        return self.supervisor.spawn(service.value, [*command.words, *args], whole, self.tree.root)

    def spawn_attendance(self) -> Child:
        """Start `attendance`. `await_attendance` waits for it."""
        self.attendance = self.spawn(Service.ATTENDANCE, attendance_env(self.tree))

        return self.attendance

    def await_attendance(self) -> None:
        if self.attendance is None:
            raise ProcError("attendance was not started")

        self.supervisor.wait_ready(self.attendance, UnixAddress(self.tree.attendance_socket))

    def start_on_port(
        self, service: Service, env_for: Callable[[str], dict[str, str]]
    ) -> tuple[Child, int]:
        """Start a service on a free loopback port, and wait for it.

        `env_for` takes the bind, as `host:port`, and returns the variables
        of the service. A start that fails because another program took the
        port first is tried again on another port. Any other failed start is
        an error.
        """
        for _ in range(_BIND_ATTEMPTS):
            port = free_port()
            child = self.spawn(service, env_for(f"{LOOPBACK}:{port}"))

            try:
                self.supervisor.wait_ready(child, TcpAddress(port))
            except ProcError:
                if child.exit_code() is None or port_is_free(port):
                    raise

                continue

            return child, port

        raise ProcError(f"{service.value} found no free port in {_BIND_ATTEMPTS} attempts")

    # ---------------------------------------------------------------- clients

    def attendance_client(self, principal: str = "door-owui") -> httpx.AsyncClient:
        """A client to `attendance` that holds the token of one principal."""
        return httpx.AsyncClient(
            base_url=_UDS_BASE_URL,
            transport=httpx.AsyncHTTPTransport(uds=str(self.tree.attendance_socket)),
            headers={"Authorization": f"Bearer {token_of(principal)}"},
            timeout=CLIENT_TIMEOUT,
        )

    # -------------------------------------------------- what crossed a boundary

    def pi_starts(self) -> list[Call]:
        """One entry per pi process a playpen started."""
        return calls_of(self.tree, PI)

    def sbx_calls(self) -> list[Call]:
        """One entry per `sbx` command a service ran."""
        return calls_of(self.tree, SBX)

    def kill_playpens(self) -> list[int]:
        """SIGKILL every playpen that runs now. Returns the pids.

        `sbx exec` becomes the playpen, so the pid the stand-in recorded is
        the pid of the playpen.
        """
        pids = [call.pid for call in self.sbx_calls() if call.pid not in self._killed]

        for pid in pids:
            kill_pid(pid)
            self._killed.add(pid)

        return pids
