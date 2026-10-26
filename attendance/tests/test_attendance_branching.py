"""An Open WebUI edit or regenerate becomes a pi branch (contract 02 §10.2).

The four rules of `decide`.
"""

from __future__ import annotations

from attendance.branching import UNMAPPED_PARENT, Branch, decide

# Two settled turns, as §10.1 writes them: user then assistant, in order.
MAP = {"u1": "e-u1", "a1": "e-a1", "u2": "e-u2", "a2": "e-a2"}


def test_a_first_turn_just_prompts() -> None:
    """Rule 1. Nothing to attach to."""
    assert decide({}, None, 0).branch is Branch.PROMPT


def test_the_current_leaf_is_a_continuation() -> None:
    """Rule 2. The newest mapped entry is where pi already is."""
    assert decide(MAP, "a2", 2).branch is Branch.PROMPT


def test_an_older_parent_forks_at_the_next_user_entry() -> None:
    """Rule 3. pi forks from a previous USER message on the active branch."""
    found = decide(MAP, "a1", 2)

    assert found.branch is Branch.FORK
    assert found.fork_from == "e-u2"
    assert found.wire_branch == {"fork_from": "e-u2"}


def test_an_unmapped_parent_falls_back() -> None:
    """Rule 4. The turn runs and the divergence is written down."""
    found = decide(MAP, "a9", 2)

    assert found.branch is Branch.FALLBACK
    assert found.reason == UNMAPPED_PARENT
    assert found.wire_branch is None


def test_a_null_parent_on_a_used_session_falls_back() -> None:
    """§10.2 gives an edit of the first message no rule, so rule 4 takes it."""
    assert decide(MAP, None, 2).branch is Branch.FALLBACK
