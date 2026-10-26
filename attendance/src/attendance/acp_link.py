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
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from .atomic import as_object
from .channel import Channel, ChannelClosed, SandboxDial
from .faults import FaultCode, FaultReporter
from .paths import playpen_lock_file
from .supervisor_lock import wait_out_lock
from .tasks import report_failure
from .wire import (
    HOST_DEADLINE_S,
    LOCK_POLL_S,
    LOCK_STALE_S,
    MISSED_PONGS_ALLOWED,
    PING_INTERVAL_S,
    STOP_GRACE_MS,
)

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
CLIENT_NAME = "attendance"
CLIENT_VERSION = "0.1.0"

#: The same `sbx exec` shape as the playpen's channel. The env file is the
#: one way a value reaches the VM (contract 03 §7.1).
FACADE_COMMAND = "sbx exec --env-file {env_file} {sandbox} -- creche-facade"

#: The playpen channel's line cap. The ACP door uses it for each frame.
MAX_ACP_LINE_BYTES = 1_048_576

#: The playpen link's cadence and grace, so the two links behave the same.
BEAT_INTERVAL_S = PING_INTERVAL_S
EXIT_WAIT_S = STOP_GRACE_MS / 1_000

_RESPONSE_KEYS = frozenset({"jsonrpc", "id", "result", "error"})
_ERROR_KEYS = frozenset({"code", "message", "data"})


class AcpHandshakeError(Exception):
    """The facade started, but its `initialize` answer is not usable."""


class _Mismatch(AcpHandshakeError):
    """A valid answer from the wrong facade: an image problem, not a restart one."""


class OrphanFacade(Exception):
    """An older supervisor still beats this sandbox's lock."""


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

    @property
    def status(self) -> LinkStatus:
        sandbox = self._channel.sandbox if self._channel is not None else None
        return LinkStatus(state=self._state, sandbox=sandbox, agent=self._agent)

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

    async def stop(self) -> None:
        """Send `_creche/shutdown`, then wait the playpen's grace for the exit."""
        channel = self._channel
        self._state = LinkState.STOPPED
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

                self._settle(parse_response(raw.text))
            except Malformed as error:
                self._fault(FaultCode.PROTOCOL_VIOLATION, str(error), None)
                await self._lose(channel)
                return

    def _settle(self, response: Response) -> None:
        """Only a beat is in flight after the handshake."""
        if response.id not in self._beats or response.error is not None:
            raise Malformed("an answer to no beat")

        self._beats.clear()

    async def _beat_loop(self) -> None:
        """Two unanswered beats mean the facade is gone (contract 03 §10 rule 4)."""
        channel = self._channel

        while channel is not None:
            await asyncio.sleep(self._beat_interval_s)

            if len(self._beats) >= MISSED_PONGS_ALLOWED:
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
        await self._stop_loops()
        await channel.close()
        await self._on_lost(channel.sandbox)

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


def _call(method: str, request_id: int | None = None) -> dict[str, Any]:
    """A request with an id, or a notification without one. No params."""
    params: dict[str, Any] = {}
    message: dict[str, Any] = {"jsonrpc": JSONRPC, "method": method, "params": params}

    if request_id is not None:
        message["id"] = request_id

    return message


def _line(message: dict[str, Any]) -> str:
    return json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n"
