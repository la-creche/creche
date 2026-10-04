"""What the terminal door refuses before it gives a terminal to pi.

Each scenario types `agent-tui` at a pseudo-terminal, as the operator does,
and reads three boundaries: the exit code, the record of the `sbx` stand-in
and the journal of `attendance`. A refusal runs no `sbx` and moves no lease.

CONTRACT-QUESTION: no contract names the exit codes of the door itself.
Contract 03 §7.6 names the codes of the launcher, 0 to 11, and the door
passes those through (`test_proc_tui_terminal.py`). Reading taken for a
refusal of the door: any code that is not 0. A change to fixed codes costs
one assertion per scenario here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from proc_tree import (
    FAMILY,
    SANDBOX,
    THIN,
    add_family,
    replace_secret,
    token_of,
    write_status,
)
from proc_tui import ARG_NEW, ARG_SESSION, TuiStack

CHECK_FLAG = "--check"
EXIT_DEADLINE_S = 30.0

#: Under the 32-byte floor of contract 02 §3 rule 7. Its text is one a test
#: can find on the terminal.
SHORT_SECRET = "short-tui-token"

ORACLE = "vault-oracle"
NO_FAMILY = "no-such-family"
NO_NAME = "Not_A_Family"


def test_the_check_validates_and_touches_nothing(tui_prepared: TuiStack) -> None:
    """`--check` reads the config and exits 0. No `attendance` and no `sbx` runs."""
    child, terminal = tui_prepared.open_terminal(CHECK_FLAG)

    assert child.wait(EXIT_DEADLINE_S) == 0, child.output()
    assert tui_prepared.terminal_calls() == []
    # Invariant 13: no secret in a message.
    assert token_of("door-tui") not in terminal.text()


@pytest.mark.parametrize("content", [None, "", SHORT_SECRET], ids=["missing", "empty", "short"])
def test_a_bad_token_file_refuses_to_start(tui_prepared: TuiStack, content: str | None) -> None:
    """Contract 02 §3 rule 7, as the door applies it to its own token. Fail closed."""
    replace_secret(tui_prepared.tree.token_file("door-tui"), content)

    child, terminal = tui_prepared.open_terminal(FAMILY, ARG_NEW)

    assert child.wait(EXIT_DEADLINE_S) != 0
    assert tui_prepared.terminal_calls() == []
    assert SHORT_SECRET not in terminal.text()


@pytest.mark.parametrize("words", [[], [FAMILY, ARG_SESSION, "owui-x", ARG_NEW]])
def test_a_command_that_names_no_one_session_is_a_usage_error(
    tui_prepared: TuiStack, words: list[str]
) -> None:
    """No family, or `--session` and `--new` at once."""
    child, _ = tui_prepared.open_terminal(*words)

    assert child.wait(EXIT_DEADLINE_S) != 0
    assert tui_prepared.terminal_calls() == []


def test_attendance_down_is_a_refusal(tui_prepared: TuiStack, launcher: Path) -> None:
    """The socket does not answer. The door runs no `sbx`."""
    child, _ = tui_prepared.open_terminal(FAMILY, ARG_NEW)

    assert child.wait(EXIT_DEADLINE_S) != 0
    assert tui_prepared.terminal_calls() == []


@pytest.mark.parametrize("family", [ORACLE, NO_FAMILY, NO_NAME], ids=["thin", "unknown", "no-name"])
def test_a_terminal_reaches_an_attended_family_only(tui: TuiStack, family: str) -> None:
    """Contract 02 §3.1: the `door-tui` token reaches an attended family."""
    tree = tui.tree
    add_family(tree, ORACLE, THIN)

    child, _ = tui.open_terminal(family, ARG_NEW)

    assert child.wait(EXIT_DEADLINE_S) != 0
    assert tui.terminal_calls() == []
    assert not (tree.sessions_root / family).is_dir() or tree.sessions_of(family) == []


async def test_a_family_with_no_ready_sandbox_is_refused(tui: TuiStack) -> None:
    """A terminal runs no handshake, so only a ready sandbox serves one (contract 05 §4.2)."""
    session = await tui.chat_session()
    write_status(tui.tree, sandboxes=((SANDBOX, "creating"),))

    child, _ = tui.open_terminal(FAMILY, ARG_SESSION, session)

    assert child.wait(EXIT_DEADLINE_S) != 0
    assert tui.terminal_calls() == []
    assert tui.lease_changes(session) == [("owui", "granted")]


async def test_a_session_id_that_is_no_id_is_refused(tui: TuiStack) -> None:
    """Contract 02 §2 gives the form of a session id. The door checks it first."""
    child, _ = tui.open_terminal(FAMILY, ARG_SESSION, "not an id")

    assert child.wait(EXIT_DEADLINE_S) != 0
    assert tui.terminal_calls() == []
