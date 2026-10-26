"""An ACP client over WebSocket, in the shape of Panda's web build.

A test plays a person in a browser. The client sends one JSON-RPC 2.0
message in each text frame, as Panda does, to the ACP door of a family.
It sends the bearer in the `Authorization` header (plan Q6). It knows the
methods of ACP v1 that the exit test needs:

    initialize       protocol 1, no `fs`, no `terminal`
    session/new      gives the session id
    session/prompt   gives the stop reason
    session/cancel   a notification
    session/load     gives the updates of the replay
    session/list     gives the result as the agent sent it

The client reads each frame in the thread of the test, while it waits for
the answer to its own request. It keeps each `session/update` as a typed
record in `updates`. It answers each request of the agent, for example
`session/request_permission`, with the error `-32601`: it offers no
capability.

The reader is strict. A frame that is not one JSON-RPC 2.0 object, a frame
over `MAX_FRAME` bytes, a JSON text with a duplicate key or `NaN`, and more
than `MAX_UPDATES` updates each raise `AcpError`. A wait has a deadline.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Final, cast

from proc_strict import strict_object
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus
from websockets.sync.client import ClientConnection, connect

PROTOCOL_VERSION: Final = 1

#: The largest frame that the client takes. A larger one closes the socket.
MAX_FRAME: Final = 4 * 1024 * 1024

#: The most updates that one client keeps.
MAX_UPDATES: Final = 10_000

DEADLINE_S: Final = 30.0

_METHOD_NOT_FOUND: Final = -32601
_UPDATE: Final = "session/update"


class AcpError(Exception):
    """The agent broke the protocol, closed the socket, or did not answer in time."""


class Refused(AcpError):
    """The door answered the WebSocket handshake with an HTTP status, for example 401."""

    def __init__(self, status: int) -> None:
        super().__init__(f"the handshake got HTTP {status}")
        self.status = status


class RpcError(AcpError):
    """The agent answered a request with a JSON-RPC error."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"error {code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class MessageChunk:
    """`agent_message_chunk`, `user_message_chunk` or `agent_thought_chunk`, as text."""

    session_id: str
    kind: str
    text: str


@dataclass(frozen=True, slots=True)
class ToolCall:
    session_id: str
    tool_call_id: str
    title: str
    status: str | None


@dataclass(frozen=True, slots=True)
class ToolCallUpdate:
    """`tool_call_update`. `text` joins the text of each content block."""

    session_id: str
    tool_call_id: str
    status: str | None
    text: str


@dataclass(frozen=True, slots=True)
class SessionInfo:
    session_id: str
    title: str | None
    updated_at: str | None


@dataclass(frozen=True, slots=True)
class OtherUpdate:
    """An update of a kind that the exit test does not read."""

    session_id: str
    kind: str
    body: dict[str, Any]


Update = MessageChunk | ToolCall | ToolCallUpdate | SessionInfo | OtherUpdate

_CHUNKS: Final = frozenset({"agent_message_chunk", "user_message_chunk", "agent_thought_chunk"})


def update_of(params: dict[str, Any]) -> Update:
    """The typed record of the params of one `session/update`."""
    session = _text(params, "sessionId")
    body = _object(params.get("update"))
    kind = _text(body, "sessionUpdate")

    if kind in _CHUNKS:
        return MessageChunk(session, kind, _block_text(body.get("content")))

    if kind == "tool_call":
        status = _optional(body, "status")

        return ToolCall(session, _text(body, "toolCallId"), _text(body, "title"), status)

    if kind == "tool_call_update":
        blocks: object = body.get("content") or []
        texts = [_block_text(_object(one).get("content")) for one in _list(blocks)]

        return ToolCallUpdate(
            session, _text(body, "toolCallId"), _optional(body, "status"), "".join(texts)
        )

    if kind == "session_info_update":
        return SessionInfo(session, _optional(body, "title"), _optional(body, "updatedAt"))

    return OtherUpdate(session, kind, body)


@dataclass(slots=True)
class AcpClient:
    """One WebSocket to an ACP agent, and the updates it sent so far."""

    socket: ClientConnection
    deadline_s: float = DEADLINE_S
    updates: list[Update] = field(default_factory=list[Update])
    _next_id: int = 0

    @classmethod
    @contextmanager
    def connect(cls, url: str, bearer: str | None = None) -> Generator[AcpClient]:
        headers = {"Authorization": f"Bearer {bearer}"} if bearer is not None else None

        try:
            socket = connect(url, additional_headers=headers, max_size=MAX_FRAME, open_timeout=10)
        except InvalidStatus as error:
            raise Refused(error.response.status_code) from error
        except (OSError, ConnectionClosed, InvalidHandshake) as error:
            raise AcpError(f"cannot open {url}: {error}") from error

        with socket:
            yield cls(socket)

    # ------------------------------------------------------------- methods

    def initialize(self) -> dict[str, Any]:
        capabilities = {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}
        params = {"protocolVersion": PROTOCOL_VERSION, "clientCapabilities": capabilities}

        return _object(self.call("initialize", params))

    def new_session(self, cwd: str) -> str:
        return _text(_object(self.call("session/new", {"cwd": cwd, "mcpServers": []})), "sessionId")

    def prompt(self, session_id: str, text: str) -> str:
        """Send one prompt and wait for its stop reason."""
        return self.stop_reason(self.start_prompt(session_id, text))

    def start_prompt(self, session_id: str, text: str) -> int:
        """Send one prompt and return its request id. `stop_reason` waits for it."""
        prompt = [{"type": "text", "text": text}]

        return self.send("session/prompt", {"sessionId": session_id, "prompt": prompt})

    def stop_reason(self, ident: int) -> str:
        return _text(_object(self.result_of(ident)), "stopReason")

    def cancel(self, session_id: str) -> None:
        self._write(
            {"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": session_id}}
        )

    def load_session(self, session_id: str, cwd: str) -> list[Update]:
        """Load one session. Returns the updates that came before the answer."""
        before = len(self.updates)
        self.call("session/load", {"sessionId": session_id, "cwd": cwd, "mcpServers": []})

        return self.updates[before:]

    def list_sessions(self, cursor: str | None = None) -> dict[str, Any]:
        params = {} if cursor is None else {"cursor": cursor}

        return _object(self.call("session/list", params))

    # ------------------------------------------------------------- reading

    def updates_of(self, session_id: str) -> list[Update]:
        return [update for update in self.updates if update.session_id == session_id]

    def agent_text(self, session_id: str) -> str:
        """The text of every `agent_message_chunk` of one session, in arrival order."""
        chunks = self.updates_of(session_id)

        return "".join(
            one.text
            for one in chunks
            if isinstance(one, MessageChunk) and one.kind == "agent_message_chunk"
        )

    def wait_for(self, found: Callable[[Update], bool]) -> Update:
        """Read frames until an update matches. A matching update read earlier counts."""
        deadline = time.monotonic() + self.deadline_s
        seen = 0

        while True:
            for update in self.updates[seen:]:
                if found(update):
                    return update

            seen = len(self.updates)
            self._take(self._read(deadline))

    def call(self, method: str, params: dict[str, Any]) -> object:
        return self.result_of(self.send(method, params))

    def send(self, method: str, params: dict[str, Any]) -> int:
        self._next_id += 1
        self._write({"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params})

        return self._next_id

    def result_of(self, ident: int) -> object:
        """Read frames until the answer to request `ident` comes. Raises on an error answer."""
        deadline = time.monotonic() + self.deadline_s

        while True:
            message = self._read(deadline)

            if message.get("id") == ident and "method" not in message:
                return _result(message)

            self._take(message)

    def _take(self, message: dict[str, Any]) -> None:
        """Keep an update. Refuse a request of the agent. Drop an answer to no request."""
        method = message.get("method")

        if method == _UPDATE:
            if len(self.updates) >= MAX_UPDATES:
                raise AcpError(f"more than {MAX_UPDATES} updates")

            self.updates.append(update_of(_object(message.get("params"))))
        elif method is not None and "id" in message:
            error = {"code": _METHOD_NOT_FOUND, "message": f"the client offers no {method}"}
            self._write({"jsonrpc": "2.0", "id": message["id"], "error": error})

    def _read(self, deadline: float) -> dict[str, Any]:
        left = deadline - time.monotonic()

        if left <= 0:
            raise AcpError(f"no answer in {self.deadline_s} s")

        try:
            frame = self.socket.recv(timeout=left)
        except TimeoutError as error:
            raise AcpError(f"no answer in {self.deadline_s} s") from error
        except ConnectionClosed as error:
            raise AcpError(f"the agent closed the socket: {error}") from error

        if not isinstance(frame, str):
            raise AcpError("a binary frame: ACP sends text frames")

        try:
            message = strict_object(frame)
        except ValueError as error:
            raise AcpError(f"a frame that is no strict JSON object: {error}") from error

        if message.get("jsonrpc") != "2.0":
            raise AcpError("a frame that is no JSON-RPC 2.0 message")

        return message

    def _write(self, message: dict[str, Any]) -> None:
        try:
            self.socket.send(json.dumps(message, ensure_ascii=False, separators=(",", ":")))
        except ConnectionClosed as error:
            raise AcpError(f"the agent closed the socket: {error}") from error


def _result(message: dict[str, Any]) -> object:
    if "error" in message:
        error = _object(message["error"])
        code = error.get("code")

        raise RpcError(code if isinstance(code, int) else 0, str(error.get("message", "")))

    if "result" not in message:
        raise AcpError("an answer with no result and no error")

    return message["result"]


def _object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AcpError(f"expected a JSON object, got {type(value).__name__}")

    return cast("dict[str, Any]", value)


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise AcpError(f"expected a JSON array, got {type(value).__name__}")

    return cast("list[object]", value)


def _text(body: dict[str, Any], key: str) -> str:
    value = body.get(key)

    if not isinstance(value, str):
        raise AcpError(f"{key} is not a string")

    return value


def _optional(body: dict[str, Any], key: str) -> str | None:
    return None if body.get(key) is None else _text(body, key)


def _block_text(block: object) -> str:
    """The text of one content block. A block of another type has none."""
    found = _object(block)

    return _text(found, "text") if found.get("type") == "text" else ""
