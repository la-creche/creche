"""The ACP client of the exit test, against a scripted agent in this process.

The agent here is no service and no stand-in: it is the peer of a client
under test. Each test gives it one function, which reads the requests of
the client and writes what an ACP agent writes.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from acp_client import (
    MAX_FRAME,
    AcpClient,
    AcpError,
    MessageChunk,
    Refused,
    RpcError,
    SessionInfo,
    ToolCall,
    ToolCallUpdate,
)
from proc_harness import LOOPBACK
from websockets.datastructures import Headers
from websockets.http11 import Request, Response
from websockets.sync.server import Server, ServerConnection, serve

#: An obvious fixture, never a credential.
BEARER = "FIXTURE-DOOR-BEARER"
SESSION = "s1"
CWD = "/work"
UNAUTHORIZED = 401

Agent = Callable[["Peer"], None]


class Peer:
    """The agent side of one connection: read a request, write a message."""

    def __init__(self, socket: ServerConnection) -> None:
        self.socket = socket

    def take(self, method: str) -> dict[str, Any]:
        message = json.loads(self.socket.recv(timeout=10))
        assert message["method"] == method, message

        return message

    def answer(self, message: dict[str, Any], result: object) -> None:
        self.write({"jsonrpc": "2.0", "id": message["id"], "result": result})

    def update(self, update: dict[str, Any]) -> None:
        params = {"sessionId": SESSION, "update": update}
        self.write({"jsonrpc": "2.0", "method": "session/update", "params": params})

    def write(self, message: dict[str, Any]) -> None:
        self.socket.send(json.dumps(message))


@pytest.fixture
def agent() -> Iterator[Callable[[Agent], str]]:
    """Serve one scripted agent on a loopback port. Gives the URL."""
    servers: list[tuple[Server, threading.Thread]] = []

    def start(script: Agent) -> str:
        def handler(socket: ServerConnection) -> None:
            script(Peer(socket))

        def check(_: ServerConnection, request: Request) -> Response | None:
            if request.headers.get("Authorization") == f"Bearer {BEARER}":
                return None

            return Response(UNAUTHORIZED, "Unauthorized", Headers())

        server = serve(handler, LOOPBACK, 0, process_request=check, max_size=2 * MAX_FRAME)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))

        return f"ws://{LOOPBACK}:{server.socket.getsockname()[1]}/acp/fam-a"

    yield start

    for server, thread in servers:
        server.shutdown()
        thread.join(10)


def _turn(peer: Peer) -> None:
    """Answer `initialize`, `session/new`, then one prompt with four updates."""
    hello = peer.take("initialize")
    assert hello["params"]["protocolVersion"] == 1
    assert hello["params"]["clientCapabilities"]["terminal"] is False
    peer.answer(hello, {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}})
    peer.answer(peer.take("session/new"), {"sessionId": SESSION})
    prompt = peer.take("session/prompt")
    peer.update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "he"}})
    peer.update(
        {"sessionUpdate": "tool_call", "toolCallId": "t1", "title": "echo", "status": "pending"}
    )
    output = [{"type": "content", "content": {"type": "text", "text": '{"a":1}'}}]
    peer.update(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "t1",
            "status": "completed",
            "content": output,
        }
    )
    peer.update(
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "llo"}}
    )
    peer.update(
        {"sessionUpdate": "session_info_update", "title": "hi", "updatedAt": "2026-10-25T00:00:00Z"}
    )
    peer.answer(prompt, {"stopReason": "end_turn"})


def test_acp_client_runs_a_turn_and_keeps_typed_updates(agent: Callable[[Agent], str]) -> None:
    with AcpClient.connect(agent(_turn), BEARER) as client:
        hello = client.initialize()
        session = client.new_session(CWD)
        stop = client.prompt(session, "hi")

    assert hello["agentCapabilities"]["loadSession"] is True
    assert stop == "end_turn"
    assert client.agent_text(SESSION) == "hello"
    assert client.updates[1:4] == [
        ToolCall(SESSION, "t1", "echo", "pending"),
        ToolCallUpdate(SESSION, "t1", "completed", '{"a":1}'),
        MessageChunk(SESSION, "agent_message_chunk", "llo"),
    ]
    assert client.updates[4] == SessionInfo(SESSION, "hi", "2026-10-25T00:00:00Z")


def test_acp_client_sends_the_bearer_in_a_header(agent: Callable[[Agent], str]) -> None:
    url = agent(_turn)

    with pytest.raises(Refused) as raised, AcpClient.connect(url, "another"):
        pass

    assert raised.value.status == UNAUTHORIZED


def test_acp_client_cancels_a_prompt_that_runs(agent: Callable[[Agent], str]) -> None:
    def script(peer: Peer) -> None:
        prompt = peer.take("session/prompt")
        peer.update(
            {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "a"}}
        )
        assert peer.take("session/cancel")["params"] == {"sessionId": SESSION}
        peer.answer(prompt, {"stopReason": "cancelled"})

    with AcpClient.connect(agent(script), BEARER) as client:
        ident = client.start_prompt(SESSION, "hi")
        client.wait_for(lambda update: isinstance(update, MessageChunk))
        client.cancel(SESSION)

        assert client.stop_reason(ident) == "cancelled"


def test_acp_client_refuses_each_request_of_the_agent(agent: Callable[[Agent], str]) -> None:
    refused: list[dict[str, Any]] = []

    def script(peer: Peer) -> None:
        prompt = peer.take("session/prompt")
        params: dict[str, object] = {
            "sessionId": SESSION,
            "toolCall": {"toolCallId": "t1"},
            "options": [],
        }
        peer.write(
            {"jsonrpc": "2.0", "id": 99, "method": "session/request_permission", "params": params}
        )
        refused.append(json.loads(peer.socket.recv(timeout=10)))
        peer.answer(prompt, {"stopReason": "end_turn"})

    with AcpClient.connect(agent(script), BEARER) as client:
        assert client.prompt(SESSION, "hi") == "end_turn"

    assert refused[0]["id"] == 99
    assert refused[0]["error"]["code"] == -32601


def test_acp_client_loads_a_session_and_lists_the_sessions(agent: Callable[[Agent], str]) -> None:
    listed = {"sessions": [{"sessionId": SESSION, "cwd": CWD, "title": "hi"}]}

    def script(peer: Peer) -> None:
        load = peer.take("session/load")
        assert load["params"] == {"sessionId": SESSION, "cwd": CWD, "mcpServers": []}
        peer.update(
            {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "hi"}}
        )
        peer.update(
            {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "yo"}}
        )
        peer.answer(load, None)
        peer.answer(peer.take("session/list"), listed)

    with AcpClient.connect(agent(script), BEARER) as client:
        replay = client.load_session(SESSION, CWD)
        sessions = client.list_sessions()

    assert replay == [
        MessageChunk(SESSION, "user_message_chunk", "hi"),
        MessageChunk(SESSION, "agent_message_chunk", "yo"),
    ]
    assert sessions == listed


def test_acp_client_raises_the_error_answer_of_the_agent(agent: Callable[[Agent], str]) -> None:
    def script(peer: Peer) -> None:
        load = peer.take("session/load")
        peer.write({"jsonrpc": "2.0", "id": load["id"], "error": {"code": -32002, "message": "no"}})

    with AcpClient.connect(agent(script), BEARER) as client, pytest.raises(RpcError) as raised:
        client.load_session(SESSION, CWD)

    assert raised.value.code == -32002


@pytest.mark.parametrize(
    "frame",
    [
        '{"jsonrpc":"2.0","id":1,"id":1,"result":{}}',
        '{"jsonrpc":"2.0","id":1,"result":NaN}',
        "[1]",
        '{"id":1,"result":{}}',
        "x" * (MAX_FRAME + 1),
    ],
    ids=["duplicate key", "NaN", "array", "no jsonrpc", "over the cap"],
)
def test_acp_client_refuses_a_frame_that_breaks_the_rules(
    agent: Callable[[Agent], str], frame: str
) -> None:
    def script(peer: Peer) -> None:
        peer.take("session/list")
        peer.socket.send(frame)

    with AcpClient.connect(agent(script), BEARER) as client, pytest.raises(AcpError):
        client.list_sessions()
