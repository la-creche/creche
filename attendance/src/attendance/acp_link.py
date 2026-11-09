"""The ACP link to the facade of one session, over its vmm ACP stream.

The vmm serves one VM per session (decision CH1), and one ACP stream per
session (decisions CH20, CH33 rule 7). A job opens the stream of its own
session and drives it with this link:

    initialize (the handshake) --> session/prompt --> stop reason
                                   session/cancel (past the deadline)

The facade in a VM opens its one session from the handshake id of guest
init (decision CH21-8). So this link never sends `session/new` or
`session/load`. The vmm owns the stop of the facade (decision CH19), so
this link sends no beat and no shutdown: a close ends the stream only.

Every byte from the facade is a claim. A line must be one strict JSON
object with a JSON-RPC 2.0 envelope, and it must answer a request that this
link sent, or update the session of the link. Any other line ends the link,
and each call in flight fails with `AcpLinkLost`.

This link parses the envelope and the answers to its own calls only. The
schema of the pinned SDK sits in `acp/schema-v1.json`, and the tests tie
these shapes to it. Of the updates of the session, only the
`agent_message_chunk` text is kept.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from .atomic import as_object
from .channel import Channel, ChannelClosed
from .lines import cut_bytes
from .tasks import report_failure

_LOG = logging.getLogger("attendance")

ACP_SDK_VERSION = "2.0.2"
ACP_SCHEMA_SHA256 = "4bcd1d2051ff7ed379bd9b234fb9b09c5ab9245478698e9fab805bd9b52a33a6"
SCHEMA_FILE = Path(__file__).parent / "acp" / "schema-v1.json"

PROTOCOL_VERSION = 1
JSONRPC = "2.0"
METHOD_INITIALIZE = "initialize"
METHOD_PROMPT = "session/prompt"
METHOD_CANCEL = "session/cancel"
METHOD_UPDATE = "session/update"
UPDATE_MESSAGE_CHUNK = "agent_message_chunk"
CLIENT_NAME = "attendance"
CLIENT_VERSION = "0.1.0"

#: The cap of one ACP line, in both directions. The ACP door uses it for
#: each frame.
MAX_ACP_LINE_BYTES = 1_048_576

#: How long the facade has to answer the handshake. The vmm answers 101
#: after the first `ControlReady` (decision CH34 rule 7), so the facade
#: listens by then. The number is the one of the sbx link.
HANDSHAKE_DEADLINE_S = 90

#: The id of the handshake. It is the first line on each stream, and its
#: answer comes before any other line passes, so no client id meets it.
HANDSHAKE_ID = 0

#: The most text one prompt collects. The rest of the chunks are dropped.
MAX_ANSWER_BYTES = 1_048_576

_RESPONSE_KEYS = frozenset({"jsonrpc", "id", "result", "error"})
_NOTICE_KEYS = frozenset({"jsonrpc", "method", "params"})
_ERROR_KEYS = frozenset({"code", "message", "data"})


class AcpHandshakeError(Exception):
    """The facade answered `initialize` with nothing usable."""


class AcpLinkLost(Exception):
    """The link ended before the facade answered this call."""


class AcpCallFailed(Exception):
    """The facade answered this call with a JSON-RPC error."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class StopReason(StrEnum):
    """ACP v1's stop reasons of a prompt turn."""

    END_TURN = "end_turn"
    MAX_TOKENS = "max_tokens"
    MAX_TURN_REQUESTS = "max_turn_requests"
    REFUSAL = "refusal"
    CANCELLED = "cancelled"


class LinkState(StrEnum):
    STARTING = "starting"
    READY = "ready"
    LOST = "lost"
    CLOSED = "closed"


@dataclass(slots=True, frozen=True)
class AgentAnswer:
    """What the facade's `initialize` answer says, after validation."""

    protocol_version: int
    load_session: bool
    session_list: bool


@dataclass(slots=True, frozen=True)
class RpcError:
    code: int
    message: str


@dataclass(slots=True, frozen=True)
class Response:
    """One JSON-RPC response. Exactly one of `result` and `error` is set."""

    id: int
    result: object = None
    error: RpcError | None = None


