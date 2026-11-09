"""What every topology of this suite shares: `attendance` and the vmm stand-in.

    attendance     one process, started through `Service.ATTENDANCE`
      | POST /create, /list, /drop, /acp over vmm.sock
      v
    the vmm stand-in    `standin_vmm.py`, which boots no VM

A topology adds the service in front: the trigger door in `proc_trigger.py`.
A test reaches each service through a socket and through the files under the
root. It holds no object of a service.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

import httpx
from proc_harness import (
    LOOPBACK,
    Child,
    Finished,
    ProcError,
    Supervisor,
    TcpAddress,
    UnixAddress,
    port_is_free,
)
from proc_services import Service, command_of, env_of
from proc_standins import start_vmm
from proc_tree import LAN_ADDRESS, Tree, build_tree, token_of

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

    The root's `bin` is first on PATH, so a service that runs a program finds
    its stand-in. No variable of a service and no variable of a stand-in is
    here.
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
    """The variables `creche-attendance.service` reads from its two files,
    and the one that it sets itself.

    The unit sets `ATTENDANCE_VMM_SOCKET` from `%t` (decision CH20). Here
    it names the socket of the vmm stand-in.
    """
    return {
        "AGENT_LAN_ADDRESS": LAN_ADDRESS,
        "ATTENDANCE_SESSIONS_ROOT": str(tree.sessions_root),
        "ATTENDANCE_STATE_ROOT": str(tree.state_root),
        "ATTENDANCE_SOCKET": str(tree.attendance_socket),
        "ATTENDANCE_VMM_SOCKET": str(tree.vmm_socket),
    }


@dataclass(slots=True)
class Stack:
    """`attendance`, the vmm stand-in, and what a test may touch of them."""

    tree: Tree
    supervisor: Supervisor
    attendance: Child | None = None
    vmm: Child | None = None

    # ------------------------------------------------------------------ start

    def prepare(self) -> None:
        """Write the tree. Start nothing."""
        build_tree(self.tree)

    def spawn(self, service: Service, env: dict[str, str], *args: str) -> Child:
        """Start one service with the base environment under its own."""
        command = command_of(service)
        whole = base_env(self.tree) | env_of(command) | env

        return self.supervisor.spawn(service.value, [*command.words, *args], whole, self.tree.root)

    def run(self, service: Service, env: dict[str, str], *args: str) -> Finished:
        """Run one command of a service to its end, with the base environment under its own."""
        command = command_of(service)
        whole = base_env(self.tree) | env_of(command) | env

        return self.supervisor.run(service.value, [*command.words, *args], whole, self.tree.root)

    def spawn_attendance(self) -> Child:
        """Start `attendance`, after the vmm stand-in on the first start.
        `await_attendance` waits for `attendance`."""
        if self.vmm is None:
            self.vmm = start_vmm(self.tree, self.supervisor, base_env(self.tree))

        self.attendance = self.spawn(Service.ATTENDANCE, attendance_env(self.tree))

        return self.attendance

    def await_attendance(self) -> None:
        if self.attendance is None:
            raise ProcError("attendance was not started")

        self.supervisor.wait_ready(self.attendance, UnixAddress(self.tree.attendance_socket))

    def start_on_port(
        self,
        service: Service,
        env_for: Callable[[str], dict[str, str]],
        *args: str,
        args_for: Callable[[str], list[str]] | None = None,
    ) -> tuple[Child, int]:
        """Start a service on a free loopback port, and wait for it.

        `env_for` takes the bind, as `host:port`, and returns the variables
        of the service. `args` are the words that the unit puts after the
        command. `args_for` takes the bind too, and returns more words, for
        a service that takes its address in an argument. A start that fails
        because another program took the port first is tried again on
        another port. Any other failed start is an error.
        """
        for _ in range(_BIND_ATTEMPTS):
            port = self.supervisor.free_port()
            bind = f"{LOOPBACK}:{port}"
            words = [*args, *(args_for(bind) if args_for is not None else [])]
            child = self.spawn(service, env_for(bind), *words)

            try:
                self.supervisor.wait_ready(child, TcpAddress(port))
            except ProcError:
                if child.exit_code() is None or port_is_free(port):
                    raise

                continue

            return child, port

        raise ProcError(f"{service.value} found no free port in {_BIND_ATTEMPTS} attempts")

    # ---------------------------------------------------------------- clients

    def attendance_client(self, principal: str = "door-trigger") -> httpx.AsyncClient:
        """A client to `attendance` that holds the token of one principal."""
        return httpx.AsyncClient(
            base_url=_UDS_BASE_URL,
            transport=httpx.AsyncHTTPTransport(uds=str(self.tree.attendance_socket)),
            headers={"Authorization": f"Bearer {token_of(principal)}"},
            timeout=CLIENT_TIMEOUT,
        )
