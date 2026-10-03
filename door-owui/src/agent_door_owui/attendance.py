"""The client of `attendance`, the session service (contract 02).

The door holds no session state. It finds or creates a session, runs one
turn, and reads the event stream back. Everything else about a session
belongs to `attendance`.

Two rules here are not style choices.

1. The NDJSON stream is split on LF and on nothing else (contract 02 §8,
   contract 03 §2 rule 4). A generic line reader also splits on U+2028 and
   U+2029, which are legal inside a JSON string: Python's `str.splitlines`
   does, and so does httpx's own `aiter_lines`. This module therefore reads
   bytes and finds its own line breaks.
2. The token never reaches argv, a URL or a log line (invariant 13). It is
   read from a file into memory and sent in one header.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

import httpx

from .config import DoorConfig
from .journal import MAX_LINE_BYTES, BadLine, JournalLine, parse_line
from .untrusted import as_object, field_text, is_object

_LOG = logging.getLogger(__name__)

DOOR_NAME = "owui"

# Contract 02 §5.4 caps a turn at 3600 seconds. A settled call waits for the
# answer, so its read timeout sits just past that. A streamed call has no
# read timeout: `attendance` heartbeats an idle stream every 15 seconds, so
# silence there is not the same as a dead socket.
SETTLED_READ_TIMEOUT_S = 3700.0
CONNECT_TIMEOUT_S = 5.0
WRITE_TIMEOUT_S = 30.0

_CREATED = 201
_OK = 200

# Contract 02 §4.3's turn state literal. Not the same string as
# `journal.LineKind.TURN_SETTLED` ("turn_settled"), which names a JOURNAL
# LINE kind, not a turn state. Comparing against the wrong one would call
# every settled-mode turn a failure.
TURN_STATE_SETTLED = "settled"


class WaitMode(StrEnum):
    """Contract 02 §5.4's three response shapes."""

    STREAM = "stream"
    SETTLED = "settled"
    ACCEPTED = "accepted"


@dataclass(frozen=True)
class TurnRequest:
    """One turn, addressed to one session."""

    family: str
    session: str
    prompt: str
    idempotency_key: str
    persona: str | None
    chat_id: str
    message_id: str
    user_message_id: str | None
    parent_id: str | None

    def body(self, wait: WaitMode, *, with_parent: bool = True) -> dict[str, object]:
        owui: dict[str, object] = {
            "chat_id": self.chat_id,
            "message_id": self.message_id,
            "user_message_id": self.user_message_id,
        }
        # Dropping `parent_id` is how the door falls back to a plain turn
        # when `attendance` cannot branch. Everything else stays the same, so
        # the idempotency key still names the same assistant message.
        if with_parent:
            owui["parent_id"] = self.parent_id

        body: dict[str, object] = {
            "prompt": self.prompt,
            "idempotency_key": self.idempotency_key,
            "wait": str(wait),
            "owui": owui,
        }
        if self.persona is not None:
            body["persona_text"] = self.persona

        return body


@dataclass(frozen=True)
class SettledTurn:
    """The `wait=settled` answer of contract 02 §5.4."""

    turn: str
    state: str
    text: str
    usage: dict[str, object]
    reason: str

    @property
    def is_settled(self) -> bool:
        """`settled` is the only success state (contract 02 §4.3)."""
        return self.state == TURN_STATE_SETTLED


class AttendanceError(Exception):
    """One error from contract 02 §14, as `attendance` reported it."""

    def __init__(self, code: str, message: str, status: int) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status


class StreamBroken(Exception):
    """The event stream ended or misbehaved before the turn settled."""


class AttendanceClient(Protocol):
    """What the door needs from the session service, and nothing more."""

    async def ensure_session(self, family: str, session: str) -> None:
        """Create or find the session. Contract 02 §5.1."""
        ...

    def stream_turn(
        self, request: TurnRequest, *, with_parent: bool = True
    ) -> AbstractAsyncContextManager[AsyncIterator[JournalLine]]:
        """Run a turn and open its event stream. Contract 02 §5.4."""
        ...

    async def settled_turn(self, request: TurnRequest, *, with_parent: bool = True) -> SettledTurn:
        """Run a turn and wait for the answer. Contract 02 §5.4."""
        ...


