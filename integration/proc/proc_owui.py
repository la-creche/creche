"""The first topology: the Open WebUI door in front of `attendance`.

a test (plays Open WebUI)
  | HTTP, loopback port, the door's key
  v
door-owui      one process, started through `Service.DOOR_OWUI`
  | HTTP over a Unix socket
  v
attendance and the stand-ins      `proc_stack.py`
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from proc_harness import LOOPBACK, Child
from proc_services import Service
from proc_stack import CLIENT_TIMEOUT, Stack
from proc_tree import DOOR_KEY, Tree


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
class OwuiStack(Stack):
    """The door and `attendance`, serving one attended family."""

    door: Child | None = None
    door_port: int = 0

    def start(self) -> None:
        """Start both services, then wait for both.

        The door needs nothing from `attendance` at start. It dials the
        socket at each request, so the two starts overlap.
        """
        self.spawn_attendance()
        self.door, self.door_port = self.start_on_port(
            Service.DOOR_OWUI, lambda bind: door_env(self.tree, bind)
        )
        self.await_attendance()

    def door_client(self) -> httpx.AsyncClient:
        """A client that plays Open WebUI: the door's key, the door's port."""
        return httpx.AsyncClient(
            base_url=f"http://{LOOPBACK}:{self.door_port}",
            headers={"Authorization": f"Bearer {DOOR_KEY}"},
            timeout=CLIENT_TIMEOUT,
        )
