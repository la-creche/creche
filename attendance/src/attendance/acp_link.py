"""The supervisor link to one sandbox's `creche-facade`, over ACP v1.

One facade per sandbox owns the agent runtime. This link starts it with
`sbx exec`, runs `initialize` as the handshake, sends `_creche/beat` on the
playpen's ping cadence, and sends `_creche/shutdown` on stop.

Every byte from the facade is a claim. A line must be one strict JSON
object with a JSON-RPC 2.0 envelope, and it must answer a request that this
link sent. Any other line ends the link in `channel_lost`, with a
`protocol_violation` fault. The service goes on serving: only this link ends.

This link parses the envelope and the answers to its own calls only. The
schema of the pinned SDK sits in `acp/schema-v1.json`, and the tests tie
these shapes to it.

The link is also the client of one job: `new_session`, `prompt` and
`cancel`. A `session/update` notification is read for a session that this
link made, and only its `agent_message_chunk` text is kept.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from .atomic import as_object
from .channel import Channel, ChannelClosed, SandboxDial
from .config import DEFAULT_FACADE_COMMAND
from .faults import FaultCode, FaultReporter
from .lines import cut_bytes
from .paths import playpen_lock_file
from .supervisor_lock import LOCK_POLL_S, LOCK_STALE_S, wait_out_lock
from .tasks import report_failure

_LOG = logging.getLogger("attendance")

ACP_SDK_VERSION = "2.0.2"
ACP_SCHEMA_SHA256 = "4bcd1d2051ff7ed379bd9b234fb9b09c5ab9245478698e9fab805bd9b52a33a6"
SCHEMA_FILE = Path(__file__).parent / "acp" / "schema-v1.json"

PROTOCOL_VERSION = 1
JSONRPC = "2.0"
METHOD_INITIALIZE = "initialize"
# CONTRACT-QUESTION: phase 2 plan (d) puts the two extension methods and the
# `initialize` `_meta` keys in a vector file under `facade/vectors/`, which
# does not exist yet. This link takes the narrow reading: `_creche/beat` is a
# request that the facade answers, so two unanswered beats end the link as two
# missed pongs do. `_creche/shutdown` is a notification. `initialize` sends no
# `_meta`. When the vector file lands, the tests read it and these change.
METHOD_BEAT = "_creche/beat"
METHOD_SHUTDOWN = "_creche/shutdown"
METHOD_NEW_SESSION = "session/new"
METHOD_PROMPT = "session/prompt"
METHOD_CANCEL = "session/cancel"
METHOD_UPDATE = "session/update"
UPDATE_MESSAGE_CHUNK = "agent_message_chunk"
CLIENT_NAME = "attendance"
CLIENT_VERSION = "0.1.0"

#: The same `sbx exec` shape as the playpen's channel. The env file is the
#: one way a value reaches the VM (contract 03 §7.1).
FACADE_COMMAND = DEFAULT_FACADE_COMMAND

#: The playpen channel's line cap. The ACP door uses it for each frame.
MAX_ACP_LINE_BYTES = 1_048_576

#: The beat cadence, the beats that may go unanswered, the deadline of the
#: handshake and the grace of a stop. They are the numbers of the playpen
#: channel (contract 03 §10), so the facade's peer sees the same host.
BEAT_INTERVAL_S = 30
MISSED_BEATS_ALLOWED = 2
HOST_DEADLINE_S = 90
EXIT_WAIT_S = 5.0

#: The most text one prompt collects. The rest of the chunks are dropped.
MAX_ANSWER_BYTES = 1_048_576

#: The longest session id this link takes from the facade.
MAX_SESSION_ID_BYTES = 256

#: How many abandoned request ids and ended sessions this link remembers, so
#: that a late answer or update for one of them is dropped, not a violation.
MAX_RETIRED = 256

_RESPONSE_KEYS = frozenset({"jsonrpc", "id", "result", "error"})
_NOTICE_KEYS = frozenset({"jsonrpc", "method", "params"})
_ERROR_KEYS = frozenset({"code", "message", "data"})


class AcpHandshakeError(Exception):
    """The facade started, but its `initialize` answer is not usable."""


class _Mismatch(AcpHandshakeError):
    """A valid answer from the wrong facade: an image problem, not a restart one."""


class OrphanFacade(Exception):
    """An older supervisor still beats this sandbox's lock."""


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
    CHANNEL_LOST = "channel_lost"
    STOPPED = "stopped"


@dataclass(slots=True, frozen=True)
class AgentAnswer:
    """What the facade's `initialize` answer says, after validation."""

    protocol_version: int
    load_session: bool
    session_list: bool


