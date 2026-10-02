"""The session and turn state machines (contract 02 §4).

One Enum per state set. Illegal moves refuse rather than correct themselves:
a turn that jumps a state is a bug somewhere, and silently allowing it would
hide the bug behind a plausible transcript.
"""

from __future__ import annotations

from enum import StrEnum


class SessionState(StrEnum):
    """Contract 02 §4.1. Derived, never stored as a decision."""

    IDLE = "idle"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting-approval"
    FAILED = "failed"


class TurnState(StrEnum):
    """Contract 02 §4.3."""

    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting-approval"
    SETTLED = "settled"
    FAILED = "failed"
    ABORTED = "aborted"


class SessionKind(StrEnum):
    """The family's kind, copied onto the session (contract 02 §4.2)."""

    ATTENDED = "attended"
    THIN = "thin"
    AUTONOMOUS = "autonomous"


TERMINAL_TURN_STATES: frozenset[TurnState] = frozenset(
    {TurnState.SETTLED, TurnState.FAILED, TurnState.ABORTED}
)

ACTIVE_TURN_STATES: frozenset[TurnState] = frozenset(
    {TurnState.RUNNING, TurnState.WAITING_APPROVAL}
)

# Contract 02 §7.3: a session holding one of these is ACTIVE, and no lease
# operation may disturb it. `queued` is in the set and not in
# `ACTIVE_TURN_STATES`, because a queued turn has its prompt on disk and a
# slot coming: handing its session to another door would strand it.
LEASE_BLOCKING_STATES: frozenset[TurnState] = ACTIVE_TURN_STATES | {TurnState.QUEUED}

# Contract 02 §4.3's "Legal moves" block, read as an adjacency table.
_LEGAL_MOVES: dict[TurnState, frozenset[TurnState]] = {
    TurnState.QUEUED: frozenset({TurnState.RUNNING, TurnState.ABORTED}),
    TurnState.RUNNING: frozenset(
        {
            TurnState.WAITING_APPROVAL,
            TurnState.SETTLED,
            TurnState.FAILED,
            TurnState.ABORTED,
        }
    ),
    TurnState.WAITING_APPROVAL: frozenset({TurnState.RUNNING, TurnState.FAILED, TurnState.ABORTED}),
    TurnState.SETTLED: frozenset(),
    TurnState.FAILED: frozenset(),
    TurnState.ABORTED: frozenset(),
}


class IllegalTransition(Exception):
    """A turn was asked to move somewhere contract 02 §4.3 does not allow."""

    def __init__(self, turn: str, current: TurnState, wanted: TurnState) -> None:
        super().__init__(f"turn {turn}: {current.value} cannot become {wanted.value}")
        self.turn = turn
        self.current = current
        self.wanted = wanted


def can_move(current: TurnState, wanted: TurnState) -> bool:
    """True when contract 02 §4.3 allows this move."""
    return wanted in _LEGAL_MOVES[current]


def is_terminal(state: TurnState) -> bool:
    """A turn in a terminal state never moves again."""
    return state in TERMINAL_TURN_STATES


def derive_session_state(
    turn_states: list[TurnState],
    last_settled_failed: bool,
) -> SessionState:
    """Contract 02 §4.1's derivation. The first match wins.

    `last_settled_failed` carries the sticky `failed` rule: the last turn that
    reached a terminal state did not settle, and no turn has started since.
    """
    if TurnState.WAITING_APPROVAL in turn_states:
        return SessionState.WAITING_APPROVAL

    if TurnState.RUNNING in turn_states:
        return SessionState.RUNNING

    if TurnState.QUEUED in turn_states:
        return SessionState.QUEUED

    if last_settled_failed:
        return SessionState.FAILED

    return SessionState.IDLE
