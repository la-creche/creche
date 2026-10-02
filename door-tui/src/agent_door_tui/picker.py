"""The picker: a numbered list, and a number read back.

Plain terminal output. No curses dependency, because the only interactive
program here is pi, and this list exists for the three seconds before pi
owns the terminal.

A session's title comes from a door, which took it from a person or from an
Open WebUI chat, so it is untrusted input. It is cut to a display width and
stripped of control characters before it is printed: a title carrying an LF
and an escape sequence could otherwise paint a row that is not in the list
and lead the operator to open a session they did not choose.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .errors import DoorError, Exit
from .ids import door_of
from .sessiond import SessionRow

#: How much of a title a row shows before it is cut.
TITLE_WIDTH = 48
#: How many sessions the picker lists. Past this, `--session` is the way in.
MAX_ROWS = 40
#: How many times a bad answer is read again before the door gives up.
MAX_TRIES = 3

_UNTITLED = "(untitled)"
_NEW_ROW = "0"


@dataclass(frozen=True)
class Choice:
    """What the picker chose. An empty session means "start a new one"."""

    session: str


#: The answer that starts a new session.
NEW_SESSION = Choice("")


class Terminal(Protocol):
    """The two things the picker does to a terminal."""

    def show(self, line: str) -> None:
        """Print one line."""
        ...

    def ask(self, prompt: str) -> str:
        """Read one answer. Raise `EOFError` at end of input."""
        ...


class RealTerminal:
    """stdout and stdin, which is every terminal the door ever meets."""

    def show(self, line: str) -> None:
        print(line)

    def ask(self, prompt: str) -> str:
        return input(prompt)


class ScriptedTerminal:
    """A terminal a test drives. It records what was shown and asked."""

    def __init__(self, answers: list[str]) -> None:
        self._answers = answers
        self.shown: list[str] = []
        self.asked: list[str] = []

    def show(self, line: str) -> None:
        self.shown.append(line)

    def ask(self, prompt: str) -> str:
        self.asked.append(prompt)

        if not self._answers:
            raise EOFError

        return self._answers.pop(0)


def pick_session(rows: list[SessionRow], terminal: Terminal) -> Choice:
    """Show the family's sessions and read one number back.

    `rows` arrives newest first: contract 02 §5.2 orders a listing by
    `updated_at` descending, and the door keeps that order.
    """
    shown = rows[:MAX_ROWS]
    _print_rows(shown, terminal)

    for _ in range(MAX_TRIES):
        answer = _ask(terminal)

        if answer == _NEW_ROW:
            return NEW_SESSION

        chosen = _chosen(answer, shown)

        if chosen is not None:
            return chosen

        terminal.show(f"Type a number from 0 to {len(shown)}.")

    raise DoorError(Exit.NO_CHOICE, "no session was chosen.")


def _print_rows(rows: list[SessionRow], terminal: Terminal) -> None:
    terminal.show("")

    if not rows:
        terminal.show("  no sessions yet in this family.")
    for number, row in enumerate(rows, start=1):
        terminal.show(f"  {number:>2}) {_line(row)}")

    terminal.show(f"  {_NEW_ROW:>2}) start a new session")
    terminal.show("")


def _line(row: SessionRow) -> str:
    """One row: title, last activity, state, and which door made it."""
    title = _safe(row.title) or _UNTITLED
    door = door_of(row.session) or "?"

    return f"{title:<{TITLE_WIDTH}}  {row.updated_at}  {row.state.value:<16}  {door}"


def _safe(title: str) -> str:
    """A display string: printable, one line, and bounded.

    A title reaches this door from Open WebUI or from another operator. An
    escape sequence or a line feed in it could paint a row that does not
    exist, so neither survives the trip to the screen (invariant 14).
    """
    printable = "".join(one if one.isprintable() else " " for one in title).strip()

    if len(printable) <= TITLE_WIDTH:
        return printable

    return printable[: TITLE_WIDTH - 1] + "…"


def _ask(terminal: Terminal) -> str:
    try:
        return terminal.ask("Session number (0 for a new one): ").strip()
    except EOFError as exc:
        # Ctrl-D at the prompt is an answer, not a crash.
        raise DoorError(Exit.NO_CHOICE, "no session was chosen.") from exc


def _chosen(answer: str, rows: list[SessionRow]) -> Choice | None:
    if not answer:
        raise DoorError(Exit.NO_CHOICE, "no session was chosen.")

    if not answer.isdigit():
        return None

    number = int(answer)

    if not 1 <= number <= len(rows):
        return None

    return Choice(rows[number - 1].session)
