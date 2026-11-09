"""`WS /acp/{family}`: the bearer, the answers from the host list, the routes.

Contract 02 §5. attendance answers `initialize`, `session/new` and
`session/list` itself, with no boot, and routes each other method by its
`sessionId` over one vmm ACP stream per session (decisions CH21-7, CH21-9,
CH10-9, CH83 rule 1).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import tempfile
from collections.abc import AsyncIterator, Callable, Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from attendance.acp_door import (
    HANDSHAKE_FAILED,
    INITIALIZE_RESPONSE,
    MAX_FRAME_BYTES,
    RELAY_ENDED,
    RPC_INTERNAL_ERROR,
    RPC_INVALID_PARAMS,
    RPC_METHOD_NOT_FOUND,
    RPC_NOT_FOUND,
    Close,
)
from attendance.api import Listener, build_app
from attendance.auth import Principal, TokenBook
from attendance.ids import is_session_id, new_session_id
from attendance.service import SessionService
from attendance_harness import FAMILY, make_config, make_service, write_status, write_tokens
from creche_wire import DoorOrigin
from fake_facade import ANSWER_CHUNKS, PromptMode, chunk
from fake_vmm import BUSY, FakeVmm
from websockets.asyncio.client import ClientConnection, connect, unix_connect
from websockets.exceptions import ConnectionClosed

DOOR_PATH = f"ws://attendance/acp/{FAMILY}"
SHORT_TOKEN = "x" * 32
HTTP_FORBIDDEN = 403
CWD = "/home/operator/la-creche"
SBX_ERA_ID = "sess-1"

#: One JSON-RPC line with text that a generic line reader splits: U+2028.
INIT = json.dumps(
    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"t": f"a{chr(0x2028)}é"}},
    ensure_ascii=False,
)


class _Server(uvicorn.Server):
    """A listener that leaves the signal handlers of the test run alone."""

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None]:
        yield


@dataclass(slots=True)
class Door:
    socket: Path
    tokens: dict[Principal, str]
    vmm: FakeVmm
    service: SessionService

    def connect(self, bearer: str | None) -> connect:
        headers = {"Authorization": f"Bearer {bearer}"} if bearer is not None else {}
        return unix_connect(str(self.socket), DOOR_PATH, additional_headers=headers)

    def door_acp(self) -> connect:
        return self.connect(self.tokens[Principal.DOOR_ACP])


@pytest.fixture
async def door(tmp_path: Path, request: pytest.FixtureRequest) -> AsyncIterator[Door]:
    # AF_UNIX cannot bind past about 104 bytes on macOS. tmp_path is close.
    socket_dir = Path(tempfile.mkdtemp(prefix="acp-door-"))
    socket = socket_dir / "attendance.sock"
    config = make_config(tmp_path)
    write_status(config.state_root)
    made = write_tokens(config.state_root)
    book = TokenBook(config.state_root)
    book.load()
    vmm = FakeVmm(prompt_mode=getattr(request, "param", PromptMode.OK))
    service = make_service(config, vmm)
    app = build_app(service, book, Listener.SOCKET)
    server = _Server(uvicorn.Config(app, uds=str(socket), log_level="warning"))
    task = asyncio.create_task(server.serve())

    async with asyncio.timeout(10):
        while not socket.is_socket():
            await asyncio.sleep(0.02)

    yield Door(socket, made, vmm, service)

    server.should_exit = True

    with contextlib.suppress(asyncio.CancelledError):
        await task

    await vmm.stop()
    shutil.rmtree(socket_dir, ignore_errors=True)


async def close_code(connection: ClientConnection) -> int | None:
    with pytest.raises(ConnectionClosed) as closed:
        async with asyncio.timeout(5):
            await connection.recv()

    received = closed.value.rcvd
    return received.code if received is not None else None


async def ask(connection: ClientConnection, request_id: object, method: str, **params: Any) -> Any:
    """One request and its answer."""
    message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
    await connection.send(json.dumps(message))
    return json.loads(await asyncio.wait_for(connection.recv(), 5))


async def new_session(connection: ClientConnection) -> str:
    answer = await ask(connection, "new", "session/new", cwd=CWD, mcpServers=[])
    return str(answer["result"]["sessionId"])


def prompt(request_id: object, session: str) -> str:
    params = {"sessionId": session, "prompt": [{"type": "text", "text": "hello"}]}
    return json.dumps(
        {"jsonrpc": "2.0", "id": request_id, "method": "session/prompt", "params": params}
    )


@pytest.mark.slow
@pytest.mark.parametrize(
    ("bearer", "code"),
    [(None, 4401), (SHORT_TOKEN, 4401), (Principal.DOOR_TRIGGER, 4403)],
    ids=["missing", "wrong", "other_door"],
)
async def test_a_bad_bearer_closes_before_any_vmm_call(
    door: Door, bearer: str | Principal | None, code: int
) -> None:
    offered = door.tokens[bearer] if isinstance(bearer, Principal) else bearer

    async with door.connect(offered) as connection:
        assert await close_code(connection) == code

    assert door.vmm.creates == []
    assert door.vmm.opened == []


@pytest.mark.slow
async def test_initialize_is_answered_with_no_boot(door: Door) -> None:
    """Decision CH21-7: one `INITIALIZE_RESPONSE`, from attendance itself."""
    async with door.door_acp() as connection:
        await connection.send(INIT)
        answer = json.loads(await asyncio.wait_for(connection.recv(), 5))

    assert answer == {"jsonrpc": "2.0", "id": 1, "result": INITIALIZE_RESPONSE}
    assert door.vmm.opened == []


@pytest.mark.slow
async def test_session_new_makes_a_door_row_and_boots_nothing(door: Door) -> None:
    """Contract 02 §5.3: a minted id, a create with the door origin, no stream."""
    async with door.door_acp() as connection:
        session = await new_session(connection)

    assert is_session_id(session)
    (create,) = door.vmm.creates
    assert create.id.root == session
    assert create.family.root == FAMILY
    assert create.cwd.root == CWD
    assert isinstance(create.origin.root, DoorOrigin)
    assert door.vmm.opened == []


@pytest.mark.slow
@pytest.mark.parametrize(
    "params",
    [
        {"cwd": "relative", "mcpServers": []},
        {"cwd": "/a\x00b", "mcpServers": []},
        {"cwd": "/" + "a" * 4096, "mcpServers": []},
        {"cwd": CWD},
        {"cwd": CWD, "mcpServers": [{"name": "x"}]},
        {"cwd": CWD, "mcpServers": [], "additionalDirectories": ["/tmp"]},
    ],
    ids=["relative", "nul", "too_long", "no_servers", "a_server", "a_directory"],
)
async def test_a_bad_session_new_is_invalid_params(door: Door, params: dict[str, Any]) -> None:
    async with door.door_acp() as connection:
        answer = await ask(connection, 7, "session/new", **params)

    assert answer["error"]["code"] == RPC_INVALID_PARAMS
    assert door.vmm.creates == []


@pytest.mark.slow
@pytest.mark.parametrize("method", ["session/new", "session/list", "session/prompt"])
async def test_a_creche_meta_key_is_invalid_params(door: Door, method: str) -> None:
    """Contract 02 §5.1 rule 5: a door client sets no creche key. The
    request reaches no vmm route and no stream (decision CH21-4)."""
    async with door.door_acp() as connection:
        session = await new_session(connection)
        meta = {"creche/job": "auto-01JBQ7ZZ9D6M0Q4RXT2J8HYVBK"}
        params: dict[str, Any] = {
            "sessionId": session,
            "cwd": CWD,
            "mcpServers": [],
            "prompt": [],
            "_meta": meta,
        }
        answer = await ask(connection, 6, method, **params)

        assert answer["error"]["code"] == RPC_INVALID_PARAMS
        assert door.service.busy(Principal.NOTICEBOARD)["prompts_in_flight"] == 0

    assert len(door.vmm.creates) == 1
    assert door.vmm.lists == []
    assert door.vmm.opened == []


@pytest.mark.slow
async def test_a_creche_meta_notification_reaches_no_stream(door: Door) -> None:
    """A refused notification gets no answer, so the next answer is the
    next request's."""
    async with door.door_acp() as connection:
        session = await new_session(connection)
        cancel: dict[str, Any] = {"sessionId": session, "_meta": {"creche/trigger": {}}}
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": "session/cancel", "params": cancel}
        await connection.send(json.dumps(message))
        answer = await ask(connection, 8, "initialize")

    assert answer["result"] == INITIALIZE_RESPONSE
    assert door.vmm.opened == []


