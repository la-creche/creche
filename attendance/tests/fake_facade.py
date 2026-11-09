"""A stand-in for `creche-facade` in the VM of one session.

A test drives a `FakeFacade` over a `FakeChannel`, the far side of one vmm
ACP stream, with `serve`. `fake_vmm.py` starts one for each stream.

`session/load` sends the updates that `replays` holds for the session, then
answers `{}`. An unknown session answers `-32002`. `session/prompt` follows
`prompt_mode`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fake_channel import FakeChannel

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

    request_id: object
    session: str
    text: str
    meta: dict[str, Any] | None


@dataclass(slots=True)
class FakeFacade:
    """The far side of one stream. `handle` turns one line into the answers."""

    prompt_mode: PromptMode = PromptMode.OK
    garbage: bool = False
    #: Any other number fails the host's handshake.
    protocol_version: int = 1
    methods: list[str] = field(default_factory=list[str])
    received: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    held: dict[str, Prompt] = field(default_factory=dict[str, Prompt])
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
            lines = [answer(request_id, result)]
            return [*lines, "not json"] if self.garbage else lines

        if method == "session/prompt":
            return self._prompt(request_id, params)

        if method == "session/load":
            return self._load(request_id, str(params.get("sessionId")))

        if method == "session/cancel":
            held = self.held.pop(str(params.get("sessionId")), None)
            return [] if held is None else [answer(held.request_id, {"stopReason": "cancelled"})]

        return []

    def _load(self, request_id: object, session: str) -> list[str]:
        updates = self.replays.get(session)

        if updates is None:
            return [error(request_id, NOT_FOUND_CODE)]

        notices = [update(session, one) for one in updates]
        return [*notices, answer(request_id, {})]

    def settle(self, session: str, stop_reason: str = "end_turn", text: str = ANSWER) -> list[str]:
        """The lines that end one held prompt."""
        held = self.held.pop(session)
        return [chunk(session, text), answer(held.request_id, {"stopReason": stop_reason})]

    def _prompt(self, request_id: object, params: dict[str, Any]) -> list[str]:
        session = str(params.get("sessionId"))
        blocks: list[dict[str, Any]] = params.get("prompt") or []
        text = "".join(str(block.get("text", "")) for block in blocks)
        self.held[session] = Prompt(request_id, session, text, params.get("_meta"))

        if self.prompt_mode in {PromptMode.HANG, PromptMode.HOLD}:
            return []

        if self.prompt_mode is PromptMode.LOSE:
            raise _Drop([chunk(session, ANSWER_CHUNKS[0])])

        held = self.held.pop(session)

        if self.prompt_mode is PromptMode.ERROR:
            refusal = {"code": ERROR_CODE, "message": ERROR_MESSAGE}
            return [line({"jsonrpc": "2.0", "id": held.request_id, "error": refusal})]

        chunks = [chunk(session, part) for part in ANSWER_CHUNKS]
        return [*chunks, answer(held.request_id, {"stopReason": "end_turn"})]


def line(message: dict[str, Any]) -> str:
    return json.dumps(message, separators=(",", ":"))


def answer(request_id: object, result: object) -> str:
    return line({"jsonrpc": "2.0", "id": request_id, "result": result})


def error(request_id: object, code: int = -32602) -> str:
    refusal = {"code": code, "message": "refused"}
    return line({"jsonrpc": "2.0", "id": request_id, "error": refusal})


def update(session: str, one: dict[str, Any]) -> str:
    params = {"sessionId": session, "update": one}
    return line({"jsonrpc": "2.0", "method": "session/update", "params": params})


def chunk(session: str, text: str) -> str:
    one = {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}
    return update(session, one)


async def serve(channel: FakeChannel, facade: FakeFacade) -> None:
    """Answer every host line on `channel` until the host closes it. A line
    the host sent before its close is read."""
    while not channel.closed or channel.host_lines_waiting():
        try:
            received = await channel.next_host_line(timeout=3600)
        except TimeoutError:
            return

        try:
            answers = facade.handle(received)
        except _Drop as drop:
            for one in drop.lines:
                await channel.feed(one)

            await channel.drop()
            return

        for one in answers:
            await channel.feed(one)
