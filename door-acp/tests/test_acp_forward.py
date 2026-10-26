"""The forward through a real server: client → door → a fake attendance → back.

The fake attendance listens on a Unix socket, as attendance does. It checks
the door's bearer the way attendance does (close 4401), then echoes, closes
with a code after the first frame, or sends one frame over the cap.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import tempfile
from collections.abc import AsyncIterator, Generator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import uvicorn
from agent_door_acp.__main__ import server_config
from agent_door_acp.app import FORWARD_AUTH_HEADER
from agent_door_acp.config import DoorConfig
from agent_door_acp.forward import MAX_FRAME_BYTES, Close
from websockets.asyncio.client import ClientConnection, connect
from websockets.asyncio.server import ServerConnection, unix_serve
from websockets.exceptions import ConnectionClosed

KEY = "k" * 32
TOKEN = "t" * 32
GOOD = {"Authorization": f"Bearer {KEY}", FORWARD_AUTH_HEADER: "operator"}

#: One JSON-RPC frame with text that a generic line reader splits: U+2028.
FRAME = json.dumps(
    {"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {"t": f"a{chr(0x2028)}é"}},
    ensure_ascii=False,
)
OVERSIZE = json.dumps({"jsonrpc": "2.0", "pad": "x" * MAX_FRAME_BYTES})


@dataclass(slots=True)
class FakeAttendance:
    socket: Path
    mode: str = "echo"
    seen: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])

    async def handle(self, connection: ServerConnection) -> None:
        request = connection.request
        assert request is not None
        self.seen.append((request.path, request.headers.get("Authorization", "")))

        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            await connection.close(4401, "unauthorized")
            return

        if self.mode == "oversize":
            await connection.send(OVERSIZE)

        # The door can close first, for a bad frame. That ends this loop.
        with contextlib.suppress(ConnectionClosed):
            async for frame in connection:
                if self.mode.startswith("close "):
                    await connection.close(int(self.mode.split()[1]), "from attendance")
                    return

                await connection.send(frame)


class _Server(uvicorn.Server):
    """A server that leaves the signal handlers of the test process alone."""

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None]:
        yield


@pytest.fixture
async def attendance() -> AsyncIterator[FakeAttendance]:
    # AF_UNIX cannot bind past about 104 bytes on macOS. tmp_path is close.
    socket_dir = Path(tempfile.mkdtemp(prefix="door-acp-"))
    fake = FakeAttendance(socket_dir / "sessiond.sock")

    async with unix_serve(fake.handle, str(fake.socket), max_size=None):
        yield fake

    shutil.rmtree(socket_dir, ignore_errors=True)


@pytest.fixture
async def door(attendance: FakeAttendance) -> AsyncIterator[str]:
    config = DoorConfig(
        bind_host="127.0.0.1",
        bind_port=0,
        door_key=KEY.encode("ascii"),
        attendance_socket=attendance.socket,
        attendance_token=TOKEN.encode("ascii"),
    )
    setting = server_config(config)
    setting.log_level = "warning"
    server = _Server(setting)
    task = asyncio.create_task(server.serve())

    async with asyncio.timeout(10):
        while not server.started:
            await asyncio.sleep(0.01)

    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"ws://127.0.0.1:{port}/acp/chat"

    server.should_exit = True
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def close_of(connection: ClientConnection) -> tuple[int, str]:
    with pytest.raises(ConnectionClosed) as closed:
        async with asyncio.timeout(5):
            await connection.recv()

    received = closed.value.rcvd
    assert received is not None
    return received.code, received.reason


async def test_a_frame_round_trips_byte_for_byte(door: str, attendance: FakeAttendance) -> None:
    async with connect(door, additional_headers=GOOD) as client:
        await client.send(FRAME)
        echoed = await asyncio.wait_for(client.recv(decode=False), 5)

    assert echoed == FRAME.encode("utf-8")
    assert attendance.seen == [("/acp/chat", f"Bearer {TOKEN}")]


@pytest.mark.parametrize("code", [1001, 4403])
async def test_a_close_from_attendance_closes_the_client_with_its_code(
    door: str, attendance: FakeAttendance, code: int
) -> None:
    attendance.mode = f"close {code}"

    async with connect(door, additional_headers=GOOD) as client:
        await client.send(FRAME)
        assert await close_of(client) == (code, "from attendance")


@pytest.mark.parametrize("mode", ["echo", "oversize"], ids=["from_client", "from_attendance"])
async def test_a_frame_over_the_cap_closes_1009(
    door: str, attendance: FakeAttendance, mode: str
) -> None:
    attendance.mode = mode

    async with connect(door, additional_headers=GOOD, max_size=None) as client:
        if mode == "echo":
            await client.send(OVERSIZE)
        assert (await close_of(client))[0] == Close.TOO_LARGE


async def test_a_bad_frame_closes_1007(door: str) -> None:
    async with connect(door, additional_headers=GOOD) as client:
        await client.send('{"jsonrpc":"2.0","id":NaN}')
        assert (await close_of(client))[0] == Close.BAD_FRAME