@dataclass(slots=True, frozen=True)
class Notice:
    """One JSON-RPC notification from the facade."""

    method: str
    params: dict[str, Any]


@dataclass(slots=True, frozen=True)
class PromptEnd:
    """How one `session/prompt` ended, and the text it streamed."""

    stop_reason: StopReason
    text: str


@dataclass(slots=True)
class _Collect:
    """The text of one prompt in flight."""

    parts: list[str] = field(default_factory=list[str])
    size: int = 0

    def add(self, text: str) -> None:
        room = MAX_ANSWER_BYTES - self.size

        if room <= 0:
            return

        kept = cut_bytes(text, room)
        self.parts.append(kept)
        self.size += len(kept.encode("utf-8", "surrogatepass"))


class Malformed(Exception):
    """A line from the facade failed the envelope check."""


def initialize_request(request_id: int) -> dict[str, Any]:
    """The handshake. The client offers no file system and no terminal."""
    return {
        "jsonrpc": JSONRPC,
        "id": request_id,
        "method": METHOD_INITIALIZE,
        "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "clientCapabilities": {
                "fs": {"readTextFile": False, "writeTextFile": False},
                "terminal": False,
            },
            "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
        },
    }


def prompt_request(request_id: int, session: str, text: str) -> dict[str, Any]:
    """`session/prompt` with one text block. The origin of the row carries
    what the creche `_meta` keys carried (decision CH21-4)."""
    params: dict[str, Any] = {"sessionId": session, "prompt": [{"type": "text", "text": text}]}
    return {"jsonrpc": JSONRPC, "id": request_id, "method": METHOD_PROMPT, "params": params}


def cancel_notice(session: str) -> dict[str, Any]:
    """`session/cancel`, a notification."""
    return {"jsonrpc": JSONRPC, "method": METHOD_CANCEL, "params": {"sessionId": session}}


def _strict_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    record = dict(pairs)

    if len(record) != len(pairs):
        raise Malformed("duplicate key")

    return record


def _no_constant(name: str) -> object:
    raise Malformed(f"{name} is not JSON")


def loads_strict(text: str) -> object:
    """Strict JSON: no `NaN`, no `Infinity`, no key twice. Raises `Malformed`."""
    try:
        return json.loads(text, object_pairs_hook=_strict_pairs, parse_constant=_no_constant)
    except Malformed:
        raise
    except Exception as error:
        # Every type: deep nesting raises RecursionError, and a long integer
        # raises a plain ValueError.
        raise Malformed("not JSON") from error


def parse_response(text: str) -> Response:
    """Validate one line as a JSON-RPC response. Raises `Malformed`.

    A request or a notification from the facade is not an answer to this
    link, so it fails here too.
    """
    fields = as_object(loads_strict(text))

    if fields is None or fields.get("jsonrpc") != JSONRPC:
        raise Malformed("not a JSON-RPC 2.0 object")

    request_id = fields.get("id")

    if not set(fields) <= _RESPONSE_KEYS or ("result" in fields) == ("error" in fields):
        raise Malformed("not a response")

    if isinstance(request_id, bool) or not isinstance(request_id, int):
        raise Malformed("the id is not an integer")

    if "error" in fields:
        return Response(id=request_id, error=_rpc_error(fields["error"]))

    return Response(id=request_id, result=fields["result"])


def parse_message(text: str) -> Response | Notice:
    """One line after the handshake: a response, or a notification.

    A request from the facade is neither, so it fails here. This client
    offers no method that the facade may call.
    """
    fields = as_object(loads_strict(text))

    if fields is not None and "method" in fields and "id" not in fields:
        return _notice(fields)

    return parse_response(text)


def _notice(fields: dict[str, Any]) -> Notice:
    method = fields.get("method")
    params = as_object(fields.get("params"))

    if fields.get("jsonrpc") != JSONRPC or not set(fields) <= _NOTICE_KEYS:
        raise Malformed("not a JSON-RPC 2.0 notification")

    if not isinstance(method, str) or params is None:
        raise Malformed("a notification with no method or no params")

    return Notice(method=method, params=params)


