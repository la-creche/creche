"""The attendance client: the wire body it sends, and what it makes of a
reply. HTTP itself is not exercised here — `HttpAttendance` is a thin,
directly-readable wrapper around `httpx`, and the door's own tests cover it
end to end through `FakeAttendance`. This module tests the parsing and shaping
functions most likely to carry a bug: the ones a single crafted response can
crash (contract 02 is untrusted input, invariant 12)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import cast

import httpx
import pytest
from agent_door_owui.attendance import (
    TURN_STATE_SETTLED,
    SettledTurn,
    StreamBroken,
    TurnRequest,
    WaitMode,
    _error_of,  # pyright: ignore[reportPrivateUsage]
    _iter_lines,  # pyright: ignore[reportPrivateUsage]
    _settled_of,  # pyright: ignore[reportPrivateUsage]
)
from agent_door_owui.journal import MAX_LINE_BYTES, JournalLine
from agent_door_owui.untrusted import as_object

TURN = TurnRequest(
    family="chat",
    session="owui-3f2a9c41",
    prompt="which sensor dropped out?",
    idempotency_key="b7c1e2d0",
    persona="You answer as the house assistant.",
    chat_id="3f2a9c41",
    message_id="b7c1e2d0",
    user_message_id="a1b2c3d4",
    parent_id="9f8e7d6c",
)


def test_the_wire_body_carries_every_owui_id() -> None:
    body = TURN.body(WaitMode.STREAM)

    assert body["prompt"] == TURN.prompt
    assert body["idempotency_key"] == TURN.idempotency_key
    assert body["wait"] == "stream"
    assert body["persona_text"] == TURN.persona
    assert body["owui"] == {
        "chat_id": "3f2a9c41",
        "message_id": "b7c1e2d0",
        "user_message_id": "a1b2c3d4",
        "parent_id": "9f8e7d6c",
    }


def test_dropping_the_parent_keeps_everything_else() -> None:
    # This is the branch-fallback retry: the idempotency key must stay put,
    # so the retry names the same assistant message as the first attempt.
    body = TURN.body(WaitMode.SETTLED, with_parent=False)
    owui = as_object(body["owui"])

    assert "parent_id" not in owui
    assert body["idempotency_key"] == TURN.idempotency_key


def test_no_persona_is_left_out_of_the_body() -> None:
    bare = TurnRequest(
        family="chat",
        session="owui-x",
        prompt="hi",
        idempotency_key="k",
        persona=None,
        chat_id="x",
        message_id="k",
        user_message_id=None,
        parent_id=None,
    )

    assert "persona_text" not in bare.body(WaitMode.STREAM)


def test_settled_is_the_only_success_state() -> None:
    settled = SettledTurn(turn="t", state=TURN_STATE_SETTLED, text="hi", usage={}, reason="")
    failed = SettledTurn(turn="t", state="failed", usage={}, text="", reason="model_error")

    assert settled.is_settled
    assert not failed.is_settled


def test_a_settled_body_parses_every_field() -> None:
    raw = '{"turn":"01JB","state":"settled","text":"hi","usage":{"input":4},"reason":null}'

    result = _settled_of(raw)

    assert result.turn == "01JB"
    assert result.is_settled
    assert result.text == "hi"
    assert result.usage == {"input": 4}
    assert result.reason == ""


def test_a_failed_settled_body_carries_its_reason() -> None:
    raw = '{"turn":"01JB","state":"failed","text":"","usage":{},"reason":"turn_timeout"}'

    result = _settled_of(raw)

    assert not result.is_settled
    assert result.reason == "turn_timeout"


def test_a_junk_settled_body_does_not_raise() -> None:
    result = _settled_of("not json")

    assert result.turn == ""
    assert not result.is_settled


def test_an_error_body_becomes_a_attendance_error() -> None:
    raw = '{"error":{"code":"session_busy","message":"another door holds the writer lease"}}'

    error = _error_of(409, raw)

    assert error.code == "session_busy"
    assert error.message == "another door holds the writer lease"
    assert error.status == 409


def test_a_missing_error_body_still_fails_shut() -> None:
    error = _error_of(500, "")

    assert error.code == "internal"
    assert "500" in error.message


async def _bytes(chunks: tuple[bytes, ...], error: Exception | None) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk
    if error is not None:
        raise error


class _FakeResponse:
    """Just enough of `httpx.Response` for `_iter_lines`: one async method.

    An optional `error`, raised after every chunk is yielded, stands in for
    a connection that drops mid-stream — the case `_iter_lines` must turn
    into `StreamBroken` rather than leak as a raw `httpx` exception.
    """

    def __init__(self, *chunks: bytes, error: Exception | None = None) -> None:
        self._chunks = chunks
        self._error = error

    def aiter_bytes(self) -> AsyncIterator[bytes]:
        return _bytes(self._chunks, self._error)


async def _collect(response: _FakeResponse) -> list[JournalLine]:
    return [line async for line in _iter_lines(cast(httpx.Response, response))]


async def test_lines_split_on_lf_even_across_chunk_boundaries() -> None:
    first = b'{"journal_seq":1,"kind":"heartbeat","turn":null,"body":{}}\n{"journal_s'
    second = b'eq":2,"kind":"note","turn":null,"body":{"text":"a"}}\n'

    lines = await _collect(_FakeResponse(first, second))

    assert [line.kind for line in lines] == ["heartbeat", "note"]


async def test_a_line_separator_inside_a_string_is_not_a_line_break() -> None:
    # U+2028 LINE SEPARATOR is legal inside a JSON string. A generic line
    # reader (Python's str.splitlines, httpx's aiter_lines) treats it as a
    # break; this one must not. Built with chr() rather than a source escape
    # so the exact byte stays visible in a diff.
    line_sep = chr(0x2028)
    body = f'{{"journal_seq":1,"kind":"note","turn":null,"body":{{"text":"a{line_sep}b"}}}}\n'

    lines = await _collect(_FakeResponse(body.encode("utf-8")))

    assert len(lines) == 1
    assert lines[0].text("text") == f"a{line_sep}b"


async def test_a_bad_line_is_dropped_and_the_good_ones_survive() -> None:
    raw = b'not json\n{"journal_seq":1,"kind":"note","turn":null,"body":{}}\n'

    lines = await _collect(_FakeResponse(raw))

    assert [line.kind for line in lines] == ["note"]


async def test_blank_lines_are_skipped() -> None:
    raw = b'\n\n{"journal_seq":1,"kind":"note","turn":null,"body":{}}\n'

    lines = await _collect(_FakeResponse(raw))

    assert len(lines) == 1


async def test_an_overlong_line_breaks_the_stream_rather_than_resync() -> None:
    huge = (
        b'{"journal_seq":1,"kind":"note","turn":null,"body":{"text":"'
        + b"x" * MAX_LINE_BYTES
        + b'"}}\n'
    )

    with pytest.raises(StreamBroken):
        await _collect(_FakeResponse(huge))


async def test_a_dropped_connection_becomes_stream_broken_not_an_httpx_error() -> None:
    # app.py is written against AttendanceClient and never imports httpx. A
    # caller that had to catch httpx.HTTPError directly would be reaching
    # past this module's own abstraction.
    good = b'{"journal_seq":1,"kind":"note","turn":null,"body":{"text":"a"}}\n'
    response = _FakeResponse(good, error=httpx.ReadError("connection reset"))

    with pytest.raises(StreamBroken, match="connection to attendance broke"):
        await _collect(response)
