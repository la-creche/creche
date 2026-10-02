"""The session and turn state machines (contract 02 §4)."""

from __future__ import annotations

from agent_sessiond.states import (
    SessionState,
    TurnState,
    can_move,
    derive_session_state,
    is_terminal,
)


def test_legal_moves_match_the_contract() -> None:
    assert can_move(TurnState.QUEUED, TurnState.RUNNING)
    assert can_move(TurnState.QUEUED, TurnState.ABORTED)
    assert can_move(TurnState.RUNNING, TurnState.WAITING_APPROVAL)
    assert can_move(TurnState.RUNNING, TurnState.SETTLED)
    assert can_move(TurnState.WAITING_APPROVAL, TurnState.RUNNING)
    assert can_move(TurnState.WAITING_APPROVAL, TurnState.FAILED)


def test_illegal_moves_refuse() -> None:
    assert not can_move(TurnState.QUEUED, TurnState.SETTLED)
    assert not can_move(TurnState.QUEUED, TurnState.WAITING_APPROVAL)
    assert not can_move(TurnState.SETTLED, TurnState.RUNNING)
    assert not can_move(TurnState.FAILED, TurnState.RUNNING)
    assert not can_move(TurnState.ABORTED, TurnState.SETTLED)


def test_terminal_set() -> None:
    assert is_terminal(TurnState.SETTLED)
    assert is_terminal(TurnState.FAILED)
    assert is_terminal(TurnState.ABORTED)
    assert not is_terminal(TurnState.RUNNING)
    assert not is_terminal(TurnState.WAITING_APPROVAL)
    assert not is_terminal(TurnState.QUEUED)


def test_session_state_derivation_order() -> None:
    both = [TurnState.RUNNING, TurnState.WAITING_APPROVAL]
    assert derive_session_state(both, False) is SessionState.WAITING_APPROVAL

    assert derive_session_state([TurnState.RUNNING], False) is SessionState.RUNNING
    assert derive_session_state([TurnState.QUEUED], False) is SessionState.QUEUED

    # Sticky failed only shows when nothing is in flight.
    assert derive_session_state([], True) is SessionState.FAILED
    assert derive_session_state([TurnState.RUNNING], True) is SessionState.RUNNING
    assert derive_session_state([], False) is SessionState.IDLE
