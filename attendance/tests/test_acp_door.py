"""`WS /acp/{family}`: the bearer, the byte pipe to `acp-relay`, the caps."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import os
import shutil
import signal
import sys
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from attendance.__main__ import serve
from attendance.acp_door import MAX_FRAME_BYTES, RELAY_ENDED, Close
from attendance.api import Listener, build_app
from attendance.auth import Principal, TokenBook
from attendance.config import DEFAULT_FACADE_COMMAND, DEFAULT_RELAY_COMMAND, Config
from attendance.service import SessionService
from attendance_harness import FAMILY, make_config, write_status, write_tokens
from websockets.asyncio.client import ClientConnection, connect, unix_connect
from websockets.exceptions import ConnectionClosed

REPO = Path(__file__).resolve().parents[2]
FAKE_SBX = REPO / "integration" / "tests" / "fake_sbx.py"
FAKE_RELAY = Path(__file__).resolve().parent / "fake_relay.py"
FAKE_FACADE = Path(__file__).resolve().parent / "fake_facade.py"
DOOR_PATH = f"ws://attendance/acp/{FAMILY}"
SHORT_TOKEN = "x" * 32
HTTP_FORBIDDEN = 403

#: One JSON-RPC line with text that a generic line reader splits: U+2028.
FRAME = json.dumps(
    {"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {"t": f"a{chr(0x2028)}é"}},
    ensure_ascii=False,
)


@dataclasses.dataclass(slots=True)
class Door:
    config: Config
    tokens: dict[Principal, str]
    record: Path

    def connect(self, bearer: str | None) -> connect:
        headers = {"Authorization": f"Bearer {bearer}"} if bearer is not None else {}
        return unix_connect(str(self.config.socket_path), DOOR_PATH, additional_headers=headers)

    def door_acp(self) -> connect:
        return self.connect(self.tokens[Principal.DOOR_ACP])


def relay_command() -> str:
    """`DEFAULT_RELAY_COMMAND`, with `fake_sbx.py` for `sbx` and the fake relay."""
    command = DEFAULT_RELAY_COMMAND.replace("sbx", f"{sys.executable} -I {FAKE_SBX}", 1)
    return command.replace("acp-relay", f"{sys.executable} -I {FAKE_RELAY}")


def facade_command() -> str:
    """`DEFAULT_FACADE_COMMAND`, with `fake_sbx.py` for `sbx` and the fake facade."""
    command = DEFAULT_FACADE_COMMAND.replace("sbx", f"{sys.executable} -I {FAKE_SBX}", 1)
    return command.replace("creche-facade", f"{sys.executable} -I {FAKE_FACADE}")


async def start_door(
    tmp_path: Path, socket_dir: Path, mode: str
) -> tuple[Door, asyncio.Task[None]]:
    record = tmp_path / "relay.txt"
    env_file = tmp_path / "relay.env"
    env_file.write_text(f"FAKE_RELAY_RECORD={record}\nFAKE_RELAY_MODE={mode}\n")
    config = dataclasses.replace(
        make_config(tmp_path),
        socket_path=socket_dir / "sessiond.sock",
        acp_relay_command=relay_command(),
        facade_command=facade_command(),
    )
    write_status(config.state_root, playpen_env=str(env_file))
    made = write_tokens(config.state_root)
    book = TokenBook(config.state_root)
    book.load()
    task = asyncio.create_task(serve(config, book))

    async with asyncio.timeout(10):
        while not config.socket_path.is_socket():
            await asyncio.sleep(0.02)

    return Door(config, made, record), task


@pytest.fixture
async def door(tmp_path: Path, request: pytest.FixtureRequest) -> AsyncIterator[Door]:
    # AF_UNIX cannot bind past about 104 bytes on macOS. tmp_path is close.
    socket_dir = Path(tempfile.mkdtemp(prefix="acp-door-"))
    mode = getattr(request, "param", "echo")
    built, task = await start_door(tmp_path, socket_dir, mode)

    yield built

    task.cancel()

    with contextlib.suppress(asyncio.CancelledError):
        await task

    shutil.rmtree(socket_dir, ignore_errors=True)


async def close_code(connection: ClientConnection) -> int | None:
    with pytest.raises(ConnectionClosed) as closed:
        async with asyncio.timeout(5):
            await connection.recv()

    received = closed.value.rcvd
    return received.code if received is not None else None


@pytest.mark.slow
@pytest.mark.parametrize(
    ("bearer", "code"),
    [(None, 4401), (SHORT_TOKEN, 4401), (Principal.DOOR_TRIGGER, 4403)],
    ids=["missing", "wrong", "other_door"],
)
async def test_a_bad_bearer_closes_before_any_exec(
    door: Door, bearer: str | Principal | None, code: int
) -> None:
    offered = door.tokens[bearer] if isinstance(bearer, Principal) else bearer

    async with door.connect(offered) as connection:
        assert await close_code(connection) == code

    assert not door.record.exists()


@pytest.mark.slow
async def test_a_frame_round_trips_byte_for_byte(door: Door) -> None:
    async with door.door_acp() as connection:
        await connection.send(FRAME)
        echoed = await asyncio.wait_for(connection.recv(), 5)

    assert echoed == FRAME
    assert door.record.read_text() == "started\n"


@pytest.mark.slow
@pytest.mark.parametrize("ending", ["\n", "\r\n"], ids=["lf", "crlf"])
async def test_one_line_ending_of_a_client_frame_is_stripped(door: Door, ending: str) -> None:
    """A client can end each frame in one line break. Agmente does.

    The door adds its own LF for the relay, so a frame it did not strip
    would also reach the relay as an empty line.
    """
    async with door.door_acp() as connection:
        await connection.send(FRAME + ending)
        assert await asyncio.wait_for(connection.recv(), 5) == FRAME
        await connection.send(FRAME)
        assert await asyncio.wait_for(connection.recv(), 5) == FRAME


@pytest.mark.slow
@pytest.mark.parametrize(
    ("frame", "code"),
    [
        (json.dumps({"jsonrpc": "2.0", "pad": "x" * MAX_FRAME_BYTES}), Close.TOO_LARGE),
        ("not json", Close.BAD_FRAME),
        ('{"jsonrpc":"2.0",\n"id":1}', Close.BAD_FRAME),
        ('{"jsonrpc":"2.0","id":NaN}', Close.BAD_FRAME),
        ('{"id":1}', Close.BAD_FRAME),
        ('{"jsonrpc":"2.0","id":1e999,"method":"session/prompt"}', Close.BAD_FRAME),
        (FRAME + "\r", Close.BAD_FRAME),
        (FRAME + "\n\n", Close.BAD_FRAME),
        (FRAME + "\r\n\n", Close.BAD_FRAME),
        ("\n", Close.BAD_FRAME),
        ("\r\n", Close.BAD_FRAME),
    ],
    ids=[
        "over_the_cap",
        "not_json",
        "two_lines",
        "nan",
        "no_envelope",
        "prompt_id_past_double",
        "lone_cr",
        "two_lf",
        "crlf_then_lf",
        "only_lf",
        "only_crlf",
    ],
)
async def test_a_bad_frame_closes_the_connection(door: Door, frame: str, code: int) -> None:
    async with door.door_acp() as connection:
        await connection.send(frame)
        assert await close_code(connection) == code


@pytest.mark.slow
@pytest.mark.parametrize("door", ["one"], indirect=True)
async def test_the_relay_exit_closes_the_websocket(door: Door) -> None:
    async with door.door_acp() as connection:
        await connection.send(FRAME)
        assert await asyncio.wait_for(connection.recv(), 5) == FRAME
        assert await close_code(connection) == Close.RELAY_GONE


#: The time a client may wait for the close of a relay that exited. The
#: beat is 30 s and a reap of a held pipe is 10 s, so neither fits in it.
RELAY_EXIT_SEEN_S = 3.0


@pytest.mark.slow
@pytest.mark.parametrize("door", ["orphan"], indirect=True)
async def test_a_relay_exit_closes_at_once_while_its_stdout_is_held(door: Door) -> None:
    """`sbx exec` can exit while a process it started holds its stdout."""
    async with door.door_acp() as connection:
        await connection.send(FRAME)
        assert await asyncio.wait_for(connection.recv(), 5) == FRAME

        try:
            with pytest.raises(ConnectionClosed) as closed:
                await asyncio.wait_for(connection.recv(), RELAY_EXIT_SEEN_S)
        finally:
            for line in door.record.read_text().splitlines():
                if line.startswith("orphan "):
                    os.kill(int(line.split()[1]), signal.SIGKILL)

    received = closed.value.rcvd
    assert received is not None
    assert received.code == Close.RELAY_GONE
    assert received.reason == RELAY_ENDED


@pytest.mark.slow
async def test_the_acp_bearer_reaches_no_http_route(door: Door) -> None:
    transport = httpx.AsyncHTTPTransport(uds=str(door.config.socket_path))
    headers = {"Authorization": f"Bearer {door.tokens[Principal.DOOR_ACP]}"}

    async with httpx.AsyncClient(transport=transport, base_url="http://attendance") as client:
        answer = await client.post(
            "/dispatch/jobs", json={"caller_family": FAMILY}, headers=headers
        )

    assert answer.status_code == HTTP_FORBIDDEN


def test_the_lan_listener_has_no_acp_route(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    service = SessionService(config)
    book = TokenBook(config.state_root)
    paths = {getattr(route, "path", "") for route in build_app(service, book, Listener.LAN).routes}
    socket_paths = {
        getattr(route, "path", "") for route in build_app(service, book, Listener.SOCKET).routes
    }

    assert "/acp/{family}" not in paths
    assert "/acp/{family}" in socket_paths
