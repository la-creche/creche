"""The ACP door: one WebSocket connection, every session of one family.

One door connection serves every session of its family (contract 02
§5.1 rule 4). attendance answers three methods itself, from the host
session list, with no boot (decision CH21-7). It routes each other request
and notification by its `sessionId`, over one vmm ACP stream per session
(decisions CH20, CH33 rule 7):

    initialize    -> INITIALIZE_RESPONSE
    session/new   -> mint an id -> vmm create, origin door -> {"sessionId"}
    session/list  -> vmm list -> {"sessions", "nextCursor"}
    each other    -> the vmm ACP stream of its sessionId -> the facade of that VM

A message whose `params._meta` holds a key that starts with `creche/`
gets `invalidParams` (-32602) and reaches no stream (contract 02 §5.1 rule
5, decision CH21-4). The typed origin of the row carries what those keys
carried, so a door client sets none of them.

An id that is not a session id, such as an sbx-era id, gets ACP
`resourceNotFound` (-32002) before attendance calls the vmm (decision
CH10-9). An id with no row of the family gets it from the vmm `not_found`,
with no boot (decision CH21-9). Each other refusal of the vmm is
`internalError` (-32603), with the vmm code as `data.code` (decision CH83
rule 1).

One WebSocket text frame is one JSON-RPC line. A client frame may end in
one line ending, one LF or one CR LF. The door strips it first. Each frame
and each stream line then gets the envelope check: UTF-8 under the size
cap, one line, strict JSON, an object with `"jsonrpc": "2.0"`. Each other
line break closes the connection. A routed frame reaches its stream as its
exact bytes, plus one LF. A stream line reaches the WebSocket as its exact
text. Nothing is translated.

Before the first frame of a session, the door runs the handshake on its new
stream: `initialize`, with the answer kept by the door. The facade sends no
beat deadline in a VM (decision CH80), so the door sends no beat.

A stream that ends closes the connection with `Close.RELAY_GONE`, and the
client opens the session again with `session/load` (decision CH19 (6)).

The frames of one session keep their order, and no session waits for
another. The door reads each frame and puts it in the lane of its session:
the `sessionId` of a request or a notification, or the session whose facade
asked, for a client answer. Each other frame goes to the one lane of the
connection. Each lane works its frames one at a time. So a VM start of one
session, which has no outer bound (decision CH50 rule 1), holds only the
frames of that session. A `session/cancel` of another session and a client
answer to another facade go through.

The door counts the `session/prompt` requests in flight on each
connection, for `GET /busy`. A client request of that method opens one,
and the stream's answer with the same id closes it. A connection that ends
closes each of its own. The count reads the lines and changes none.

The facade is JavaScript. It reads each JSON number as a double and writes
that double in its answer, so the count keys a number id by its double:
`1`, `1.0` and `1e0` are one id. A string id stays apart from a number id.
A prompt with a number id past the double range closes with
`Close.BAD_FRAME`, because its answer cannot carry the same id.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import re
from collections.abc import Generator
from enum import IntEnum
from typing import Any, Final, Literal

from creche_wire import (
    CreateSession,
    Door,
    DoorOrigin,
    FamilyName,
    ListCursor,
    ListSessions,
    SessionCwd,
    SessionId,
    SessionOrigin,
    SessionRow,
    UnixMillis,
)
from starlette.websockets import WebSocket

from .acp_link import (
    JSONRPC,
    MAX_ACP_LINE_BYTES,
    METHOD_INITIALIZE,
    METHOD_PROMPT,
    PROTOCOL_VERSION,
    AcpHandshakeError,
    Malformed,
    handshake,
    loads_strict,
)
from .atomic import as_array, as_object
from .channel import Channel, ChannelClosed
from .clock import from_ms, now_ms, rfc3339_ms
from .errors import ApiError
from .ids import is_session_cwd, is_session_id, new_session_id
from .tasks import report_failure
from .vmm import NOT_FOUND, Vmm, VmmRefused

#: The cap for both directions.
MAX_FRAME_BYTES = MAX_ACP_LINE_BYTES

#: A refusal before the pipe opens closes with 4000 plus the HTTP status of
#: contract 02 §14: 4401 for a bad bearer, 4403 for a door that may not.
_REFUSAL_BASE = 4000

_LINE_BREAKS = ("\n", "\r")

#: The one line ending that a client frame may carry. CR LF is tried first.
_LINE_ENDINGS = ("\r\n", "\n")

#: The reason of a `Close.RELAY_GONE`. The door-acp service sends its own
#: 1001 too, so the reason says that the stream to the facade is what ended.
RELAY_ENDED = "the relay ended"

METHOD_NEW = "session/new"
METHOD_LIST = "session/list"

#: The prefix of a creche key of `_meta`. A door client sends none
#: (decision CH21-4).
CRECHE_META_PREFIX: Final = "creche/"

#: The methods that attendance answers itself (decision CH21-7).
_ANSWERED: Final = frozenset({METHOD_INITIALIZE, METHOD_NEW, METHOD_LIST})

#: The one answer to `initialize` (decision CH21-7, contract 02 §5.2). The
#: facade answers the same one to the handshake of each stream.
INITIALIZE_RESPONSE: Final[dict[str, Any]] = {
    "protocolVersion": PROTOCOL_VERSION,
    "agentCapabilities": {"loadSession": True, "sessionCapabilities": {"list": {}}},
    "authMethods": [],
}

#: JSON-RPC's and ACP's error codes.
RPC_METHOD_NOT_FOUND = -32601
RPC_INVALID_PARAMS = -32602
RPC_INTERNAL_ERROR = -32603
RPC_NOT_FOUND = -32002

#: The `data.code` of a stream whose handshake failed.
#:
#: CONTRACT-QUESTION: decision CH83 rule 1 names the JSON-RPC error of a
#: vmm refusal, and no row names the error of a facade that answers the
#: handshake with nothing usable after 101. The reading here: the same
#: `internalError`, with this code of contract 02 §14 table 2 as `data.code`.
#: Another reading changes this one word.
HANDSHAKE_FAILED = "protocol_violation"

#: How many frames one lane holds before the door reads no more frames.
#: A lane works its frames as fast as its stream takes them, so only a lane
#: that waits for a VM start fills. Past the cap, the read of the next frame
#: waits, as each frame waited before the door had lanes.
LANE_DEPTH: Final = 64

#: The lane of each frame that names no session: `initialize`,
#: `session/new`, `session/list`, and a client answer to no facade.
_DOOR_LANE: Final = ""

#: ACP's string cursor of `session/list`: the decimal text of the vmm's
#: `ListCursor`, 1 to 2^53 - 1 (contract 02 §5.4).
_CURSOR_RE = re.compile(r"^[1-9][0-9]{0,15}\Z")
_CURSOR_MAX = 2**53 - 1


class Close(IntEnum):
    """Why the pipe closed, as a WebSocket close code (RFC 6455 §7.4.1)."""

    CLIENT_GONE = 1000
    RELAY_GONE = 1001
    BAD_FRAME = 1007
    TOO_LARGE = 1009


class _OpenPrompts:
    """The prompts of one connection that wait for their answer."""

    def __init__(self) -> None:
        self.ids: set[_RpcIdKey] = set()

    def sent(self, record: dict[str, Any]) -> Close | None:
        """A client request. None when it may pass."""
        if record.get("method") != METHOD_PROMPT or "id" not in record:
            return None

        key = _rpc_id_key(record["id"])

        if key is None:
            return Close.BAD_FRAME

        self.ids.add(key)
        return None

    def refused(self, record: dict[str, Any]) -> None:
        """A client request that attendance refused. It reached no stream."""
        if record.get("method") == METHOD_PROMPT and "id" in record:
            key = _rpc_id_key(record["id"])

            if key is not None:
                self.ids.discard(key)

    def answered(self, text: str) -> None:
        """A stream line. A prompt answer holds `stopReason` or `error`."""
        if not self.ids or ("stopReason" not in text and '"error"' not in text):
            return

        record = as_object(loads_strict(text)) or {}

        if "method" not in record and "id" in record:
            key = _rpc_id_key(record["id"])

            if key is not None:
                self.ids.discard(key)


class DoorPrompts:
    """The `session/prompt` requests in flight on every door connection."""

    def __init__(self) -> None:
        self._open: list[_OpenPrompts] = []

    @property
    def in_flight(self) -> int:
        return sum(len(one.ids) for one in self._open)

    @contextlib.contextmanager
    def connection(self) -> Generator[_OpenPrompts]:
        """The count of one connection, for as long as it lives."""
        one = _OpenPrompts()
        self._open.append(one)

        try:
            yield one
        finally:
            self._open.remove(one)


#: A JSON-RPC id as the facade sees it: a number as its double, each other
#: value as its JSON text.
type _RpcIdKey = tuple[Literal["number"], float] | tuple[Literal["json"], str]


def _rpc_id_key(value: object) -> _RpcIdKey | None:
    """The key of one id. None for a number past the double range."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return ("json", json.dumps(value, separators=(",", ":")))

    try:
        double = float(value)
    except OverflowError:
        return None

    return ("number", double) if math.isfinite(double) else None