def _rpc_error(value: object) -> RpcError:
    error = as_object(value)

    if error is None or not set(error) <= _ERROR_KEYS:
        raise Malformed("the error is not an object")

    code = error.get("code")
    message = error.get("message")

    if isinstance(code, bool) or not isinstance(code, int) or not isinstance(message, str):
        raise Malformed("the error has no code or no message")

    return RpcError(code=code, message=message)


def read_agent(response: Response) -> AgentAnswer:
    """The `initialize` answer: protocol 1, `loadSession`, `sessionCapabilities.list`."""
    answer = as_object(response.result)

    if response.error is not None or answer is None:
        raise AcpHandshakeError("the facade refused initialize")

    version = answer.get("protocolVersion")

    if isinstance(version, bool) or version != PROTOCOL_VERSION:
        raise AcpHandshakeError(f"the facade speaks protocol {version!r}")

    agent = as_object(answer.get("agentCapabilities")) or {}
    sessions = as_object(agent.get("sessionCapabilities")) or {}

    if agent.get("loadSession") is not True:
        raise AcpHandshakeError("the facade cannot load a session")

    if as_object(sessions.get("list")) is None:
        raise AcpHandshakeError("the facade cannot list sessions")

    return AgentAnswer(protocol_version=PROTOCOL_VERSION, load_session=True, session_list=True)


async def handshake(channel: Channel) -> AgentAnswer:
    """`initialize` on a fresh stream, and its validated answer.

    Raises `AcpHandshakeError` for an answer that is not usable or that does
    not come in `HANDSHAKE_DEADLINE_S`, and `ChannelClosed` for a stream that
    ends first. The door and the view run it too, before they pass a line."""
    try:
        async with asyncio.timeout(HANDSHAKE_DEADLINE_S):
            await channel.send(_line(initialize_request(HANDSHAKE_ID)))
            raw = await channel.receive()
    except TimeoutError as error:
        raise AcpHandshakeError("no answer to initialize in time") from error

    if raw is None:
        raise ChannelClosed("the stream ended before the answer to initialize")

    try:
        if raw.text is None:
            raise Malformed(f"refused line: {raw.refusal}")

        response = parse_response(raw.text)

        if response.id != HANDSHAKE_ID:
            raise Malformed("the answer names another request")
    except Malformed as error:
        raise AcpHandshakeError(str(error)) from error

    return read_agent(response)