@pytest.mark.slow
async def test_another_meta_key_passes(door: Door) -> None:
    async with door.door_acp() as connection:
        meta = {"agmente/draft": True, "crechex": 1}
        answer = await ask(connection, 3, "session/new", cwd=CWD, mcpServers=[], _meta=meta)

    assert is_session_id(answer["result"]["sessionId"])


@pytest.mark.slow
async def test_session_list_pages_the_host_list(door: Door) -> None:
    """Contract 02 §5.4: newest created first, the vmm cursor as a string."""
    async with door.door_acp() as connection:
        made = [await new_session(connection) for _ in range(3)]
        first = (await ask(connection, 2, "session/list", cwd=CWD))["result"]
        second = (await ask(connection, 3, "session/list", cursor=first["nextCursor"]))["result"]

    assert [one["sessionId"] for one in first["sessions"]] == [made[2], made[1]]
    assert [one["sessionId"] for one in second["sessions"]] == [made[0]]
    assert "nextCursor" not in second
    assert first["sessions"][0]["cwd"] == CWD
    assert first["sessions"][0]["title"] is None
    assert first["sessions"][0]["updatedAt"].endswith("Z")
    assert door.vmm.lists[0].cwd is not None
    assert door.vmm.opened == []


@pytest.mark.slow
@pytest.mark.parametrize("cursor", ["0", "x", "01", "9007199254740992", 5], ids=str)
async def test_a_cursor_no_page_gave_is_invalid_params(door: Door, cursor: object) -> None:
    async with door.door_acp() as connection:
        answer = await ask(connection, 2, "session/list", cursor=cursor)

    assert answer["error"]["code"] == RPC_INVALID_PARAMS
    assert door.vmm.lists == []


