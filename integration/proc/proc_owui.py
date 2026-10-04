"""The first topology: the Open WebUI door and `attendance`, as processes.

    a test (plays Open WebUI)
      | HTTP, loopback port
      v
    door-owui      one process, started through `Service.DOOR_OWUI`
      | HTTP over a Unix socket
      v
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

A test reaches the two services through sockets and through the files under
the root. It holds no object of either service.
"""

from __future__ import annotations

import os
import shlex
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
from proc_tree import DOOR_KEY, LAN_ADDRESS, Tree, build_tree, playpen_bundle, token_of

#: A Unix socket has no host. An HTTP client still needs one for the request
#: line, and nothing resolves this name.
_UDS_BASE_URL: Final = "http://attendance"

#: How many ports one door start may try. A port can be taken between the
#: check in this process and the bind in the child.
_BIND_ATTEMPTS: Final = 3

_CLIENT_TIMEOUT: Final = httpx.Timeout(5.0, read=60.0)

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


def door_env(tree: Tree, bind: str) -> dict[str, str]:
    """The variables `creche-door-owui.service` reads from its file."""
    return {
        "DOOR_OWUI_BIND": bind,
        "DOOR_OWUI_KEY_FILE": str(tree.door_key_file),
        "DOOR_OWUI_SESSIOND_TOKEN_FILE": str(tree.token_file("door-owui")),
        "DOOR_OWUI_SESSIOND_SOCKET": str(tree.attendance_socket),
        "DOOR_OWUI_FAMILIES_DIR": str(tree.families_dir),
    }


@dataclass(slots=True)
class OwuiStack:
    """The two services of the first topology, and what a test may touch."""

    tree: Tree
    supervisor: Supervisor
    door_port: int = 0
    attendance: Child | None = None
    door: Child | None = None
    _killed: set[int] = field(default_factory=set[int])

    # ------------------------------------------------------------------ start

    def prepare(self) -> None:
        """Write the tree and both stand-in programs. Start nothing."""
        build_tree(self.tree)
        install_sbx(self.tree)
        install_pi(self.tree)

    def start(self) -> None:
        """Start both services, then wait for both.

        The door needs nothing from `attendance` at start. It dials the
        socket at each request, so the two starts overlap.
        """
        self.attendance = self.spawn(Service.ATTENDANCE, attendance_env(self.tree))
        self.door = self._start_door()
        self.supervisor.wait_ready(self.attendance, UnixAddress(self.tree.attendance_socket))

    def spawn(self, service: Service, env: dict[str, str], *args: str) -> Child:
        """Start one service with the base environment under its own."""
        command = command_of(service)
        whole = base_env(self.tree) | env_of(command) | env

        return self.supervisor.spawn(service.value, [*command.words, *args], whole, self.tree.root)

    def _start_door(self) -> Child:
        """Start the door on a free loopback port.

        A start that fails because another program took the port first is
        tried again on another port. Any other failed start is an error.
        """
        for _ in range(_BIND_ATTEMPTS):
            port = free_port()
            child = self.spawn(Service.DOOR_OWUI, door_env(self.tree, f"{LOOPBACK}:{port}"))

            try:
                self.supervisor.wait_ready(child, TcpAddress(port))
            except ProcError:
                if child.exit_code() is None or port_is_free(port):
                    raise

                continue

            self.door_port = port

            return child

        raise ProcError(f"the door found no free port in {_BIND_ATTEMPTS} attempts")

    # ---------------------------------------------------------------- clients

    def door_client(self) -> httpx.AsyncClient:
        """A client that plays Open WebUI: the door's key, the door's port."""
        return httpx.AsyncClient(
            base_url=f"http://{LOOPBACK}:{self.door_port}",
            headers={"Authorization": f"Bearer {DOOR_KEY}"},
            timeout=_CLIENT_TIMEOUT,
        )

    def attendance_client(self, principal: str = "door-owui") -> httpx.AsyncClient:
        """A client to `attendance` that holds the token of one principal."""
        return httpx.AsyncClient(
            base_url=_UDS_BASE_URL,
            transport=httpx.AsyncHTTPTransport(uds=str(self.tree.attendance_socket)),
            headers={"Authorization": f"Bearer {token_of(principal)}"},
            timeout=_CLIENT_TIMEOUT,
        )

    # -------------------------------------------------- what crossed a boundary

    def pi_starts(self) -> list[Call]:
        """One entry per pi process the playpen started."""
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