class AcpLink:
    """The ACP stream of one session: the handshake, prompts, refusals."""

    def __init__(self, family: str, session: str, channel: Channel) -> None:
        self._family = family
        self._session = session
        self._channel = channel
        self._state = LinkState.STARTING
        self._next_id = 1
        self._reader: asyncio.Task[None] | None = None
        self._calls: dict[int, asyncio.Future[str]] = {}
        self._prompts: dict[int, _Collect] = {}
        self._dropped_ids: set[int] = set()

    @property
    def session(self) -> str:
        return self._session

    @property
    def state(self) -> LinkState:
        return self._state

    @property
    def prompts_in_flight(self) -> int:
        """How many `session/prompt` calls wait for their answer."""
        return len(self._prompts)

    async def start(self) -> AgentAnswer:
        """Run the handshake, then read the stream. Raises
        `AcpHandshakeError` or `ChannelClosed`."""
        agent = await handshake(self._channel)
        self._state = LinkState.READY
        self._reader = asyncio.create_task(self._read_loop(), name=f"acp {self._family}")
        self._reader.add_done_callback(report_failure)
        return agent

    async def prompt(self, text: str) -> PromptEnd:
        """`session/prompt`, and the `agent_message_chunk` text until it answers.

        A cancel of the awaiting task abandons the request: its late answer,
        and each late update of the session, is dropped.
        """
        request_id = self._mint_id()
        collect = _Collect()
        self._prompts[request_id] = collect

        try:
            reason = await self._call(request_id, prompt_request(request_id, self._session, text))
        finally:
            self._prompts.pop(request_id, None)

        return PromptEnd(stop_reason=StopReason(reason), text="".join(collect.parts))

    async def cancel(self) -> None:
        """`session/cancel`. The prompt then answers `cancelled`."""
        if self._state is not LinkState.READY:
            return

        with contextlib.suppress(ChannelClosed):
            await self._channel.send(_line(cancel_notice(self._session)))

    async def close(self) -> None:
        """End the stream. The vmm sees the close (decision CH20)."""
        self._state = LinkState.CLOSED
        self._fail_calls("the link closed")
        reader = self._reader
        self._reader = None

        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)

        await self._channel.close()

    async def _call(self, request_id: int, message: dict[str, Any]) -> str:
        if self._state is not LinkState.READY:
            raise AcpLinkLost(f"the link is {self._state.value}")

        answer: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._calls[request_id] = answer

        try:
            await self._channel.send(_line(message))
            return await answer
        except ChannelClosed as error:
            raise AcpLinkLost("the channel closed") from error
        finally:
            # Still listed means no answer came: a late one is dropped.
            if self._calls.pop(request_id, None) is not None:
                self._dropped_ids.add(request_id)

    async def _read_loop(self) -> None:
        while True:
            try:
                raw = await self._channel.receive()
            except ChannelClosed:
                raw = None

            if raw is None:
                self._lose("the stream ended")
                return

            try:
                if raw.text is None:
                    raise Malformed(f"refused line: {raw.refusal}")

                message = parse_message(raw.text)

                if isinstance(message, Notice):
                    self._read_notice(message)
                else:
                    self._settle(message)
            except Malformed as error:
                _LOG.warning("family=%s acp: %s", self._family, error)
                self._lose(str(error))
                return

    def _settle(self, response: Response) -> None:
        """An answer to a call in flight, or to an abandoned call."""
        call = self._calls.get(response.id)

        if call is None:
            if response.id in self._dropped_ids:
                return

            raise Malformed("an answer to no request")

        if response.error is not None:
            del self._calls[response.id]
            call.set_exception(AcpCallFailed(response.error.code, response.error.message))
            return

        # Read before the call leaves the book: a bad answer loses the link,
        # and the loss fails each call still in the book.
        result = _read_result(response.result)
        del self._calls[response.id]
        call.set_result(result)

    def _read_notice(self, notice: Notice) -> None:
        """A `session/update` of the session of this link. Nothing else."""
        if notice.method != METHOD_UPDATE:
            raise Malformed(f"a notification this client does not take: {notice.method}")

        update = as_object(notice.params.get("update"))

        if notice.params.get("sessionId") != self._session or update is None:
            raise Malformed("an update of another session or with no update")

        if not isinstance(update.get("sessionUpdate"), str):
            raise Malformed("an update with no kind")

        # One prompt at a time runs on a session, so its text is the text
        # of each prompt in flight. An update between prompts is dropped.
        if update["sessionUpdate"] == UPDATE_MESSAGE_CHUNK:
            text = _chunk_text(update)

            for collect in self._prompts.values():
                collect.add(text)

    def _lose(self, why: str) -> None:
        if self._state is not LinkState.READY:
            return

        self._state = LinkState.LOST
        _LOG.warning("family=%s session=%s acp link lost: %s", self._family, self._session, why)
        self._fail_calls(f"the link was lost: {why}")

    def _fail_calls(self, why: str) -> None:
        calls = list(self._calls.values())
        self._calls.clear()

        for call in calls:
            if not call.done():
                call.set_exception(AcpLinkLost(why))

    def _mint_id(self) -> int:
        request_id = self._next_id
        self._next_id += 1
        return request_id


def _read_result(result: object) -> str:
    """The stop reason of a prompt answer. Raises `Malformed`."""
    answer = as_object(result)

    if answer is None:
        raise Malformed("the answer to session/prompt is not an object")

    reason = answer.get("stopReason")

    if not isinstance(reason, str) or reason not in StopReason.__members__.values():
        raise Malformed("session/prompt named no known stop reason")

    return reason


def _chunk_text(update: dict[str, Any]) -> str:
    """The text of one `agent_message_chunk`. Another content kind is ''."""
    content = as_object(update.get("content"))

    if content is None or not isinstance(content.get("type"), str):
        raise Malformed("a message chunk with no content")

    if content["type"] != "text":
        return ""

    text = content.get("text")

    if not isinstance(text, str):
        raise Malformed("a text chunk with no text")

    return text


def _line(message: dict[str, Any]) -> str:
    return json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n"
