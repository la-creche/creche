"""Live turns: the index, the idempotency key and the settle signal.

A turn record on disk is the durable fact. This module holds what only a
running service can hold: which turn a key already started, who is waiting
for it to settle, and the text the turn has produced so far.

Nothing here reaches disk or the channel. It is the layer the service asks
"what is happening", and it answers from memory alone.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from .atomic import as_object
from .clock import now
from .models import Turn
from .states import (
    ACTIVE_TURN_STATES,
    LEASE_BLOCKING_STATES,
    SessionState,
    TurnState,
    derive_session_state,
)

# Contract 02 §6 rule 2. A choice, not a measurement (§16 rule 5).
IDEMPOTENCY_WINDOW_S = 86_400

_TEXT_DELTA = "text_delta"
_ASSISTANT_EVENT = "assistantMessageEvent"
_DELTA = "delta"


@dataclass(slots=True)
class LiveTurn:
    """One turn this process started, with what only memory can hold."""

    record: Turn
    prompt: str
    first_seq: int = 0
    text: list[str] = field(default_factory=list[str])
    done: asyncio.Event = field(default_factory=asyncio.Event)
    deadline_task: asyncio.Task[None] | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.record.family, self.record.session, self.record.turn)

    @property
    def is_active(self) -> bool:
        return self.record.state in ACTIVE_TURN_STATES

    def answer(self) -> str:
        """Every assistant delta of this turn, joined."""
        return "".join(self.text)


def text_delta(event: dict[str, Any]) -> str | None:
    """The assistant text a pi event carries, or None.

    This is the one thing the host reads out of a pi event, and it reads it
    to record an answer, never to act on it (contract 03 §13 rule 5). The
    `wait=settled` body needs the text, and a thin job's journal dies with
    its session, so the text has to travel in the response.
    """
    inner = as_object(event.get(_ASSISTANT_EVENT))

    if inner is None or inner.get("type") != _TEXT_DELTA:
        return None

    delta = inner.get(_DELTA)
    return delta if isinstance(delta, str) else None


class TurnBook:
    """Every live turn, indexed the three ways the service asks for one."""

    def __init__(self, idempotency_window_s: int = IDEMPOTENCY_WINDOW_S) -> None:
        self._window = timedelta(seconds=idempotency_window_s)
        self._turns: dict[tuple[str, str, str], LiveTurn] = {}
        self._keys: dict[tuple[str, str, str], str] = {}

    def add(self, live: LiveTurn) -> None:
        self._turns[live.key] = live
        key = live.record.idempotency_key

        if key is not None:
            self._keys[(live.record.family, live.record.session, key)] = live.record.turn

    def get(self, family: str, session: str, turn: str) -> LiveTurn | None:
        return self._turns.get((family, session, turn))

    def by_key(self, family: str, session: str, key: str) -> LiveTurn | None:
        """The turn an idempotency key already started, inside the window."""
        turn = self._keys.get((family, session, key))

        if turn is None:
            return None

        live = self._turns.get((family, session, turn))

        if live is None:
            return None

        if now() - live.record.started_at > self._window:
            del self._keys[(family, session, key)]
            return None

        return live

    def all_turns(self) -> list[LiveTurn]:
        """Every live turn. Shutdown walks this to cancel its timers."""
        return list(self._turns.values())

    def of_session(self, family: str, session: str) -> list[LiveTurn]:
        """This session's turns, oldest first. Turn ids sort by start time."""
        found = [live for live in self._turns.values() if _in_session(live, family, session)]
        return sorted(found, key=lambda live: live.record.turn)

    def active_in_family(self, family: str) -> list[LiveTurn]:
        """Turns still running anywhere in one family (contract 03 §11.4)."""
        return [
            live for live in self._turns.values() if live.is_active and _in_family(live, family)
        ]

    def active_in_session(self, family: str, session: str) -> list[LiveTurn]:
        return [live for live in self.of_session(family, session) if live.is_active]

    def blocking_in_session(self, family: str, session: str) -> list[LiveTurn]:
        """Turns that make a session ACTIVE for the lease (contract 02 §7.3).

        Wider than `active_in_session` by one state: a `queued` turn counts,
        because a slot is coming for it and another door would strand it.
        """
        found = self.of_session(family, session)
        return [live for live in found if live.record.state in LEASE_BLOCKING_STATES]

    def drop_session(self, family: str, session: str) -> None:
        """Forget one session's turns. A delete calls this last."""
        for live in self.of_session(family, session):
            self._turns.pop(live.key, None)

        for key in [entry for entry in self._keys if entry[0] == family and entry[1] == session]:
            del self._keys[key]

    def session_state(self, family: str, session: str) -> SessionState:
        """Contract 02 §4.1's derivation, over this session's live turns."""
        turns = self.of_session(family, session)
        states = [live.record.state for live in turns]
        return derive_session_state(states, _last_terminal_failed(turns))

    def turns_running(self, family: str, session: str) -> int:
        return len(self.active_in_session(family, session))


def _in_session(live: LiveTurn, family: str, session: str) -> bool:
    return live.record.family == family and live.record.session == session


def _in_family(live: LiveTurn, family: str) -> bool:
    return live.record.family == family


def _last_terminal_failed(turns: list[LiveTurn]) -> bool:
    """Contract 02 §4.1 rule 4. `aborted` is not `failed` and never sticks."""
    for live in reversed(turns):
        if live.record.state in ACTIVE_TURN_STATES or live.record.state is TurnState.QUEUED:
            continue

        return live.record.state is TurnState.FAILED

    return False
