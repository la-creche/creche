"""The fifth topology: the terminal door, on a pseudo-terminal.

    a test (plays the operator at a keyboard)
      | a pseudo-terminal: the test holds the master side
      v
    door-tui       one program, started through `Service.DOOR_TUI`.
      |            The terminal is its controlling terminal.
      | HTTP over a Unix socket, the `door-tui` token:
      |   find or create, the writer lease, release the pi process
      v
    attendance and the stand-ins      `proc_stack.py`

    door-tui, after it holds the lease:
      | sbx exec -it --env-file <env file> <sandbox> -- node <launcher> ...
      v                 the `sbx` stand-in, found through PATH
    node playpen/dist/agent-pi-launch.js      the real launcher bundle
      | the terminal, with no pipe between
      v
    the `pi` stand-in      found through AGENT_PI_BIN. It reads the terminal
                           to the end of the input, as a person ends pi.

The door is a program that the operator types. It has no unit, so its
environment is the one of a shell: the `DOOR_TUI_` variables and nothing of
a service.

The Open WebUI door of the first topology runs beside it. One session has
two doors (invariant 3), and a scenario needs the first door to make the
session that the terminal then takes.

The pi stand-in has no interactive mode. On a terminal it reads lines as it
reads its pipe, and it ends at the end of the input. So a test ends pi with
the EOF key. Nothing here is a statement about the screen of the real pi.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from proc_chat import SESSIONS_PATH, await_settled, chat_id, run_stream, session_of
from proc_harness import Child, ProcError
from proc_owui import OwuiStack
from proc_services import Service
from proc_standins import PI, SBX, TERMINAL_FLAG, Call, calls_of
from proc_terminal import Terminal
from proc_tree import FAMILY, Tree, repo_root

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


def launch_bundle() -> Path:
    """The built launcher. `pnpm run build` in `playpen/` writes it."""
    return repo_root() / "playpen" / "dist" / "agent-pi-launch.js"


def tui_env(tree: Tree) -> dict[str, str]:
    """The variables of `agent-tui`, as the shell of the operator holds them.

    The launcher path is the one difference from the host. There it is a
    path in the sandbox image. Here no image exists, so it is the bundle.
    `sbx` comes from PATH, as on the host.
    """
    return {
        "DOOR_TUI_SESSIOND_TOKEN_FILE": str(tree.token_file("door-tui")),
        "DOOR_TUI_SESSIOND_SOCKET": str(tree.attendance_socket),
        "DOOR_TUI_FAMILIES_DIR": str(tree.families_dir),
        "DOOR_TUI_PI_LAUNCH": str(launch_bundle()),
    }


def instance_of(child: Child) -> str:
    """The name of one terminal in a lease: `tui.<pid>` (contract 02 §7.1)."""
    return f"{HOLDER}.{child.popen.pid}"


@dataclass(slots=True)
class TuiStack(OwuiStack):
    """The terminal door beside the Open WebUI door and `attendance`."""

    def open_terminal(self, *args: str) -> tuple[Child, Terminal]:
        """Type `agent-tui` with these words at a new terminal."""
        return self.spawn_on_terminal(Service.DOOR_TUI, tui_env(self.tree), *args)

    async def chat_session(self) -> str:
        """One session of the Open WebUI door with one settled turn.

        The playpen holds the pi process of the session after the turn
        (contract 03 §6), so the terminal door has a process to release.
        """
        chat = chat_id()

        async with self.door_client() as door:
            frames = await run_stream(door, chat, "hello")

        if frames.error_chunks:
            raise ProcError(f"the turn of the chat failed: {frames.error_chunks}")

        session = session_of(chat)
        await await_settled(self.tree, session)

        return session

    def terminal_calls(self) -> list[Call]:
        """Each `sbx exec -it` the door ran. `attendance` runs `sbx` with no terminal."""
        return [call for call in calls_of(self.tree, SBX) if TERMINAL_FLAG in call.argv]

    def terminal_pi_starts(self) -> list[Call]:
        """Each pi process that a launcher started. The playpen starts pi in rpc mode."""
        return [call for call in calls_of(self.tree, PI) if "--mode" not in call.argv]

    async def session(self, session: str, family: str = FAMILY) -> dict[str, Any] | None:
        """One session as the noticeboard reads it, or None when `attendance` has none."""
        async with self.attendance_client("view-ro") as view:
            response = await view.get(f"{SESSIONS_PATH}/{family}/{session}")

        if response.status_code != _HTTP_OK:
            return None

        body: dict[str, Any] = response.json()

        return body

    async def writer(self, session: str, family: str = FAMILY) -> dict[str, Any] | None:
        """The writer lease of one session (contract 02 §7.1), or None when no door holds it."""
        found = await self.session(session, family)

        if found is None:
            return None

        lease: dict[str, Any] | None = found.get("writer")

        return lease

    def lease_changes(self, session: str, family: str = FAMILY) -> list[tuple[str, str]]:
        """Each lease change in the journal of one session: the holder and the reason."""
        lines = self.tree.journal_lines(session, family)

        return [
            (str(line["body"].get("holder")), str(line["body"].get("reason")))
            for line in lines
            if line.get("kind") == WRITER_CHANGED
        ]


_HTTP_OK: Final = 200