@dataclass(slots=True, frozen=True)
class LinkStatus:
    state: LinkState
    sandbox: str | None
    agent: AgentAnswer | None


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
class _Call:
    """One request in flight: its method decides how the answer is read."""

    method: str
    answer: asyncio.Future[object]


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


LostCallback = Callable[[str], Awaitable[None]]
ChannelFactory = Callable[[SandboxDial], Channel]


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


def new_session_request(request_id: int, cwd: str, meta: dict[str, Any] | None) -> dict[str, Any]:
    """`session/new`. The agent reaches its tools by itself: no MCP server."""
    params: dict[str, Any] = {"cwd": cwd, "mcpServers": []}
    return _request(METHOD_NEW_SESSION, request_id, params, meta)


def prompt_request(
    request_id: int, session: str, text: str, meta: dict[str, Any] | None
) -> dict[str, Any]:
    """`session/prompt` with one text block."""
    params: dict[str, Any] = {"sessionId": session, "prompt": [{"type": "text", "text": text}]}
    return _request(METHOD_PROMPT, request_id, params, meta)


def cancel_notice(session: str) -> dict[str, Any]:
    """`session/cancel`, a notification."""
    return {"jsonrpc": JSONRPC, "method": METHOD_CANCEL, "params": {"sessionId": session}}


def _request(
    method: str, request_id: int, params: dict[str, Any], meta: dict[str, Any] | None
) -> dict[str, Any]:
    if meta is not None:
        params["_meta"] = meta

    return {"jsonrpc": JSONRPC, "id": request_id, "method": method, "params": params}


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
        raise _Mismatch("the facade refused initialize")

    version = answer.get("protocolVersion")

    if isinstance(version, bool) or version != PROTOCOL_VERSION:
        raise _Mismatch(f"the facade speaks protocol {version!r}")

    agent = as_object(answer.get("agentCapabilities")) or {}
    sessions = as_object(agent.get("sessionCapabilities")) or {}

    if agent.get("loadSession") is not True:
        raise _Mismatch("the facade cannot load a session")

    if as_object(sessions.get("list")) is None:
        raise _Mismatch("the facade cannot list sessions")

    return AgentAnswer(protocol_version=PROTOCOL_VERSION, load_session=True, session_list=True)


