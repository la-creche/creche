"""The forward: one client WebSocket to one attendance WebSocket.

Each text frame passes as it is, in both directions. A client frame may
end in one line ending, one LF or one CR LF. The door strips it, and the
frame passes without it. Each frame then gets the envelope check of
attendance's ACP door and nothing else: UTF-8 under the size cap, one line,
strict JSON, an object with `"jsonrpc": "2.0"`. Each other line break is a
bad frame. A frame from attendance gets no allowance. Nothing is
translated. When one side closes, the door closes the other side with the
same code and reason.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from pathlib import Path

from starlette.websockets import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import ClientConnection, unix_connect
from websockets.exceptions import ConnectionClosed, WebSocketException
from websockets.frames import EXTERNAL_CLOSE_CODES

#: attendance's cap for one frame (`MAX_ACP_LINE_BYTES`), in UTF-8 bytes.
MAX_FRAME_BYTES = 1_048_576

#: The time to open the WebSocket to attendance.
CONNECT_TIMEOUT_S = 10.0

UNREACHABLE_REASON = "attendance unreachable"

_JSONRPC = "2.0"
_LINE_BREAKS = ("\n", "\r")

#: The one line ending that a client frame may carry. CR LF is tried first.
_LINE_ENDINGS = ("\r\n", "\n")
_NO_STATUS = 1005


class Close(IntEnum):
    """The codes that the door itself sends (RFC 6455 §7.4.1)."""

    NORMAL = 1000
    GOING_AWAY = 1001
    BAD_FRAME = 1007
    TOO_LARGE = 1009
    #: 1014 "Bad Gateway": the door cannot reach attendance.
    UNREACHABLE = 1014


class Side(StrEnum):
    """The side that ended the forward. The door closes the other sides."""

    CLIENT = "client"
    ATTENDANCE = "attendance"
    DOOR = "door"


@dataclass(frozen=True, slots=True)
class Upstream:
    """attendance's Unix socket and the door's bearer for it. The token is a secret."""

    socket: Path
    token: bytes


@dataclass(frozen=True, slots=True)
class End:
    side: Side
    code: int
    reason: str = ""


class _Malformed(Exception):
    pass


def strip_line_end(text: str) -> str:
    """A client frame without its one line ending: one LF or one CR LF.

    Some clients end each frame in a line break. Agmente does. Each other
    line break stays, so `check_frame` refuses it.
    """
    for ending in _LINE_ENDINGS:
        if text.endswith(ending):
            return text.removesuffix(ending)

    return text


def check_frame(text: str) -> Close | None:
    """The envelope check. None when the frame may pass."""
    if len(text.encode("utf-8", "surrogatepass")) > MAX_FRAME_BYTES:
        return Close.TOO_LARGE

    if any(mark in text for mark in _LINE_BREAKS):
        return Close.BAD_FRAME

    try:
        record = json.loads(text, object_pairs_hook=_strict_pairs, parse_constant=_no_constant)
    except Exception:
        # Every type: deep nesting raises RecursionError, a long integer a ValueError.
        return Close.BAD_FRAME

    if not isinstance(record, dict) or record.get("jsonrpc") != _JSONRPC:  # pyright: ignore[reportUnknownMemberType]
        return Close.BAD_FRAME

    return None


async def forward(client: WebSocket, family: str, upstream: Upstream) -> None:
    """Forward frames between an accepted client and attendance until one side closes."""
    try:
        attendance = await unix_connect(
            str(upstream.socket),
            f"ws://attendance/acp/{family}",
            additional_headers={"Authorization": "Bearer " + upstream.token.decode("ascii")},
            max_size=MAX_FRAME_BYTES,
            compression=None,
            open_timeout=CONNECT_TIMEOUT_S,
        )
    except (OSError, TimeoutError, WebSocketException):
        await _close_client(client, End(Side.DOOR, Close.UNREACHABLE, UNREACHABLE_REASON))
        return

    # The code for attendance when the race itself fails or is cancelled.
    end = End(Side.DOOR, Close.GOING_AWAY)
    try:
        end = await _race(client, attendance)

        if end.side is not Side.CLIENT:
            await _close_client(client, end)
    finally:
        # A no-op when attendance closed first.
        await attendance.close(end.code, end.reason)


async def _race(client: WebSocket, attendance: ClientConnection) -> End:
    inbound = asyncio.create_task(_to_attendance(client, attendance), name="acp in")
    outbound = asyncio.create_task(_to_client(attendance, client), name="acp out")

    try:
        done, _ = await asyncio.wait({inbound, outbound}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        inbound.cancel()
        outbound.cancel()
        await asyncio.gather(inbound, outbound, return_exceptions=True)

    return done.pop().result()


async def _to_attendance(client: WebSocket, attendance: ClientConnection) -> End:
    while True:
        message = await client.receive()

        if message["type"] == "websocket.disconnect":
            code = int(message.get("code", _NO_STATUS))
            return End(Side.CLIENT, _sendable(code), str(message.get("reason") or ""))

        text: object = message.get("text")
        if not isinstance(text, str):
            return End(Side.DOOR, Close.BAD_FRAME)

        line = strip_line_end(text)
        refused = check_frame(line)
        if refused is not None:
            return End(Side.DOOR, refused)

        try:
            await attendance.send(line)
        except ConnectionClosed as closed:
            return _attendance_end(closed)


async def _to_client(attendance: ClientConnection, client: WebSocket) -> End:
    while True:
        try:
            frame = await attendance.recv()
        except ConnectionClosed as closed:
            return _attendance_end(closed)

        if not isinstance(frame, str):
            return End(Side.DOOR, Close.BAD_FRAME)

        refused = check_frame(frame)
        if refused is not None:
            return End(Side.DOOR, refused)

        try:
            await client.send_text(frame)
        except (OSError, RuntimeError, WebSocketDisconnect):
            return End(Side.CLIENT, Close.GOING_AWAY)


def _attendance_end(closed: ConnectionClosed) -> End:
    """attendance's own close frame, else the one the door sent, else a lost link."""
    frame = closed.rcvd or closed.sent
    if frame is None:
        return End(Side.ATTENDANCE, Close.UNREACHABLE, UNREACHABLE_REASON)

    return End(Side.ATTENDANCE, _sendable(frame.code), frame.reason)


def _sendable(code: int) -> int:
    """1005 and 1006 name a close with no code. They never go on the wire."""
    if code in EXTERNAL_CLOSE_CODES or 3000 <= code < 5000:
        return code

    return Close.NORMAL if code == _NO_STATUS else Close.GOING_AWAY


async def _close_client(client: WebSocket, end: End) -> None:
    # The client can leave in the same moment. A second close is no error.
    with contextlib.suppress(OSError, RuntimeError):
        await client.close(code=end.code, reason=end.reason)


def _strict_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    record = dict(pairs)
    if len(record) != len(pairs):
        raise _Malformed("a key twice")

    return record


def _no_constant(name: str) -> object:
    raise _Malformed(name)