@pytest.mark.slow
async def test_a_prompt_reaches_the_stream_of_its_session(door: Door) -> None:
    """Decision CH21-7: the door shakes hands on the new stream, then passes
    the client's line, and each line of the facade back."""
    async with door.door_acp() as connection:
        session = await new_session(connection)
        await connection.send(prompt(9, session))
        lines = [json.loads(await asyncio.wait_for(connection.recv(), 5)) for _ in range(3)]

    assert door.vmm.opened == [session]
    assert door.vmm.facades[session].methods == ["initialize", "session/prompt"]
    assert [json.dumps(one, separators=(",", ":")) for one in lines[:2]] == [
        chunk(session, part) for part in ANSWER_CHUNKS
    ]
    assert lines[2] == {"jsonrpc": "2.0", "id": 9, "result": {"stopReason": "end_turn"}}


@pytest.mark.slow
async def test_an_sbx_era_id_is_not_found_with_no_vmm_call(door: Door) -> None:
    """Decision CH10-9: an id that is no session id never reaches the vmm."""
    async with door.door_acp() as connection:
        answer = await ask(connection, 4, "session/load", sessionId=SBX_ERA_ID, cwd=CWD)

    assert answer["error"]["code"] == RPC_NOT_FOUND
    assert door.vmm.opened == []


@pytest.mark.slow
async def test_an_unknown_session_is_not_found_with_no_boot(door: Door) -> None:
    """Decision CH21-9: the vmm answers `not_found` before 101."""
    unknown = new_session_id()

    async with door.door_acp() as connection:
        answer = await ask(connection, 4, "session/load", sessionId=unknown, cwd=CWD)

    assert answer["error"]["code"] == RPC_NOT_FOUND
    assert door.vmm.opened == [unknown]
    assert door.vmm.facades == {}


@pytest.mark.slow
@pytest.mark.parametrize("code", [BUSY, "host_full", "sandbox_start_failed", "unreachable"])
async def test_a_vmm_refusal_is_an_internal_error_with_its_code(door: Door, code: str) -> None:
    """Decision CH83 rule 1."""
    async with door.door_acp() as connection:
        session = await new_session(connection)
        door.vmm.refuse["acp"] = code
        await connection.send(prompt(5, session))
        answer = json.loads(await asyncio.wait_for(connection.recv(), 5))

    assert answer["error"]["code"] == RPC_INTERNAL_ERROR
    assert answer["error"]["data"] == {"code": code}
    assert door.service.busy(Principal.NOTICEBOARD)["prompts_in_flight"] == 0


