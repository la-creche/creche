"""The sixth topology: the terminal door, on a pseudo-terminal.

    a test (plays the operator at a keyboard)
      | a pseudo-terminal: the test holds the master side
      v
    door-tui       one program, started through `Service.DOOR_TUI`.
      |            The terminal is its controlling terminal.
      | HTTP over a Unix socket, the `door-tui` token:
      |   find or create, the writer lease, release the pi process
      v
    attendance and the stand-ins      `proc_stack.py`

Each scenario is a refusal: the door ends before it runs
`sbx exec -it`. The image holds no launcher, so no scenario gives a
terminal to pi.

The door is a program that the operator types. It has no unit, so its
environment is the one of a shell: the `DOOR_TUI_` variables and nothing of
a service.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from proc_harness import Child
from proc_services import Service
from proc_stack import Stack
from proc_standins import SBX, TERMINAL_FLAG, Call, calls_of
from proc_terminal import Terminal
from proc_tree import Tree

#: The name of the door in a lease (contract 02 §7.1).
HOLDER: Final = "tui"

#: The prefix of a session that this door makes (contract 02 §2).
TUI_PREFIX: Final = "tui-"

#: The journal line of a lease change, and its reasons (contract 02 §7.3).
WRITER_CHANGED: Final = "writer_changed"
GRANTED: Final = "granted"
TAKEN_OVER: Final = "taken_over"
RELEASED: Final = "released"

#: The words of the launcher (contract 03 §7.6).
ARG_SESSION: Final = "--session"
ARG_SANDBOX: Final = "--sandbox"
ARG_NEW: Final = "--new"


def tui_env(tree: Tree) -> dict[str, str]:
    """The variables of `agent-tui`, as the shell of the operator holds them.

    `sbx` comes from PATH, as on the host.
    """
    return {
        "DOOR_TUI_SESSIOND_TOKEN_FILE": str(tree.token_file("door-tui")),
        "DOOR_TUI_SESSIOND_SOCKET": str(tree.attendance_socket),
        "DOOR_TUI_FAMILIES_DIR": str(tree.families_dir),
    }


def instance_of(child: Child) -> str:
    """The name of one terminal in a lease: `tui.<pid>` (contract 02 §7.1)."""
    return f"{HOLDER}.{child.popen.pid}"


@dataclass(slots=True)
class TuiStack(Stack):
    """The terminal door beside `attendance`."""

    def start(self) -> None:
        """Start `attendance`, then wait for it."""
        self.spawn_attendance()
        self.await_attendance()

    def open_terminal(self, *args: str) -> tuple[Child, Terminal]:
        """Type `agent-tui` with these words at a new terminal."""
        return self.spawn_on_terminal(Service.DOOR_TUI, tui_env(self.tree), *args)

    def terminal_calls(self) -> list[Call]:
        """Each `sbx exec -it` the door ran. `attendance` runs `sbx` with no terminal."""
        return [call for call in calls_of(self.tree, SBX) if TERMINAL_FLAG in call.argv]
