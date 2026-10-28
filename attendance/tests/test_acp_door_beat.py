"""The ACP door beats each relay connection (the owner's decision).

The door sends `_creche/beat` on the relay with ids of its own,
`creche-beat-<n>`. It takes the facade's answers, so no answer reaches
the client. Each other line passes as it is. A relay that misses two
beats closes with `Close.NO_BEAT`.

The relay is a `FakeChannel`: the test plays the facade.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

from attendance.acp_door import (
    BEAT_ID_PREFIX,
    BEAT_INTERVAL_S,
    MISSED_BEATS_ALLOWED,
    Close,
    pipe,
)
from attendance.channel import FakeChannel
from starlette.websockets import WebSocket

VECTORS = Path(__file__).resolve().parents[2] / "facade" / "vectors" / "creche-extensions.json"
SHORT_S = 0.1
WAIT_S = 2.0

CLIENT_FRAME = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "session/list", "params": {}})


class FakeSocket:
    """The client end of the door: what `pipe` reads and writes."""

    def __init__(self) -> None:
        self.inbound: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.sent: asyncio.Queue[str] = asyncio.Queue()
        self.closed: asyncio.Future[int] = asyncio.get_running_loop().create_future()

    async def receive(self) -> dict[str, Any]:
        return await self.inbound.get()

    async def send_text(self, text: str) -> None:
        await self.sent.put(text)

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        if not self.closed.done():
            self.closed.set_result(code)

    async def say(self, text: str) -> None:
        await self.inbound.put({"type": "websocket.receive", "text": text})

    async def leave(self) -> None:
        await self.inbound.put({"type": "websocket.disconnect", "code": 1000})


async def started(socket: FakeSocket, relay: FakeChannel) -> asyncio.Task[None]:
    await relay.start()
    door = cast(WebSocket, socket)
    return asyncio.create_task(pipe(door, relay, beat_interval_s=SHORT_S))


async def next_beat(relay: FakeChannel) -> dict[str, Any]:
    while True:
        message = json.loads(await relay.next_host_line(WAIT_S))

        if message.get("method") == "_creche/beat":
            return message


def answer(beat: dict[str, Any]) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": beat["id"], "result": {}})


def test_the_cadence_is_the_cadence_of_the_vector_file() -> None:
    vectors = json.loads(VECTORS.read_text(encoding="utf-8"))

    assert vectors["beat_interval_s"] == BEAT_INTERVAL_S
    assert vectors["missed_beats_allowed"] == MISSED_BEATS_ALLOWED


async def test_beats_are_sent_on_the_cadence_with_reserved_ids() -> None:
    socket, relay = FakeSocket(), FakeChannel("sbx-1")
    task = await started(socket, relay)
    loop = asyncio.get_running_loop()
    begun = loop.time()

    first = await next_beat(relay)
    await relay.feed(answer(first))
    second = await next_beat(relay)
    took = loop.time() - begun

    vectors = json.loads(VECTORS.read_text(encoding="utf-8"))
    shape = {key: value for key, value in vectors["beat"]["request"].items() if key != "id"}
    assert {key: value for key, value in first.items() if key != "id"} == shape
    assert first["id"] == f"{BEAT_ID_PREFIX}0"
    assert second["id"] == f"{BEAT_ID_PREFIX}1"
    assert took >= 2 * SHORT_S

    await socket.leave()
    await asyncio.wait_for(task, WAIT_S)


async def test_a_beat_answer_never_reaches_the_client() -> None:
    socket, relay = FakeSocket(), FakeChannel("sbx-1")
    task = await started(socket, relay)

    beat = await next_beat(relay)
    await relay.feed(answer(beat))
    after = json.dumps({"jsonrpc": "2.0", "id": 7, "result": {"sessions": []}})
    await relay.feed(after)

    assert await asyncio.wait_for(socket.sent.get(), WAIT_S) == after
    assert socket.sent.empty()

    await socket.leave()
    await asyncio.wait_for(task, WAIT_S)


async def test_client_frames_with_the_beat_id_shape_still_pass() -> None:
    socket, relay = FakeSocket(), FakeChannel("sbx-1")
    task = await started(socket, relay)
    request = json.dumps(
        {"jsonrpc": "2.0", "id": f"{BEAT_ID_PREFIX}0", "method": "session/list", "params": {}}
    )

    await socket.say(request)
    await socket.say(CLIENT_FRAME)
    forwarded: list[str] = []

    while len(forwarded) < 2:
        line = await relay.next_host_line(WAIT_S)

        if '"_creche/beat"' not in line:
            forwarded.append(line)

    assert forwarded == [request + "\n", CLIENT_FRAME + "\n"]

    await socket.leave()
    await asyncio.wait_for(task, WAIT_S)


async def test_an_answer_for_no_beat_in_flight_passes() -> None:
    socket, relay = FakeSocket(), FakeChannel("sbx-1")
    task = await started(socket, relay)
    stray = json.dumps({"jsonrpc": "2.0", "id": f"{BEAT_ID_PREFIX}99", "result": {}})

    await relay.feed(stray)

    assert await asyncio.wait_for(socket.sent.get(), WAIT_S) == stray

    await socket.leave()
    await asyncio.wait_for(task, WAIT_S)


async def test_missed_beats_close_the_relay_with_the_code() -> None:
    socket, relay = FakeSocket(), FakeChannel("sbx-1")
    task = await started(socket, relay)

    for _ in range(MISSED_BEATS_ALLOWED):
        await next_beat(relay)

    assert await asyncio.wait_for(socket.closed, WAIT_S) == Close.NO_BEAT
    await asyncio.wait_for(task, WAIT_S)
    assert not relay.alive


async def test_answered_beats_keep_the_relay_open() -> None:
    socket, relay = FakeSocket(), FakeChannel("sbx-1")
    task = await started(socket, relay)

    for _ in range(MISSED_BEATS_ALLOWED + 2):
        await relay.feed(answer(await next_beat(relay)))

    assert not socket.closed.done()
    assert relay.alive

    await socket.leave()
    await asyncio.wait_for(task, WAIT_S)


async def test_a_refused_beat_counts_as_missed() -> None:
    socket, relay = FakeSocket(), FakeChannel("sbx-1")
    task = await started(socket, relay)
    error = {"code": -32601, "message": "no such method"}

    for _ in range(MISSED_BEATS_ALLOWED):
        beat = await next_beat(relay)
        await relay.feed(json.dumps({"jsonrpc": "2.0", "id": beat["id"], "error": error}))

    assert await asyncio.wait_for(socket.closed, WAIT_S) == Close.NO_BEAT
    await asyncio.wait_for(task, WAIT_S)
    assert socket.sent.empty()