class HttpAttendance:
    """`AttendanceClient` over HTTP, on a Unix socket or the LAN address."""

    def __init__(self, config: DoorConfig, client: httpx.AsyncClient | None = None) -> None:
        self._client = client if client is not None else _build_client(config)
        self._auth = {"Authorization": f"Bearer {config.attendance_token}"}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def ensure_session(self, family: str, session: str) -> None:
        response = await self._client.post(
            "/v1/sessions",
            json={"family": family, "session": session, "labels": {"door": DOOR_NAME}},
            headers=self._auth,
            timeout=httpx.Timeout(CONNECT_TIMEOUT_S, read=CONNECT_TIMEOUT_S * 2),
        )
        if response.status_code in (_OK, _CREATED):
            return

        raise _error_of(response.status_code, response.text)

    async def settled_turn(self, request: TurnRequest, *, with_parent: bool = True) -> SettledTurn:
        response = await self._client.post(
            _turns_path(request),
            json=request.body(WaitMode.SETTLED, with_parent=with_parent),
            headers=self._auth,
            timeout=httpx.Timeout(CONNECT_TIMEOUT_S, read=SETTLED_READ_TIMEOUT_S),
        )
        if response.status_code != _OK:
            raise _error_of(response.status_code, response.text)

        return _settled_of(response.text)

    @asynccontextmanager
    async def stream_turn(
        self, request: TurnRequest, *, with_parent: bool = True
    ) -> AsyncGenerator[AsyncIterator[JournalLine], None]:
        """Open the turn's event stream. The caller iterates it."""
        stream = self._client.stream(
            "POST",
            _turns_path(request),
            json=request.body(WaitMode.STREAM, with_parent=with_parent),
            headers=self._auth,
            timeout=httpx.Timeout(CONNECT_TIMEOUT_S, read=None, write=WRITE_TIMEOUT_S),
        )
        async with stream as response:
            if response.status_code != _OK:
                # The refusal arrives before any frame is written, so the
                # caller can still answer with a status of its own.
                raise _error_of(response.status_code, (await response.aread()).decode("utf-8"))

            yield _iter_lines(response)


def _build_client(config: DoorConfig) -> httpx.AsyncClient:
    transport = (
        httpx.AsyncHTTPTransport(uds=str(config.attendance_socket))
        if config.attendance_socket is not None
        else None
    )

    return httpx.AsyncClient(base_url=config.attendance_url, transport=transport)


def _turns_path(request: TurnRequest) -> str:
    return f"/v1/sessions/{request.family}/{request.session}/turns"


async def _iter_lines(response: httpx.Response) -> AsyncIterator[JournalLine]:
    """Split the NDJSON stream on LF, and on nothing else.

    Every failure this function can raise is `StreamBroken`, never a raw
    `httpx` exception. `StreamBroken` is this module's own vocabulary, and
    the caller (app.py) is written against `AttendanceClient` and never
    imports `httpx` — a caller that had to catch `httpx.HTTPError` directly
    would be reaching past this module's abstraction to a transport detail
    it should never need to know about.
    """
    buffer = bytearray()
    try:
        async for chunk in response.aiter_bytes():
            buffer.extend(chunk)
            if len(buffer) > MAX_LINE_BYTES:
                # No safe resync: a line this long means the stream is not
                # what this door thinks it is. The turn keeps running on
                # the host.
                raise StreamBroken(f"a line passed {MAX_LINE_BYTES} bytes")

            while (index := buffer.find(b"\n")) >= 0:
                raw = bytes(buffer[:index]).rstrip(b"\r")
                del buffer[: index + 1]
                line = _parsed(raw)
                if line is not None:
                    yield line
    except httpx.HTTPError as exc:
        # The connection to attendance dropped mid-stream. The turn may still
        # be running on the host (invariant 4) — only this door's read of
        # it broke.
        raise StreamBroken(f"the connection to attendance broke: {exc}") from exc


def _parsed(raw: bytes) -> JournalLine | None:
    if not raw.strip():
        return None

    try:
        return parse_line(raw)
    except BadLine as exc:
        # Drop and count, the way the host treats a playpen line
        # (contract 03 §13 rule 1). A dropped terminal line still ends the
        # stream visibly, because the translator closes an unsettled turn.
        _LOG.warning("attendance: dropped a line (%s)", exc)
        return None


def _settled_of(raw: str) -> SettledTurn:
    body = _json_object(raw)

    return SettledTurn(
        turn=field_text(body, "turn"),
        state=field_text(body, "state"),
        text=field_text(body, "text"),
        usage=as_object(body.get("usage")),
        reason=field_text(body, "reason"),
    )


def _error_of(status: int, raw: str) -> AttendanceError:
    """Read contract 02 §14's error body. A missing one still fails."""
    error = as_object(_json_object(raw).get("error"))
    code = field_text(error, "code") or "internal"
    message = field_text(error, "message") or f"attendance answered {status}"

    return AttendanceError(code, message, status)


def _json_object(raw: str) -> dict[str, object]:
    try:
        parsed: object = json.loads(raw)
    except ValueError:
        _LOG.warning("attendance: answered with something that is not JSON")
        return {}

    return parsed if is_object(parsed) else {}
