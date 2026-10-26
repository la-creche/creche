"""A stand-in for `creche-facade`: the ACP side of the supervisor link.

    fake_facade.py

It reads JSON-RPC lines on stdin and answers on stdout, one line each.
`fake_sbx.py` starts it, so its environment comes from the env file only.
A test in this process drives the same `FakeFacade` over a `FakeChannel`
with `serve` and `FacadeFleet`.

- `FAKE_FACADE_MODE=garbage` writes one line that is not JSON after it
  answers `initialize`.
- `FAKE_FACADE_PROMPT=<mode>` scripts each `session/prompt` (`PromptMode`).
- `FAKE_FACADE_RECORD=<path>` appends each method it receives to the file.

`session/list` answers the page that `pages` holds for the cursor (`""`
for none). `session/load` sends the updates that `replays` holds for the
session, then answers `{}`. An unknown session answers `-32002`.

`_creche/shutdown` ends the process with exit status 0.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from attendance.channel import Channel, FakeChannel, SandboxDial

INITIALIZE_RESULT: dict[str, Any] = {
    "protocolVersion": 1,
    "agentCapabilities": {"loadSession": True, "sessionCapabilities": {"list": {}}},
    "authMethods": [],
}

#: The two chunks an `ok` prompt streams before it answers `end_turn`.
ANSWER_CHUNKS = ("The boiler was serviced ", "on 2026-03-11.")
ANSWER = "".join(ANSWER_CHUNKS)

#: The JSON-RPC error an `error` prompt answers with.
ERROR_CODE = -32603
ERROR_MESSAGE = "the model provider refused the request"

#: ACP's `resourceNotFound`, the facade's answer to an unknown session.
NOT_FOUND_CODE = -32002


class PromptMode(StrEnum):
    """What the fake does with each `session/prompt`."""

    #: Stream `ANSWER_CHUNKS`, then answer `end_turn`.
    OK = "ok"
    #: Answer nothing until `session/cancel`, then answer `cancelled`.
    HANG = "hang"
    #: Answer with a JSON-RPC error.
    ERROR = "error"
    #: Stream one chunk, then end the stream.
    LOSE = "lose"
    #: Hold the prompt until the test calls `settle`.
    HOLD = "hold"


class _Drop(Exception):
    """The far side writes `lines`, then ends the stream."""

    def __init__(self, lines: list[str]) -> None:
        super().__init__("drop")
        self.lines = lines


@dataclass(slots=True)
class Prompt:
    """One `session/prompt` the fake holds."""

    request_id: int
    session: str
    text: str
    meta: dict[str, Any] | None


@dataclass(slots=True)
class FakeFacade:
    """The far side of one link. `handle` turns one line into the answers."""

    prompt_mode: PromptMode = PromptMode.OK
    garbage: bool = False
    #: Any other number fails the host's handshake.
    protocol_version: int = 1
    methods: list[str] = field(default_factory=list[str])
    received: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    held: dict[str, Prompt] = field(default_factory=dict[str, Prompt])
    sessions: list[str] = field(default_factory=list[str])
    #: `session/list` results by cursor, `""` for the first page.
    pages: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    #: `session/load` updates by session id.
    replays: dict[str, list[dict[str, Any]]] = field(
        default_factory=dict[str, list[dict[str, Any]]]
    )

    def handle(self, line: str) -> list[str]:
        """The lines this fake writes for one line it read. Raises `_Drop`."""
        message: dict[str, Any] = json.loads(line)
        method = str(message.get("method", ""))
        self.methods.append(method)
        self.received.append(message)
        request_id = message.get("id")
        params: dict[str, Any] = message.get("params") or {}

        if method == "initialize":
            result = {**INITIALIZE_RESULT, "protocolVersion": self.protocol_version}
            lines = [_answer(request_id, result)]
            return [*lines, "not json"] if self.garbage else lines

        if method == "_creche/beat":
            return [_answer(request_id, {})]

        if method == "session/new":
            session = f"sess-{len(self.sessions) + 1}"
            self.sessions.append(session)
            return [_answer(request_id, {"sessionId": session})]

        if method == "session/prompt":
            return self._prompt(int(str(request_id)), params)

        if method == "session/list":
            page = self.pages.get(str(params.get("cursor") or ""))
            return [_answer(request_id, page)] if page is not None else [_error(request_id)]

        if method == "session/load":
            return self._load(request_id, str(params.get("sessionId")))

        if method == "session/cancel":
            held = self.held.pop(str(params.get("sessionId")), None)
            return [] if held is None else [_answer(held.request_id, {"stopReason": "cancelled"})]

        return []

    def _load(self, request_id: object, session: str) -> list[str]:
        updates = self.replays.get(session)

        if updates is None:
            return [_error(request_id, NOT_FOUND_CODE)]

        notices = [_update(session, update) for update in updates]
        return [*notices, _answer(request_id, {})]

    def settle(self, session: str, stop_reason: str = "end_turn", text: str = ANSWER) -> list[str]:
        """The lines that end one held prompt."""
        held = self.held.pop(session)
        return [_chunk(session, text), _answer(held.request_id, {"stopReason": stop_reason})]

    def _prompt(self, request_id: int, params: dict[str, Any]) -> list[str]:
        session = str(params.get("sessionId"))
        blocks: list[dict[str, Any]] = params.get("prompt") or []
        text = "".join(str(block.get("text", "")) for block in blocks)
        self.held[session] = Prompt(request_id, session, text, params.get("_meta"))

        if self.prompt_mode in {PromptMode.HANG, PromptMode.HOLD}:
            return []

        if self.prompt_mode is PromptMode.LOSE:
            raise _Drop([_chunk(session, ANSWER_CHUNKS[0])])

        held = self.held.pop(session)

        if self.prompt_mode is PromptMode.ERROR:
            error = {"code": ERROR_CODE, "message": ERROR_MESSAGE}
            return [_line({"jsonrpc": "2.0", "id": held.request_id, "error": error})]

        chunks = [_chunk(session, part) for part in ANSWER_CHUNKS]
        return [*chunks, _answer(held.request_id, {"stopReason": "end_turn"})]


def _line(message: dict[str, Any]) -> str:
    return json.dumps(message, separators=(",", ":"))


def _answer(request_id: object, result: object) -> str:
    return _line({"jsonrpc": "2.0", "id": request_id, "result": result})


def _error(request_id: object, code: int = -32602) -> str:
    error = {"code": code, "message": "refused"}
    return _line({"jsonrpc": "2.0", "id": request_id, "error": error})


def _update(session: str, update: dict[str, Any]) -> str:
    params = {"sessionId": session, "update": update}
    return _line({"jsonrpc": "2.0", "method": "session/update", "params": params})


def _chunk(session: str, text: str) -> str:
    update = {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}
    return _update(session, update)


# ----------------------------------------------------------- in process


async def serve(channel: FakeChannel, facade: FakeFacade) -> None:
    """Answer every host line on `channel` until the host stops."""
    while True:
        try:
            line = await channel.next_host_line(timeout=3600)
        except TimeoutError:
            return

        try:
            answers = facade.handle(line)
        except _Drop as drop:
            for answer in drop.lines:
                await channel.feed(answer)

            await channel.drop()
            return

        for answer in answers:
            await channel.feed(answer)

        if facade.methods[-1] == "_creche/shutdown":
            # The process exits: the host reads the end of the stream.
            await channel.drop()
            return


@dataclass(slots=True)
class FacadeFleet:
    """One `FakeChannel` and one `FakeFacade` per dialled sandbox."""

    prompt_mode: PromptMode = PromptMode.OK
    #: The sandboxes whose facade speaks another protocol version.
    mismatched: set[str] = field(default_factory=set[str])
    channels: dict[str, FakeChannel] = field(default_factory=dict[str, "FakeChannel"])
    facades: dict[str, FakeFacade] = field(default_factory=dict[str, FakeFacade])
    dials: list[str] = field(default_factory=list[str])
    #: What each relay's facade answers, and each relay's facade.
    pages: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    replays: dict[str, list[dict[str, Any]]] = field(
        default_factory=dict[str, list[dict[str, Any]]]
    )
    relays: list[FakeFacade] = field(default_factory=list[FakeFacade])
    #: A relay that ends before it answers: no facade listens in the VM.
    relay_gone: bool = False
    _tasks: list[asyncio.Task[None]] = field(default_factory=list[asyncio.Task[None]])

    def factory(self, dial: SandboxDial) -> Channel:
        from attendance.channel import FakeChannel

        self.dials.append(dial.sandbox)
        channel = FakeChannel(dial.sandbox)
        version = 2 if dial.sandbox in self.mismatched else 1
        facade = FakeFacade(prompt_mode=self.prompt_mode, protocol_version=version)
        self.channels[dial.sandbox] = channel
        self.facades[dial.sandbox] = facade
        self._tasks.append(asyncio.create_task(serve(channel, facade)))
        return channel

    def relay(self, dial: SandboxDial) -> Channel:
        """One `acp-relay`: its own channel, to a facade that shares the script."""
        from attendance.channel import FakeChannel

        channel = FakeChannel(dial.sandbox)
        facade = FakeFacade(pages=self.pages, replays=self.replays)
        self.relays.append(facade)

        if self.relay_gone:
            self._tasks.append(asyncio.create_task(channel.drop()))
        else:
            self._tasks.append(asyncio.create_task(serve(channel, facade)))

        return channel

    async def settle(
        self, sandbox: str, session: str, stop_reason: str = "end_turn", text: str = ANSWER
    ) -> None:
        """End one held prompt with `stop_reason`."""
        for line in self.facades[sandbox].settle(session, stop_reason, text):
            await self.channels[sandbox].feed(line)

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()

        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


# ------------------------------------------------------------- program


def record(method: str) -> None:
    path = os.environ.get("FAKE_FACADE_RECORD", "")

    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(method + "\n")


def main() -> int:
    facade = FakeFacade(
        prompt_mode=PromptMode(os.environ.get("FAKE_FACADE_PROMPT") or PromptMode.OK.value),
        garbage=os.environ.get("FAKE_FACADE_MODE") == "garbage",
    )

    for line in sys.stdin:
        try:
            answers = facade.handle(line)
        except _Drop as drop:
            record(facade.methods[-1])
            sys.stdout.write("".join(answer + "\n" for answer in drop.lines))
            sys.stdout.flush()
            return 1

        record(facade.methods[-1])
        sys.stdout.write("".join(answer + "\n" for answer in answers))
        sys.stdout.flush()

        if facade.methods[-1] == "_creche/shutdown":
            return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
