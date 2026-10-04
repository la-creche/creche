"""The attendance client over a transport that a test writes.

`test_attendance.py` covers the functions that shape a body and parse a
reply. This module covers what the client does when a call gets no answer,
and when a refusal is not text.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from agent_door_owui.attendance import AttendanceError, HttpAttendance, TurnRequest
from agent_door_owui.config import DoorConfig
from agent_door_owui.errors import CODE_UNREACHABLE

ATTENDANCE_URL = "http://attendance"

TURN = TurnRequest(
    family="chat",
    session="owui-3f2a9c41",
    prompt="which sensor dropped out?",
    idempotency_key="b7c1e2d0",
    persona=None,
    chat_id="3f2a9c41",
    message_id="b7c1e2d0",
    user_message_id="a1b2c3d4",
    parent_id=None,
)


def _over(handler: Callable[[httpx.Request], httpx.Response]) -> HttpAttendance:
    config = DoorConfig(
        bind_host="127.0.0.1",
        bind_port=8340,
        door_key="d" * 32,
        attendance_token="t" * 32,
        attendance_url=ATTENDANCE_URL,
        attendance_socket=None,
        families_dir=Path("families"),
    )
    upstream = httpx.AsyncClient(base_url=ATTENDANCE_URL, transport=httpx.MockTransport(handler))

    return HttpAttendance(config, upstream)


def _no_answer(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("no answer", request=request)


async def test_a_session_call_with_no_answer_is_an_attendance_error() -> None:
    with pytest.raises(AttendanceError) as caught:
        await _over(_no_answer).ensure_session(TURN.family, TURN.session)

    assert caught.value.code == CODE_UNREACHABLE


async def test_a_settled_turn_with_no_answer_is_an_attendance_error() -> None:
    with pytest.raises(AttendanceError) as caught:
        await _over(_no_answer).settled_turn(TURN)

    assert caught.value.code == CODE_UNREACHABLE


async def test_a_streamed_turn_with_no_answer_is_an_attendance_error() -> None:
    with pytest.raises(AttendanceError) as caught:
        async with _over(_no_answer).stream_turn(TURN):
            pass

    assert caught.value.code == CODE_UNREACHABLE


async def test_a_stream_refusal_that_is_not_utf8_is_still_a_refusal() -> None:
    def refuse(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"\xff\xfe")

    with pytest.raises(AttendanceError) as caught:
        async with _over(refuse).stream_turn(TURN):
            pass

    assert caught.value.code == "internal"
    assert caught.value.status == 503


async def test_a_streamed_turn_that_opens_gives_its_lines() -> None:
    line = b'{"journal_seq":1,"kind":"note","turn":null,"body":{"text":"a"}}\n'

    def answer(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=line)

    async with _over(answer).stream_turn(TURN) as lines:
        kinds = [one.kind async for one in lines]

    assert kinds == ["note"]