class AcpLink:
    """One sandbox's supervisor exec: handshake, beat, shutdown, refusals."""

    def __init__(
        self,
        family: str,
        factory: ChannelFactory,
        faults: FaultReporter,
        state_root: Path,
        on_lost: LostCallback,
        beat_interval_s: float = BEAT_INTERVAL_S,
        host_deadline_s: float = HOST_DEADLINE_S,
        lock_stale_s: float = LOCK_STALE_S,
        lock_poll_s: float = LOCK_POLL_S,
        exit_wait_s: float = EXIT_WAIT_S,
    ) -> None:
        self._family = family
        self._factory = factory
        self._faults = faults
        self._state_root = state_root
        self._on_lost = on_lost
        self._beat_interval_s = beat_interval_s
        self._host_deadline_s = host_deadline_s
        self._lock_stale_s = lock_stale_s
        self._lock_poll_s = lock_poll_s
        self._exit_wait_s = exit_wait_s

        self._channel: Channel | None = None
        self._state = LinkState.STARTING
        self._agent: AgentAnswer | None = None
        self._next_id = 0
        self._beats: set[int] = set()
        self._loops: list[asyncio.Task[None]] = []
        self._calls: dict[int, _Call] = {}
        self._prompts: dict[str, _Collect] = {}
        self._sessions: set[str] = set()
        self._dropped_ids: deque[int] = deque(maxlen=MAX_RETIRED)
        self._ended_sessions: deque[str] = deque(maxlen=MAX_RETIRED)

    @property
    def status(self) -> LinkStatus:
        sandbox = self._channel.sandbox if self._channel is not None else None
        return LinkStatus(state=self._state, sandbox=sandbox, agent=self._agent)

    @property
    def prompts_in_flight(self) -> int:
        """How many `session/prompt` calls wait for their answer."""
        return len(self._prompts)

    async def start(self, dial: SandboxDial) -> AgentAnswer:
        """Wait out the lock, start the facade, run the handshake."""
        lock = playpen_lock_file(self._state_root, self._family, dial.sandbox)

        if not await wait_out_lock(
            lock, self._lock_stale_s, self._lock_poll_s, self._host_deadline_s
        ):
            self._fault(FaultCode.ORPHAN_PROCESSES, "supervisor.lock is still beating", dial)
            raise OrphanFacade(f"sandbox {dial.sandbox} may still hold a supervisor")

        channel = self._factory(dial)
        await channel.start()
        self._channel = channel

        try:
            async with asyncio.timeout(self._host_deadline_s):
                agent = await self._handshake(channel, dial)
        except BaseException as error:
            self._state = LinkState.CHANNEL_LOST
            await channel.close()

            if isinstance(error, TimeoutError):
                raise AcpHandshakeError("no answer to initialize in time") from error

            raise

        # A completed handshake is the evidence that clears this writer's
        # faults (contract 05 §3.3.1).
        self._faults.clear_all(self._family)
        self._agent = agent
        self._state = LinkState.READY
        self._loops = [self._spawn(self._read_loop(channel)), self._spawn(self._beat_loop())]
        return agent

    async def new_session(self, cwd: str, meta: dict[str, Any] | None) -> str:
        """`session/new`. The facade mints the id. Raises `AcpLinkLost` or
        `AcpCallFailed`."""
        request_id = self._mint_id()
        answer = await self._call(
            METHOD_NEW_SESSION, request_id, new_session_request(request_id, cwd, meta)
        )
        session = str(answer)
        self._sessions.add(session)
        return session

    async def prompt(self, session: str, text: str, meta: dict[str, Any] | None) -> PromptEnd:
        """`session/prompt`, and the `agent_message_chunk` text until it answers.

        A cancel of the awaiting task abandons the request: its late answer,
        and each late update of the session, is dropped.
        """
        if session not in self._sessions:
            raise ValueError(f"session {session} was not made on this link")

        request_id = self._mint_id()
        collect = _Collect()
        self._prompts[session] = collect

        try:
            reason = await self._call(
                METHOD_PROMPT, request_id, prompt_request(request_id, session, text, meta)
            )
        finally:
            self._prompts.pop(session, None)

        return PromptEnd(stop_reason=StopReason(str(reason)), text="".join(collect.parts))

    async def cancel(self, session: str) -> None:
        """`session/cancel`. The prompt then answers `cancelled`."""
        channel = self._channel

        if channel is None or self._state is not LinkState.READY:
            return

        with contextlib.suppress(ChannelClosed, ValueError):
            await channel.send(_line(cancel_notice(session)))

    def end_session(self, session: str) -> None:
        """Forget a session whose job ended. A late update of it is dropped."""
        if session in self._sessions:
            self._sessions.discard(session)
            self._ended_sessions.append(session)

    async def _call(self, method: str, request_id: int, message: dict[str, Any]) -> object:
        channel = self._channel

        if channel is None or self._state is not LinkState.READY:
            raise AcpLinkLost(f"the link is {self._state.value}")

        answer: asyncio.Future[object] = asyncio.get_running_loop().create_future()
        self._calls[request_id] = _Call(method=method, answer=answer)

        try:
            await channel.send(_line(message))
            return await answer
        except ChannelClosed as error:
            raise AcpLinkLost("the channel closed") from error
        finally:
            # Still listed means no answer came: a late one is dropped.
            if self._calls.pop(request_id, None) is not None:
                self._dropped_ids.append(request_id)

    async def stop(self) -> None:
        """Send `_creche/shutdown`, then wait the playpen's grace for the exit."""
        channel = self._channel
        self._state = LinkState.STOPPED
        self._fail_calls("the link stopped")
        await self._stop_loops()

        if channel is None:
            return

        with contextlib.suppress(ChannelClosed, ValueError):
            await channel.send(_line(_call(METHOD_SHUTDOWN)))

        # Nothing is owed after a shutdown, so each line here is dropped.
        with contextlib.suppress(TimeoutError, ChannelClosed):
            async with asyncio.timeout(self._exit_wait_s):
                while await channel.receive() is not None:
                    continue

        await channel.close()

    async def _handshake(self, channel: Channel, dial: SandboxDial) -> AgentAnswer:
        request_id = self._mint_id()
        await channel.send(_line(initialize_request(request_id)))
        raw = await channel.receive()

        try:
            if raw is None or raw.text is None:
                raise Malformed("no answer to initialize")

            response = parse_response(raw.text)

            if response.id != request_id:
                raise Malformed("the answer names another request")

            return read_agent(response)
        except Malformed as error:
            self._fault(FaultCode.PROTOCOL_VIOLATION, str(error), dial)
            raise AcpHandshakeError(str(error)) from error
        except _Mismatch as error:
            self._fault(FaultCode.PROTOCOL_MISMATCH, str(error), dial)
            raise

    async def _read_loop(self, channel: Channel) -> None:
        while True:
            try:
                raw = await channel.receive()
            except ChannelClosed:
                raw = None

            if raw is None:
                await self._lose(channel)
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
                self._fault(FaultCode.PROTOCOL_VIOLATION, str(error), None)
                await self._lose(channel)
                return

    def _settle(self, response: Response) -> None:
        """An answer to a beat, to a call in flight, or to an abandoned call."""
        if response.id in self._beats:
            if response.error is not None:
                raise Malformed("a refused beat")

            self._beats.clear()
            return

        call = self._calls.get(response.id)

        if call is None:
            if response.id in self._dropped_ids:
                return

            raise Malformed("an answer to no request")

        if response.error is not None:
            del self._calls[response.id]
            call.answer.set_exception(AcpCallFailed(response.error.code, response.error.message))
            return

        # Read before the call leaves the book: a bad answer loses the link,
        # and the loss fails each call still in the book.
        result = _read_result(call.method, response.result)
        del self._calls[response.id]
        call.answer.set_result(result)

    def _read_notice(self, notice: Notice) -> None:
        """A `session/update` of a session this link made. Nothing else."""
        if notice.method != METHOD_UPDATE:
            raise Malformed(f"a notification this client does not take: {notice.method}")

        session = notice.params.get("sessionId")
        update = as_object(notice.params.get("update"))

        if not isinstance(session, str) or update is None:
            raise Malformed("an update with no session or no update")

        if not isinstance(update.get("sessionUpdate"), str):
            raise Malformed("an update with no kind")

        collect = self._prompts.get(session)

        if collect is None:
            if session in self._sessions or session in self._ended_sessions:
                return

            raise Malformed("an update for a session of no call")

        if update["sessionUpdate"] == UPDATE_MESSAGE_CHUNK:
            collect.add(_chunk_text(update))

    async def _beat_loop(self) -> None:
        """Two unanswered beats mean the facade is gone (contract 03 §10 rule 4)."""
        channel = self._channel

        while channel is not None:
            await asyncio.sleep(self._beat_interval_s)

            if len(self._beats) >= MISSED_BEATS_ALLOWED:
                await self._lose(channel)
                return

            request_id = self._mint_id()
            self._beats.add(request_id)
            try:
                await channel.send(_line(_call(METHOD_BEAT, request_id)))
            except ChannelClosed:
                await self._lose(channel)
                return

    async def _lose(self, channel: Channel) -> None:
        if self._state is not LinkState.READY:
            return

        self._state = LinkState.CHANNEL_LOST
        _LOG.warning("family=%s sandbox=%s acp link lost", self._family, channel.sandbox)
        self._fail_calls("the link was lost")
        await self._stop_loops()
        await channel.close()
        await self._on_lost(channel.sandbox)

    def _fail_calls(self, why: str) -> None:
        calls = list(self._calls.values())
        self._calls.clear()

        for call in calls:
            if not call.answer.done():
                call.answer.set_exception(AcpLinkLost(why))

    async def _stop_loops(self) -> None:
        current = asyncio.current_task()
        loops = [task for task in self._loops if task is not current]
        self._loops = []

        for task in loops:
            task.cancel()

        await asyncio.gather(*loops, return_exceptions=True)

    def _spawn(self, loop: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = asyncio.create_task(loop, name=f"acp {self._family}")
        task.add_done_callback(report_failure)
        return task

    def _mint_id(self) -> int:
        request_id = self._next_id
        self._next_id += 1
        return request_id

    def _fault(self, code: FaultCode, message: str, dial: SandboxDial | None) -> None:
        sandbox = dial.sandbox if dial is not None else self.status.sandbox
        self._faults.raise_fault(self._family, code, f"acp: {message}", sandbox)


def _read_result(method: str, result: object) -> object:
    """The one field of an answer that this client reads. Raises `Malformed`."""
    answer = as_object(result)

    if answer is None:
        raise Malformed(f"the answer to {method} is not an object")

    if method == METHOD_NEW_SESSION:
        session = answer.get("sessionId")

        if not isinstance(session, str) or not session:
            raise Malformed("session/new named no session")

        if len(session.encode("utf-8", "surrogatepass")) > MAX_SESSION_ID_BYTES:
            raise Malformed("session/new named a session id over the cap")

        return session

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


def _call(method: str, request_id: int | None = None) -> dict[str, Any]:
    """A request with an id, or a notification without one. No params."""
    params: dict[str, Any] = {}
    message: dict[str, Any] = {"jsonrpc": JSONRPC, "method": method, "params": params}

    if request_id is not None:
        message["id"] = request_id

    return message


def _line(message: dict[str, Any]) -> str:
    return json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n"
