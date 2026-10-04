"""A `AttendanceClient` test double, scoped to what this door calls.

Built from contract 02 §15's fake section, but only the part of it this
door touches: create-or-find (§5.1), the writer-lease conflict (§7.2),
idempotency (§6), and the three turn shapes (settled, streamed, failed) of
§4.3. It is not a wire-level fake — the real NDJSON framing and the
`httpx`-based client are covered separately in `test_attendance.py`. A
standalone, protocol-level fake is a different tool (contract 02 §15.1's
`fake-attendance/`).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from agent_door_owui.attendance import (
    TURN_STATE_SETTLED,
    AttendanceError,
    SettledTurn,
    TurnRequest,
)
from agent_door_owui.journal import JournalLine

_DEFAULT_SETTLED = SettledTurn(
    turn="01TESTTURN00000000000000",
    state=TURN_STATE_SETTLED,
    text="ok",
    usage={"input": 1, "output": 1},
    reason="",
)


@dataclass
class FakeAttendance:
    """One script, played back to whichever request arrives next.

    Settable before a call:
      `lines`      -- the JournalLine sequence a streamed turn emits.
      `settled`    -- the SettledTurn a non-streamed turn returns.
      `turn_error` -- a AttendanceError raised by the very next `settled_turn`
                      or `stream_turn` call, then cleared.
      `open_delay_s` -- how long `stream_turn` waits before it answers, with
                      lines or with `turn_error`.

    Recorded for assertions:
      `ensured`       -- every (family, session) `ensure_session` saw.
      `requests`      -- every `TurnRequest` a turn call saw, in order.
      `stream_closed` -- True once an opened stream's context manager has
                         exited, however it exited. `AttendanceClient` has no
                         "stop" or "abort" method at all, so this fake
                         cannot end a turn even if it wanted to — a
                         disconnect can only ever close a READER.
    """

    lines: list[JournalLine] = field(default_factory=list[JournalLine])
    settled: SettledTurn = field(default_factory=lambda: _DEFAULT_SETTLED)
    turn_error: AttendanceError | None = None
    # Raised mid-stream, AFTER `lines` is exhausted — unlike `turn_error`,
    # which is refused before the stream ever opens. This is what a dropped
    # connection to the real attendance looks like once attendance.py has
    # already turned it into StreamBroken (see test_attendance.py).
    stream_error: Exception | None = None
    open_delay_s: float = 0.0

    ensured: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    requests: list[TurnRequest] = field(default_factory=list[TurnRequest])
    # Parallel to `requests`: the `with_parent` each call actually used.
    # A wire-based fake would see this in the body's `owui` object instead;
    # this one is in-process, so it records the flag directly.
    with_parent_flags: list[bool] = field(default_factory=list[bool])
    stream_closed: bool = False

    _by_key: dict[str, tuple[str, SettledTurn | list[JournalLine]]] = field(
        default_factory=dict[str, tuple[str, SettledTurn | list[JournalLine]]]
    )

    async def ensure_session(self, family: str, session: str) -> None:
        self.ensured.append((family, session))

    async def settled_turn(self, request: TurnRequest, *, with_parent: bool = True) -> SettledTurn:
        self.requests.append(request)
        self.with_parent_flags.append(with_parent)
        stored = self._stored(request)
        if stored is not None:
            assert isinstance(stored, SettledTurn)
            return stored

        if self._raise_scripted_error():
            raise self._take_error()

        self._by_key[request.idempotency_key] = (request.prompt, self.settled)
        return self.settled

    @asynccontextmanager
    async def stream_turn(
        self, request: TurnRequest, *, with_parent: bool = True
    ) -> AsyncGenerator[AsyncIterator[JournalLine], None]:
        self.requests.append(request)
        self.with_parent_flags.append(with_parent)
        await asyncio.sleep(self.open_delay_s)
        stored = self._stored(request)
        if stored is not None:
            assert isinstance(stored, list)
            lines = stored
        else:
            if self._raise_scripted_error():
                raise self._take_error()
            lines = self.lines
            self._by_key[request.idempotency_key] = (request.prompt, lines)

        self.stream_closed = False
        try:
            yield _replay(lines, self.stream_error)
        finally:
            self.stream_closed = True

    def _stored(self, request: TurnRequest) -> SettledTurn | list[JournalLine] | None:
        """Contract 02 §6: a repeated key returns the stored turn, and a
        repeat whose prompt differs is `idempotency_mismatch`."""
        entry = self._by_key.get(request.idempotency_key)
        if entry is None:
            return None

        prompt, result = entry
        if prompt != request.prompt:
            raise AttendanceError(
                "idempotency_mismatch", "the key is known and the prompt differs", 400
            )

        return result

    def _raise_scripted_error(self) -> bool:
        return self.turn_error is not None

    def _take_error(self) -> AttendanceError:
        assert self.turn_error is not None
        error, self.turn_error = self.turn_error, None
        return error


async def _replay(
    lines: list[JournalLine], error: Exception | None = None
) -> AsyncIterator[JournalLine]:
    for line in lines:
        yield line
    if error is not None:
        raise error
