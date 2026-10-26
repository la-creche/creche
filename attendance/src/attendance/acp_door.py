"""The byte pipe between one ACP door connection and one `acp-relay`.

One WebSocket text frame is one JSON-RPC line. Each frame and each relay
line gets the envelope check and nothing else: UTF-8 under the size cap,
one line, strict JSON, an object with `"jsonrpc": "2.0"`. Nothing is
translated. A frame reaches the relay as its exact bytes plus one LF, and a
relay line reaches the WebSocket as its exact text.

Either end that closes or fails closes the other one.

The door beats each relay connection. It sends `_creche/beat` on the relay
on the supervisor link's cadence, with string ids of its own:
`creche-beat-<n>`. The facade arms the deadline of a relay connection on
its first beat. The door takes the facade's answer to each of its beats, so
that answer never reaches the client. Each other line passes as it is, a
client line with an id of the same shape included. A relay that leaves two
beats unanswered closes with `Close.NO_BEAT`. An error answer counts as no
answer.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from enum import IntEnum

from starlette.websockets import WebSocket

from .acp_link import (
    BEAT_INTERVAL_S,
    JSONRPC,
    MAX_ACP_LINE_BYTES,
    METHOD_BEAT,
    MISSED_BEATS_ALLOWED,
    Malformed,
    loads_strict,
)
from .atomic import as_object
from .channel import Channel, ChannelClosed
from .errors import ApiError

#: The cap for both directions.
MAX_FRAME_BYTES = MAX_ACP_LINE_BYTES

#: A refusal before the pipe opens closes with 4000 plus the HTTP status of
#: contract 02 §14: 4401 for a bad bearer, 4403 for a door that may not.
_REFUSAL_BASE = 4000

_LINE_BREAKS = ("\n", "\r")

#: The ids of the door's beats are strings with this prefix. The facade's
#: answers to the client's own requests carry the client's ids, so a client
#: that uses this prefix can lose an answer. The prefix is reserved.
BEAT_ID_PREFIX = "creche-beat-"


class Close(IntEnum):
    """Why the pipe closed, as a WebSocket close code (RFC 6455 §7.4.1)."""

    CLIENT_GONE = 1000
    RELAY_GONE = 1001
    BAD_FRAME = 1007
    TOO_LARGE = 1009
    # 4000 plus HTTP 408: the facade did not answer the door's beats.
    NO_BEAT = 4408


class _Beats:
    """The door's beats on one relay connection, and the ones not answered."""

    def __init__(self) -> None:
        self._next = 0
        self._pending: set[str] = set()

    @property
    def missed(self) -> int:
        return len(self._pending)

    def line(self) -> str:
        """The next beat request, as one relay line."""
        beat_id = f"{BEAT_ID_PREFIX}{self._next}"
        self._next += 1
        self._pending.add(beat_id)
        params: dict[str, object] = {}
        message = {"jsonrpc": JSONRPC, "id": beat_id, "method": METHOD_BEAT, "params": params}
        return json.dumps(message, separators=(",", ":")) + "\n"

    def take(self, text: str) -> bool:
        """True for an answer to a beat in flight. The door keeps that line.

        `text` passed `check_frame`, so it is one strict JSON object.
        """
        if BEAT_ID_PREFIX not in text:
            return False

        record = as_object(loads_strict(text)) or {}
        beat_id = record.get("id")

        if "method" in record or not isinstance(beat_id, str) or beat_id not in self._pending:
            return False

        if "result" in record:
            self._pending.clear()

        return True


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


async def pipe(
    websocket: WebSocket, relay: Channel, beat_interval_s: float = BEAT_INTERVAL_S
) -> None:
    """Forward both ways and beat the relay until one end stops. Then close both."""
    beats = _Beats()
    tasks = {
        asyncio.create_task(_to_relay(websocket, relay), name="acp door in"),
        asyncio.create_task(_to_door(websocket, relay, beats), name="acp door out"),
        asyncio.create_task(_beat(relay, beats, beat_interval_s), name="acp door beat"),
    }

    # The relay closes on each way out, a cancel of this call included.
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()

        await asyncio.gather(*tasks, return_exceptions=True)
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


async def _beat(relay: Channel, beats: _Beats, interval_s: float) -> Close:
    """A beat on each tick. The tick after the last allowed miss closes."""
    while True:
        await asyncio.sleep(interval_s)

        if beats.missed >= MISSED_BEATS_ALLOWED:
            return Close.NO_BEAT

        try:
            await relay.send(beats.line())
        except ChannelClosed:
            return Close.RELAY_GONE


async def _to_door(websocket: WebSocket, relay: Channel, beats: _Beats) -> Close:
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

        if beats.take(raw.text):
            continue

        await websocket.send_text(raw.text)