@pytest.mark.slow
async def test_a_failed_create_is_an_internal_error(door: Door) -> None:
    door.vmm.refuse["create"] = "unknown_family"

    async with door.door_acp() as connection:
        answer = await ask(connection, 3, "session/new", cwd=CWD, mcpServers=[])

    assert answer["error"]["code"] == RPC_INTERNAL_ERROR
    assert answer["error"]["data"] == {"code": "unknown_family"}


@pytest.mark.slow
async def test_a_bad_handshake_answers_with_no_stream_kept(door: Door) -> None:
    door.vmm.protocol_version = 2

    async with door.door_acp() as connection:
        session = await new_session(connection)
        await connection.send(prompt(5, session))
        answer = json.loads(await asyncio.wait_for(connection.recv(), 5))

    assert answer["error"]["code"] == RPC_INTERNAL_ERROR
    assert answer["error"]["data"] == {"code": HANDSHAKE_FAILED}
    assert door.vmm.channels[session].closed


@pytest.mark.slow
async def test_a_method_with_no_session_is_method_not_found(door: Door) -> None:
    async with door.door_acp() as connection:
        answer = await ask(connection, 6, "authenticate", methodId="x")
        await connection.send(json.dumps({"jsonrpc": "2.0", "method": "session/cancel"}))
        after = await ask(connection, 7, "initialize")

    assert answer["error"]["code"] == RPC_METHOD_NOT_FOUND
    assert after["result"] == INITIALIZE_RESPONSE


@pytest.mark.slow
async def test_two_sessions_of_one_connection_get_two_streams(door: Door) -> None:
    async with door.door_acp() as connection:
        first = await new_session(connection)
        second = await new_session(connection)

        for request_id, session in ((1, first), (2, second)):
            await connection.send(prompt(request_id, session))

            for _ in range(3):
                await asyncio.wait_for(connection.recv(), 5)

    assert door.vmm.opened == [first, second]
    assert set(door.vmm.channels) == {first, second}


@pytest.mark.slow
@pytest.mark.parametrize("door", [PromptMode.LOSE], indirect=True)
async def test_a_stream_that_ends_closes_the_connection(door: Door) -> None:
    """Decision CH19 (6): the client opens the session again with `session/load`."""
    async with door.door_acp() as connection:
        session = await new_session(connection)
        await connection.send(prompt(1, session))
        await asyncio.wait_for(connection.recv(), 5)

        with pytest.raises(ConnectionClosed) as closed:
            await asyncio.wait_for(connection.recv(), 5)

    received = closed.value.rcvd
    assert received is not None
    assert received.code == Close.RELAY_GONE
    assert received.reason == RELAY_ENDED
    assert door.vmm.channels[session].closed


@pytest.mark.slow
@pytest.mark.parametrize("door", [PromptMode.HOLD], indirect=True)
async def test_a_prompt_counts_in_busy_until_its_answer(door: Door) -> None:
    async with door.door_acp() as connection:
        session = await new_session(connection)
        await connection.send(prompt(1, session))

        async with asyncio.timeout(5):
            while session not in door.vmm.facades or not door.vmm.facades[session].held:
                await asyncio.sleep(0.01)

        assert door.service.busy(Principal.NOTICEBOARD)["prompts_in_flight"] == 1
        await door.vmm.settle(session)

        for _ in range(2):
            await asyncio.wait_for(connection.recv(), 5)

        assert door.service.busy(Principal.NOTICEBOARD)["prompts_in_flight"] == 0


async def wait_for(check: Callable[[], bool]) -> None:
    async with asyncio.timeout(5):
        while not check():
            await asyncio.sleep(0.01)


async def held_prompt(door: Door, connection: ClientConnection, request_id: int) -> str:
    """A new session with one prompt that its facade holds."""
    session = await new_session(connection)
    await connection.send(prompt(request_id, session))
    await wait_for(lambda: session in door.vmm.facades and bool(door.vmm.facades[session].held))
    return session


