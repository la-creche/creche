"""The picker: a numbered list and a number read back.

Plain terminal output, no curses dependency. A session's title, its last
activity, its state and which door made it, newest first.
"""

from __future__ import annotations

import pytest
from agent_door_tui.errors import DoorError, Exit
from agent_door_tui.picker import NEW_SESSION, Choice, ScriptedTerminal, pick_session
from agent_door_tui.sessiond import SessionState
from fake_tui_sessiond import row

ROWS = [
    row("tui-01JBQ7WZ0X4T9V6K2H8M3N5PQR", title="Boiler", updated_at="2026-09-19T10:04:11Z"),
    row(
        "owui-3f2a9c41-77b0-4a1e-9a4c-1d0e5f8b2c33",
        title="Kitchen sensor debug",
        state=SessionState.RUNNING,
        updated_at="2026-09-19T09:00:00Z",
    ),
]


def terminal(*answers: str) -> ScriptedTerminal:
    return ScriptedTerminal(list(answers))


def test_the_list_shows_title_activity_state_and_door() -> None:
    screen = terminal("1")

    pick_session(ROWS, screen)

    text = "\n".join(screen.shown)
    assert "Boiler" in text
    assert "2026-09-19T10:04:11Z" in text
    assert "running" in text
    assert "owui" in text and "tui" in text


def test_a_number_picks_that_session() -> None:
    choice = pick_session(ROWS, terminal("2"))

    assert choice == Choice(ROWS[1].session)


def test_the_first_row_is_the_newest() -> None:
    """Contract 02 §5.2 orders by `updated_at` descending. The door keeps it."""
    assert pick_session(ROWS, terminal("1")) == Choice(ROWS[0].session)


def test_zero_starts_a_new_session() -> None:
    assert pick_session(ROWS, terminal("0")) == NEW_SESSION


def test_an_untitled_session_still_reads() -> None:
    screen = terminal("1")

    pick_session([row("tui-01JB", title="")], screen)

    assert "(untitled)" in "\n".join(screen.shown)


def test_a_bad_number_asks_again() -> None:
    screen = terminal("nine", "7", "2")

    choice = pick_session(ROWS, screen)

    assert choice == Choice(ROWS[1].session)
    assert len(screen.asked) == 3


def test_an_empty_answer_chooses_nothing() -> None:
    with pytest.raises(DoorError) as caught:
        pick_session(ROWS, terminal(""))

    assert caught.value.code is Exit.NO_CHOICE


def test_end_of_input_chooses_nothing() -> None:
    """Ctrl-D at the prompt is an answer, not a crash."""
    with pytest.raises(DoorError) as caught:
        pick_session(ROWS, terminal())

    assert caught.value.code is Exit.NO_CHOICE


def test_an_empty_family_offers_only_a_new_session() -> None:
    screen = terminal("0")

    assert pick_session([], screen) == NEW_SESSION
    assert "no sessions yet" in "\n".join(screen.shown)


def test_a_title_is_cut_before_it_wraps() -> None:
    screen = terminal("1")

    pick_session([row("tui-01JB", title="x" * 300)], screen)

    assert max(len(line) for line in screen.shown) < 200


def test_a_title_cannot_forge_a_second_row() -> None:
    """Untrusted input: a title is a display string, never terminal control."""
    screen = terminal("1")

    pick_session([row("tui-01JB", title="ok\n 2) fake\x1b[31m")], screen)

    text = "\n".join(screen.shown)
    assert "fake" in text
    assert "\x1b" not in text
    assert sum(1 for line in screen.shown if line.strip().startswith("2)")) == 0
