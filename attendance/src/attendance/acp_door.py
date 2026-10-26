"""The byte pipe between one ACP door connection and one `acp-relay`.

One WebSocket text frame is one JSON-RPC line. Each frame and each relay
line gets the envelope check and nothing else: UTF-8 under the size cap,
one line, strict JSON, an object with `"jsonrpc": "2.0"`. Nothing is
translated. A frame reaches the relay as its exact bytes plus one LF, and a
relay line reaches the WebSocket as its exact text.

Either end that closes or fails closes the other one.
"""

from __future__ import annotations

import asyncio
import contextlib
from enum import IntEnum

from starlette.websockets import WebSocket

from .acp_link import JSONRPC, MAX_ACP_LINE_BYTES, Malformed, loads_strict
from .atomic import as_object
from .channel import Channel, ChannelClosed
from .errors import ApiError

#: The cap for both directions.
MAX_FRAME_BYTES = MAX_ACP_LINE_BYTES

#: A refusal before the pipe opens closes with 4000 plus the HTTP status of
#: contract 02 §14: 4401 for a bad bearer, 4403 for a door that may not.
_REFUSAL_BASE = 4000

_LINE_BREAKS = ("\n", "\r")


class Close(IntEnum):
    """Why the pipe closed, as a WebSocket close code (RFC 6455 §7.4.1)."""

    CLIENT_GONE = 1000
    RELAY_GONE = 1001
    BAD_FRAME = 1007
    TOO_LARGE = 1009


def refusal_code(error: ApiError) -> int:
    return _REFUSAL_BASE + error.status


def check_frame(text: str) -> Close | None:
    """The envelope check. None when the frame may pass."""
    if len(text.encode("utf-8", "surrogatepass")) > MAX_FRAME_BYTES:
        return Close.TOO_LARGE

    if any(mark in text for mark in _LINE_BREAKS):
        return Close.BAD_FRAME

    try:
        record = as_object(loads_strict(text))
    except Malformed:
        return Close.BAD_FRAME

    if record is None or record.get("jsonrpc") != JSONRPC:
        return Close.BAD_FRAME

    return None


async def pipe(websocket: WebSocket, relay: Channel) -> None:
    """Forward both ways until one end stops. Then close both."""
    inbound = asyncio.create_task(_to_relay(websocket, relay), name="acp door in")
    outbound = asyncio.create_task(_to_door(websocket, relay), name="acp door out")

    # The relay closes on each way out, a cancel of this call included.
    try:
        done, _ = await asyncio.wait({inbound, outbound}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        inbound.cancel()
        outbound.cancel()
        await asyncio.gather(inbound, outbound, return_exceptions=True)
        await relay.close()

    finished = done.pop()
    why = finished.result() if finished.exception() is None else Close.RELAY_GONE

    if why is Close.CLIENT_GONE:
        return

    # The client can leave in the same moment. A second close is no error.
    with contextlib.suppress(RuntimeError, OSError):
        await websocket.close(code=why)


async def _to_relay(websocket: WebSocket, relay: Channel) -> Close:
    while True:
        message = await websocket.receive()
        text: object = message.get("text")

        if message["type"] == "websocket.disconnect":
            return Close.CLIENT_GONE

        if not isinstance(text, str):
            return Close.BAD_FRAME

        refused = check_frame(text)

        if refused is not None:
            return refused

        try:
            await relay.send(text + "\n")
        except ChannelClosed:
            return Close.RELAY_GONE


async def _to_door(websocket: WebSocket, relay: Channel) -> Close:
    while True:
        try:
            raw = await relay.receive()
        except ChannelClosed:
            return Close.RELAY_GONE

        if raw is None:
            return Close.RELAY_GONE

        if raw.text is None:
            return Close.TOO_LARGE if raw.size > MAX_FRAME_BYTES else Close.BAD_FRAME

        refused = check_frame(raw.text)

        if refused is not None:
            return refused

        await websocket.send_text(raw.text)