@pytest.mark.slow
@pytest.mark.parametrize("door", [PromptMode.HOLD], indirect=True)
async def test_a_slow_start_of_one_session_holds_no_frame_of_another(door: Door) -> None:
    """A VM start has no outer bound (decision CH50 rule 1). The start of B
    holds the frames of B alone, so the cancel of A reaches its facade."""
    async with door.door_acp() as connection:
        first = await new_session(connection)
        second = await new_session(connection)
        await connection.send(prompt(1, first))

        async with asyncio.timeout(5):
            while first not in door.vmm.facades or not door.vmm.facades[first].held:
                await asyncio.sleep(0.01)

        gate = door.vmm.open_gates[second] = asyncio.Event()

        try:
            await connection.send(prompt(2, second))
            cancel = {"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": first}}
            await connection.send(json.dumps(cancel))

            async with asyncio.timeout(5):
                while "session/cancel" not in door.vmm.facades[first].methods:
                    await asyncio.sleep(0.01)

            held_open = second not in door.vmm.channels
        finally:
            gate.set()

    assert held_open


@pytest.mark.slow
async def test_a_close_in_a_handshake_closes_the_new_stream(door: Door) -> None:
    """A stream in its handshake is in no table yet. A close of the
    connection still closes it, so the vmm claim of the session ends."""
    async with door.door_acp() as connection:
        session = await new_session(connection)
        door.vmm.mute.add(session)
        await connection.send(prompt(1, session))

        async with asyncio.timeout(5):
            while session not in door.vmm.channels:
                await asyncio.sleep(0.01)

    async with asyncio.timeout(5):
        while not door.vmm.channels[session].closed:
            await asyncio.sleep(0.01)


@pytest.mark.slow
@pytest.mark.parametrize("door", [PromptMode.HOLD], indirect=True)
async def test_a_facade_request_holds_the_stream_until_the_client_answers(door: Door) -> None:
    """Contract 02 §5.5 rule 5: a request of the facade is in flight on the
    stream too. Its answer reaches the facade that asked."""
    async with door.door_acp() as connection:
        session = await held_prompt(door, connection, 1)
        channel = door.vmm.channels[session]
        asked = {"jsonrpc": "2.0", "id": 40, "method": "session/request_permission"}
        await channel.feed(json.dumps(asked))
        assert json.loads(await asyncio.wait_for(connection.recv(), 5)) == asked
        await door.vmm.settle(session)

        for _ in range(2):
            await asyncio.wait_for(connection.recv(), 5)

        assert not channel.closing
        answer: dict[str, Any] = {"jsonrpc": "2.0", "id": 40, "result": {}}
        await connection.send(json.dumps(answer))
        await wait_for(lambda: door.vmm.facades[session].received[-1:] == [answer])
        await wait_for(lambda: channel.closed)


@pytest.mark.slow
async def test_an_answered_prompt_closes_its_stream_and_keeps_the_connection(door: Door) -> None:
    """Contract 02 §5.5 rule 5, decision CH21-8: with no request in flight
    the stream closes, so the vmm can snapshot the VM. The next request
    opens a new stream with a new handshake."""
    async with door.door_acp() as connection:
        session = await new_session(connection)

        for request_id in (1, 2):
            await connection.send(prompt(request_id, session))

            for _ in range(3):
                await asyncio.wait_for(connection.recv(), 5)

            await wait_for(lambda: door.vmm.channels[session].closed)
            assert door.vmm.facades[session].methods == ["initialize", "session/prompt"]

        assert (await ask(connection, 3, "initialize"))["result"] == INITIALIZE_RESPONSE

    assert door.vmm.opened == [session, session]


@pytest.mark.slow
@pytest.mark.parametrize("door", [PromptMode.HOLD], indirect=True)
async def test_a_stream_closes_only_after_its_last_answer(door: Door) -> None:
    """A `session/load` counts like a `session/prompt`, and two requests in
    flight keep the stream until both have their answers."""
    async with door.door_acp() as connection:
        session = await held_prompt(door, connection, 1)
        door.vmm.replays[session] = []
        loaded = await ask(connection, 2, "session/load", sessionId=session, cwd=CWD)
        channel = door.vmm.channels[session]

        assert loaded["result"] == {}
        assert not channel.closing
        await door.vmm.settle(session)
        await wait_for(lambda: channel.closed)

    assert door.vmm.opened == [session]