def refusal_code(error: ApiError) -> int:
    return _REFUSAL_BASE + error.status


def strip_line_end(text: str) -> str:
    """A client frame without its one line ending: one LF or one CR LF.

    Some clients end each frame in a line break. Agmente does. Each other
    line break stays, so `check_frame` refuses it. The door adds its own LF
    for the stream, so an ending that stayed would also make an empty line.
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
        record = as_object(loads_strict(text))
    except Malformed:
        return Close.BAD_FRAME

    if record is None or record.get("jsonrpc") != JSONRPC:
        return Close.BAD_FRAME

    return None


class _Refusal(Exception):
    """A JSON-RPC error that answers one client request."""

    def __init__(self, code: int, message: str, vmm_code: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.vmm_code = vmm_code


class _Door:
    """One door connection: its streams, its writes and its answers."""

    def __init__(self, websocket: WebSocket, family: str, vmm: Vmm, prompts: _OpenPrompts) -> None:
        self._websocket = websocket
        self._family = family
        self._vmm = vmm
        self._prompts = prompts
        self._write = asyncio.Lock()
        self._streams: dict[str, Channel] = {}
        self._readers: dict[str, asyncio.Task[Close]] = {}
        #: The session of each request a facade sent to the client, by id,
        #: so that the client's answer reaches the same stream.
        self._asked: dict[_RpcIdKey, str] = {}
        #: The frames of each lane, and the task that works them.
        self._lanes: dict[str, asyncio.Queue[str]] = {}
        self._workers: list[asyncio.Task[None]] = []
        self.ended: asyncio.Future[Close] = asyncio.get_running_loop().create_future()

    async def from_client(self) -> Close:
        while True:
            message = await self._websocket.receive()
            text: object = message.get("text")

            if message["type"] == "websocket.disconnect":
                return Close.CLIENT_GONE

            if not isinstance(text, str):
                return Close.BAD_FRAME

            line = strip_line_end(text)
            refused = check_frame(line)

            if refused is not None:
                return refused

            await self._lane(self._lane_of(line)).put(line)

    def _lane_of(self, line: str) -> str:
        """The session whose frames keep their order with this one."""
        record = as_object(loads_strict(line)) or {}

        if "method" not in record:
            key = _rpc_id_key(record.get("id"))
            return self._asked.get(key, _DOOR_LANE) if key is not None else _DOOR_LANE

        params = as_object(record.get("params"))
        session = params.get("sessionId") if params is not None else None
        return session if isinstance(session, str) and session else _DOOR_LANE

    def _lane(self, key: str) -> asyncio.Queue[str]:
        """The frames of one lane. The first frame starts its worker."""
        lane = self._lanes.get(key)

        if lane is None:
            lane = asyncio.Queue[str](LANE_DEPTH)
            self._lanes[key] = lane
            worker = asyncio.create_task(self._work(lane), name="acp door lane")
            worker.add_done_callback(report_failure)
            self._workers.append(worker)

        return lane

    async def _work(self, lane: asyncio.Queue[str]) -> None:
        """Route each frame of one lane in turn. A frame that closes the
        connection ends the lane and the connection."""
        while True:
            line = await lane.get()

            try:
                refused = await self._route(line)
            except Exception:
                # A fault of the door ends the connection, as it did when
                # the read of the frames routed each one itself.
                if not self.ended.done():
                    self.ended.set_result(Close.RELAY_GONE)

                raise

            if refused is not None:
                if not self.ended.done():
                    self.ended.set_result(refused)

                return

    async def _route(self, line: str) -> Close | None:
        record = as_object(loads_strict(line)) or {}
        method = record.get("method")

        if not isinstance(method, str):
            return await self._to_asker(record, line)

        refused = self._prompts.sent(record)

        if refused is not None:
            return refused

        try:
            answer = await self._answer(method, record, line)
        except _Refusal as refusal:
            self._prompts.refused(record)
            await self._refuse(record, refusal)
            return None
        except ChannelClosed:
            return Close.RELAY_GONE

        if answer is not None and "id" in record:
            await self._send({"jsonrpc": JSONRPC, "id": record["id"], "result": answer})

        return None

    async def _answer(self, method: str, record: dict[str, Any], line: str) -> object:
        """The result attendance answers, or None for a routed line. A
        notification of a method that attendance answers does nothing."""
        params = as_object(record.get("params"))
        _check_meta(params)

        if method in _ANSWERED and "id" not in record:
            return None

        if method == METHOD_INITIALIZE:
            return INITIALIZE_RESPONSE

        if method == METHOD_NEW:
            return await self._new_session(params)

        if method == METHOD_LIST:
            return await self._list_sessions(params)

        session = params.get("sessionId") if params is not None else None

        if not isinstance(session, str):
            raise _Refusal(RPC_METHOD_NOT_FOUND, f"{method} names no session")

        stream = await self._stream(session)
        await stream.send(line + "\n")
        return None

    async def _new_session(self, params: dict[str, Any] | None) -> dict[str, Any]:
        """Contract 02 §5.3: check the `cwd`, mint an id, create the row.

        The family decides the tools and the work directory of the agent, so
        a client MCP server or an additional directory is refused, as the
        facade refuses them."""
        if params is None:
            raise _Refusal(RPC_INVALID_PARAMS, "session/new has no params")

        cwd = params.get("cwd")

        if not isinstance(cwd, str) or not is_session_cwd(cwd):
            raise _Refusal(RPC_INVALID_PARAMS, "cwd is not an absolute path of a session")

        servers = as_array(params.get("mcpServers"))

        if servers is None or servers:
            raise _Refusal(RPC_INVALID_PARAMS, "the facade takes no client MCP server")

        if as_array(params.get("additionalDirectories")):
            raise _Refusal(RPC_INVALID_PARAMS, "the facade takes no additional directory")

        session = new_session_id()
        row = CreateSession(
            created_at=UnixMillis(now_ms()),
            cwd=SessionCwd(cwd),
            family=FamilyName(self._family),
            id=SessionId(session),
            origin=SessionOrigin(DoorOrigin(door=Door())),
        )

        try:
            await self._vmm.create(row)
        except VmmRefused as error:
            raise _vmm_refusal(error) from error

        return {"sessionId": session}

    async def _list_sessions(self, params: dict[str, Any] | None) -> dict[str, Any]:
        """Contract 02 §5.4: one page of the host list, newest created first."""
        fields = params if params is not None else {}
        cwd = fields.get("cwd")
        cursor = fields.get("cursor")

        if cwd is not None and not (isinstance(cwd, str) and is_session_cwd(cwd)):
            raise _Refusal(RPC_INVALID_PARAMS, "cwd is not an absolute path of a session")

        query = ListSessions(
            cursor=_list_cursor(cursor),
            cwd=SessionCwd(cwd) if isinstance(cwd, str) else None,
            family=FamilyName(self._family),
        )

        try:
            page = await self._vmm.list_page(query)
        except VmmRefused as error:
            raise _vmm_refusal(error) from error

        answer: dict[str, Any] = {"sessions": [_session_info(row) for row in page.rows]}

        if page.next is not None:
            answer["nextCursor"] = str(page.next.root)

        return answer

    async def _stream(self, session: str) -> Channel:
        """The stream of `session`, opened and shaken hands with once."""
        stream = self._streams.get(session)

        if stream is not None:
            return stream

        if not is_session_id(session):
            raise _Refusal(RPC_NOT_FOUND, f"no session {session}")

        try:
            stream = await self._vmm.open_acp(self._family, session, MAX_FRAME_BYTES)
        except VmmRefused as error:
            raise _vmm_refusal(error) from error

        try:
            await handshake(stream)
        except (AcpHandshakeError, ChannelClosed) as error:
            await stream.close()
            raise _Refusal(RPC_INTERNAL_ERROR, str(error), HANDSHAKE_FAILED) from error
        except BaseException:
            # A close of the connection cancels the lane here. The stream is
            # in no table yet, so `close` cannot reach it, and an open
            # stream keeps the claim of the session on the vmm.
            await stream.close()
            raise

        self._streams[session] = stream
        reader = asyncio.create_task(self._from_stream(session, stream), name="acp door out")
        reader.add_done_callback(report_failure)
        reader.add_done_callback(self._stream_ended)
        self._readers[session] = reader
        return stream

    async def _from_stream(self, session: str, stream: Channel) -> Close:
        while True:
            try:
                raw = await stream.receive()
            except ChannelClosed:
                return Close.RELAY_GONE

            if raw is None:
                return Close.RELAY_GONE

            if raw.text is None:
                return Close.TOO_LARGE if raw.size > MAX_FRAME_BYTES else Close.BAD_FRAME

            refused = check_frame(raw.text)

            if refused is not None:
                return refused

            self._note_ask(session, raw.text)
            self._prompts.answered(raw.text)
            await self._send_text(raw.text)

    def _stream_ended(self, task: asyncio.Task[Close]) -> None:
        if self.ended.done():
            return

        if task.cancelled():
            return

        error = task.exception()
        self.ended.set_result(Close.RELAY_GONE if error is not None else task.result())

    def _note_ask(self, session: str, text: str) -> None:
        """A request of the facade to the client: its answer goes back here."""
        if '"method"' not in text:
            return

        record = as_object(loads_strict(text)) or {}

        if "method" in record and "id" in record:
            key = _rpc_id_key(record["id"])

            if key is not None:
                self._asked[key] = session

    async def _to_asker(self, record: dict[str, Any], line: str) -> Close | None:
        """A client answer goes to the stream whose facade asked. An answer
        to no request of a facade is dropped."""
        key = _rpc_id_key(record.get("id"))
        session = self._asked.pop(key, None) if key is not None else None
        stream = self._streams.get(session) if session is not None else None

        if stream is None:
            return None

        try:
            await stream.send(line + "\n")
        except ChannelClosed:
            return Close.RELAY_GONE

        return None

    async def _refuse(self, record: dict[str, Any], refusal: _Refusal) -> None:
        """A notification gets no answer, so a refused one is dropped."""
        if "id" not in record:
            return

        error: dict[str, Any] = {"code": refusal.code, "message": refusal.message}

        if refusal.vmm_code is not None:
            error["data"] = {"code": refusal.vmm_code}

        await self._send({"jsonrpc": JSONRPC, "id": record["id"], "error": error})

    async def _send(self, message: dict[str, Any]) -> None:
        await self._send_text(json.dumps(message, separators=(",", ":"), ensure_ascii=False))

    async def _send_text(self, text: str) -> None:
        async with self._write:
            await self._websocket.send_text(text)

    async def close(self) -> None:
        tasks: list[asyncio.Task[object]] = [*self._workers, *self._readers.values()]

        for task in tasks:
            task.cancel()

        await asyncio.gather(*tasks, return_exceptions=True)

        for stream in self._streams.values():
            await stream.close()


async def serve(websocket: WebSocket, family: str, vmm: Vmm, prompts: DoorPrompts) -> None:
    """Serve one door connection until the client or a stream ends. Then
    close each stream."""
    with prompts.connection() as open_prompts:
        door = _Door(websocket, family, vmm, open_prompts)
        client = asyncio.create_task(door.from_client(), name="acp door in")
        client.add_done_callback(report_failure)

        try:
            done, _ = await asyncio.wait({client, door.ended}, return_when=asyncio.FIRST_COMPLETED)
            client.cancel()
            await asyncio.gather(client, return_exceptions=True)
            why = _why(done)
        finally:
            client.cancel()
            await door.close()

        await _close_client(websocket, why)


def _why(done: set[asyncio.Future[Close]]) -> Close:
    finished = done.pop()

    if finished.cancelled() or finished.exception() is not None:
        return Close.RELAY_GONE

    return finished.result()


async def _close_client(websocket: WebSocket, why: Close) -> None:
    if why is Close.CLIENT_GONE:
        return

    reason = RELAY_ENDED if why is Close.RELAY_GONE else None

    # The client can leave in the same moment. A second close is no error.
    with contextlib.suppress(RuntimeError, OSError):
        await websocket.close(code=why, reason=reason)


def _check_meta(params: dict[str, Any] | None) -> None:
    """Contract 02 §5.1 rule 5: a door client sets no creche key of
    `_meta`. Such a key named a job or its trigger in minor 13. The origin
    of the row names them now, and a door client names neither (decision
    CH21-4). A `_meta` that is no object holds no key."""
    meta = as_object(params.get("_meta")) if params is not None else None

    if meta is not None and any(key.startswith(CRECHE_META_PREFIX) for key in meta):
        raise _Refusal(RPC_INVALID_PARAMS, "a door client sets no creche key of _meta")


def _list_cursor(value: object) -> ListCursor | None:
    """The vmm cursor of an ACP cursor, which a page of this door gave."""
    if value is None:
        return None

    if not isinstance(value, str) or not _CURSOR_RE.match(value) or int(value) > _CURSOR_MAX:
        raise _Refusal(RPC_INVALID_PARAMS, "the cursor is not one that session/list gave")

    return ListCursor(int(value))


def _session_info(row: SessionRow) -> dict[str, Any]:
    """Contract 02 §5.4 rule 4: one ACP session of one host row."""
    return {
        "sessionId": row.id.root,
        "cwd": row.cwd.root,
        "title": row.title.root if row.title is not None else None,
        "updatedAt": rfc3339_ms(from_ms(row.updated_at.root)),
    }


def _vmm_refusal(error: VmmRefused) -> _Refusal:
    """Decision CH21-9: `not_found` is `resourceNotFound`. Decision CH83
    rule 1: each other refusal is `internalError` with the vmm code."""
    if error.code == NOT_FOUND:
        return _Refusal(RPC_NOT_FOUND, "no such session")

    return _Refusal(RPC_INTERNAL_ERROR, f"the vmm refused: {error.code}", error.code)