@pytest.mark.slow
async def test_a_notification_alone_closes_its_stream_after_it(door: Door) -> None:
    """A notification opens no call. It reaches the facade, and the stream
    closes with no request in flight."""
    async with door.door_acp() as connection:
        session = await new_session(connection)
        cancel = {"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": session}}
        await connection.send(json.dumps(cancel))
        await wait_for(lambda: session in door.vmm.channels and door.vmm.channels[session].closed)
        await wait_for(lambda: len(door.vmm.facades[session].methods) == 2)

        assert (await ask(connection, 3, "initialize"))["result"] == INITIALIZE_RESPONSE

    assert door.vmm.facades[session].methods == ["initialize", "session/cancel"]
    assert door.vmm.opened == [session]


@pytest.mark.slow
async def test_a_new_stream_waits_for_the_close_of_the_last_one(door: Door) -> None:
    """Decision CH83 rule 4: one holder per session. The vmm answers `busy`
    to an Upgrade while the last stream still closes."""
    gate = asyncio.Event()
    door.vmm.close_gate = gate

    async with door.door_acp() as connection:
        session = await new_session(connection)
        await connection.send(prompt(1, session))

        for _ in range(3):
            await asyncio.wait_for(connection.recv(), 5)

        await wait_for(lambda: door.vmm.channels[session].closing)
        await connection.send(prompt(2, session))
        await asyncio.sleep(0.05)
        assert door.vmm.opened == [session]
        gate.set()
        lines = [json.loads(await asyncio.wait_for(connection.recv(), 5)) for _ in range(3)]

    assert lines[2] == {"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}}
    assert door.vmm.opened == [session, session]


@pytest.mark.slow
@pytest.mark.parametrize("ending", ["\n", "\r\n"], ids=["lf", "crlf"])
async def test_one_line_ending_of_a_client_frame_is_stripped(door: Door, ending: str) -> None:
    """A client can end each frame in one line break. Agmente does."""
    async with door.door_acp() as connection:
        await connection.send(INIT + ending)
        assert json.loads(await asyncio.wait_for(connection.recv(), 5))["id"] == 1


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
        ('{"jsonrpc":"2.0","id":-1e999,"method":"session/load"}', Close.BAD_FRAME),
        ('{"jsonrpc":"2.0","id":' + "9" * 400 + ',"method":"session/load"}', Close.BAD_FRAME),
        (INIT + "\r", Close.BAD_FRAME),
        (INIT + "\n\n", Close.BAD_FRAME),
        ("\n", Close.BAD_FRAME),
    ],
    ids=[
        "over_the_cap",
        "not_json",
        "two_lines",
        "nan",
        "no_envelope",
        "prompt_id_past_double",
        "load_id_past_double",
        "load_id_long_integer",
        "lone_cr",
        "two_lf",
        "only_lf",
    ],
)
async def test_a_bad_frame_closes_the_connection(door: Door, frame: str, code: int) -> None:
    async with door.door_acp() as connection:
        await connection.send(frame)
        assert await close_code(connection) == code


@pytest.mark.slow
async def test_the_acp_bearer_reaches_no_http_route(door: Door) -> None:
    transport = httpx.AsyncHTTPTransport(uds=str(door.socket))
    headers = {"Authorization": f"Bearer {door.tokens[Principal.DOOR_ACP]}"}

    async with httpx.AsyncClient(transport=transport, base_url="http://attendance") as client:
        answer = await client.post(
            "/dispatch/jobs", json={"caller_family": FAMILY}, headers=headers
        )

    assert answer.status_code == HTTP_FORBIDDEN


def test_the_lan_listener_has_no_acp_route(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    service = make_service(config, FakeVmm())
    book = TokenBook(config.state_root)
    paths = {getattr(route, "path", "") for route in build_app(service, book, Listener.LAN).routes}
    socket_paths = {
        getattr(route, "path", "") for route in build_app(service, book, Listener.SOCKET).routes
    }

    assert "/acp/{family}" not in paths
    assert "/acp/{family}" in socket_paths
