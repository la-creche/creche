"""The read-only view of one family's sessions, over one `acp-relay`.

    GET /families/{f}/sessions        initialize, session/list, close
    GET /families/{f}/sessions/{id}   initialize, session/load, close

The view sends `initialize` and one read method on a relay of its own,
and never `session/prompt`. The facade owns each conversation. This module
turns its answer into JSON for the noticeboard.

Every line from the relay is a claim. A line that is not one strict
JSON-RPC answer or update ends the read in `sandbox_unavailable`. Each text
is cut to a byte cap, and a half of a surrogate pair becomes `?`.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .acp_jobs import META_DELEGATION, META_JOB, META_TRIGGER
from .acp_link import (
    JSONRPC,
    METHOD_UPDATE,
    AcpHandshakeError,
    Malformed,
    Notice,
    RpcError,
    initialize_request,
    parse_message,
    parse_response,
    read_agent,
)
from .atomic import as_array, as_object
from .channel import Channel, ChannelClosed
from .errors import ApiError, ErrorCode
from .lines import cut_bytes

METHOD_LIST = "session/list"
METHOD_LOAD = "session/load"

#: The cap of one text of the transcript: the facade's own forward cap.
MAX_TEXT_BYTES = 65_536
#: The cap of an id, a title, a time or a cursor.
MAX_NAME_BYTES = 256
MAX_CURSOR_BYTES = MAX_NAME_BYTES
#: The most updates one read takes. The rest of the replay is not read.
MAX_UPDATES = 20_000
#: The entries of one transcript page, and the bytes of their JSON.
DEFAULT_PAGE_ENTRIES = 100
MAX_PAGE_ENTRIES = 500
MAX_PAGE_BYTES = 1_048_576
#: How long one read may take, from `initialize` to the answer.
READ_DEADLINE_S = 30.0

#: ACP's `resourceNotFound` and JSON-RPC's `invalidParams`.
RPC_NOT_FOUND = -32002
RPC_INVALID_PARAMS = -32602

#: CONTRACT-QUESTION: ACP's session row has no creation time, and the
#: vector file names no key for one. The view reads this key of the row's
#: `_meta`. The facade sends no `_meta` on a row yet, so `created`,
#: `job_id`, `delegation` and `trigger` read null until it does.
META_CREATED = "creche/created_at"

_TOOL_STATUSES = frozenset({"pending", "in_progress", "completed", "failed"})
_TRIGGER_KEYS = frozenset({"kind", "name", "fired_at", "chain"})
_TEXT_KINDS = {
    "user_message_chunk": "user",
    "agent_message_chunk": "agent",
    "agent_thought_chunk": "thought",
}
_CURSOR_RE = re.compile(r"^[\x21-\x7e]{1,256}\Z")
_COUNT_RE = re.compile(r"^[0-9]{1,9}\Z")
_INIT_ID = 0
_READ_ID = 1


@dataclass(slots=True, frozen=True)
class Page:
    """Which entries of a transcript one answer holds."""

    start: int = 0
    limit: int = DEFAULT_PAGE_ENTRIES


def read_cursor(query: Mapping[str, str]) -> str | None:
    """The facade's cursor, opaque. Printable ASCII, at most 256 bytes."""
    cursor = query.get("cursor")

    if cursor is not None and not _CURSOR_RE.match(cursor):
        raise ApiError(ErrorCode.BAD_REQUEST, "cursor is not one that a page gave")

    return cursor


def read_page(query: Mapping[str, str]) -> Page:
    """`start` from 0, `limit` from 1 to `MAX_PAGE_ENTRIES`."""
    start = _count(query, "start", 0)
    limit = _count(query, "limit", DEFAULT_PAGE_ENTRIES)

    if not 1 <= limit <= MAX_PAGE_ENTRIES:
        raise ApiError(ErrorCode.BAD_REQUEST, f"limit is not 1 to {MAX_PAGE_ENTRIES}")

    return Page(start=start, limit=limit)


def _count(query: Mapping[str, str], name: str, default: int) -> int:
    value = query.get(name)

    if value is None:
        return default

    if not _COUNT_RE.match(value):
        raise ApiError(ErrorCode.BAD_REQUEST, f"{name} is not a whole number")

    return int(value)


async def list_sessions(relay: Channel, family: str, cursor: str | None) -> dict[str, Any]:
    """One page of `session/list`, newest first, with the facade's cursor."""
    params: dict[str, Any] = {} if cursor is None else {"cursor": cursor}
    result = await _read(relay, family, METHOD_LIST, params, None)

    try:
        return _list_view(result)
    except Malformed as error:
        raise _unavailable(family, str(error)) from error


async def load_transcript(
    relay: Channel, family: str, session: str, cwd: str, page: Page
) -> dict[str, Any]:
    """The replay of `session/load`, folded into entries, one page of them."""
    transcript = Transcript(session)
    params: dict[str, Any] = {"sessionId": session, "cwd": cwd, "mcpServers": []}
    await _read(relay, family, METHOD_LOAD, params, transcript.add)
    return transcript.page(page)


async def _read(
    relay: Channel,
    family: str,
    method: str,
    params: dict[str, Any],
    on_update: Callable[[dict[str, Any]], bool] | None,
) -> object:
    """`initialize`, then one read. None when `on_update` took enough."""
    try:
        async with asyncio.timeout(READ_DEADLINE_S):
            return await _exchange(relay, method, params, on_update)
    except (Malformed, AcpHandshakeError, ChannelClosed, TimeoutError) as error:
        raise _unavailable(family, str(error) or type(error).__name__) from error
    except _Refused as refused:
        raise _refusal(family, refused.error) from refused


async def _exchange(
    relay: Channel,
    method: str,
    params: dict[str, Any],
    on_update: Callable[[dict[str, Any]], bool] | None,
) -> object:
    await relay.send(_line(initialize_request(_INIT_ID)))
    answer = parse_response(await _next(relay))

    if answer.id != _INIT_ID:
        raise Malformed("the answer names another request")

    read_agent(answer)
    await relay.send(
        _line({"jsonrpc": JSONRPC, "id": _READ_ID, "method": method, "params": params})
    )

    while True:
        message = parse_message(await _next(relay))

        if isinstance(message, Notice):
            if message.method != METHOD_UPDATE or on_update is None:
                raise Malformed(f"a notification this read does not take: {message.method}")

            if not on_update(message.params):
                return None

            continue

        if message.id != _READ_ID:
            raise Malformed("an answer to no request")

        if message.error is not None:
            raise _Refused(message.error)

        return message.result


async def _next(relay: Channel) -> str:
    raw = await relay.receive()

    if raw is None:
        raise ChannelClosed("the relay ended")

    if raw.text is None:
        raise Malformed(f"refused line: {raw.refusal}")

    return raw.text


class _Refused(Exception):
    def __init__(self, error: RpcError) -> None:
        super().__init__(error.message)
        self.error = error


def _refusal(family: str, error: RpcError) -> ApiError:
    if error.code == RPC_NOT_FOUND:
        return ApiError(ErrorCode.NOT_FOUND, "the facade knows no such session", family=family)

    if error.code == RPC_INVALID_PARAMS:
        return ApiError(ErrorCode.BAD_REQUEST, "the facade refused the request", family=family)

    return _unavailable(family, f"the facade answered error {error.code}")


def _unavailable(family: str, why: str) -> ApiError:
    return ApiError(
        ErrorCode.SANDBOX_UNAVAILABLE,
        f"family {family} facade gave no answer to read",
        family=family,
        detail={"reason": _clean(cut_bytes(why, MAX_NAME_BYTES))},
    )


# ------------------------------------------------------------ session/list


def _list_view(result: object) -> dict[str, Any]:
    answer = as_object(result) or {}
    rows = as_array(answer.get("sessions"))
    cursor = answer.get("nextCursor")

    if rows is None:
        raise Malformed("session/list named no sessions")

    if cursor is not None and not (isinstance(cursor, str) and _CURSOR_RE.match(cursor)):
        raise Malformed("session/list named a cursor that is not one")

    return {"sessions": [_row(one) for one in rows], "next_cursor": cursor}


def _row(value: object) -> dict[str, Any]:
    row = as_object(value)

    if row is None:
        raise Malformed("a session row is not an object")

    meta = as_object(row.get("_meta")) or {}
    return {
        "id": _name(row.get("sessionId")),
        "title": _optional(row.get("title")),
        "created": _optional(meta.get(META_CREATED)),
        "updated": _optional(row.get("updatedAt")),
        "job_id": _optional(meta.get(META_JOB)),
        "delegation": _optional(meta.get(META_DELEGATION)),
        "trigger": _trigger(meta.get(META_TRIGGER)),
    }


def _trigger(value: object) -> dict[str, Any] | None:
    """The trigger of contract 02 §13.2, rebuilt from its checked fields."""
    if value is None:
        return None

    trigger = as_object(value)

    if trigger is None or not set(trigger) <= _TRIGGER_KEYS or "kind" not in trigger:
        raise Malformed("a trigger that is not of its shape")

    built: dict[str, Any] = {"kind": _name(trigger["kind"])}
    built["name"] = _optional(trigger.get("name"))
    built["fired_at"] = _optional(trigger.get("fired_at"))

    if "chain" in trigger:
        chain = as_array(trigger["chain"])

        if not chain:
            raise Malformed("a trigger chain that is not a list of families")

        built["chain"] = [_name(one) for one in chain]

    return built


def _name(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise Malformed("an id that is not a text")

    if len(value.encode("utf-8", "surrogatepass")) > MAX_NAME_BYTES:
        raise Malformed("an id over the cap")

    return _clean(value)


def _optional(value: object, cap: int = MAX_NAME_BYTES) -> str | None:
    if value is None:
        return None

    if not isinstance(value, str):
        raise Malformed("a field that is not a text")

    return _clean(cut_bytes(value, cap))


def _clean(text: str) -> str:
    """A half of a surrogate pair has no UTF-8 form, so no answer holds it."""
    return text.encode("utf-8", "replace").decode("utf-8")


# ------------------------------------------------------------ session/load


@dataclass(slots=True)
class _Said:
    """Text of one kind, from consecutive chunks of that kind."""

    kind: str
    parts: list[str] = field(default_factory=list[str])
    size: int = 0

    def add(self, text: str) -> None:
        kept = cut_bytes(text, MAX_TEXT_BYTES - self.size)
        self.parts.append(kept)
        self.size += len(kept.encode("utf-8"))

    def view(self, position: int) -> dict[str, Any]:
        return {"position": position, "kind": self.kind, "text": "".join(self.parts)}


@dataclass(slots=True)
class _Tool:
    """One tool call: its title, its last status and its result text."""

    call_id: str
    title: str | None = None
    status: str | None = None
    result: str | None = None

    def view(self, position: int) -> dict[str, Any]:
        return {
            "position": position,
            "kind": "tool_call",
            "tool_call_id": self.call_id,
            "title": self.title,
            "status": self.status,
            "result": self.result,
        }


class Transcript:
    """The updates of one replay, folded into entries in replay order."""

    def __init__(self, session: str) -> None:
        self._session = session
        self._entries: list[_Said | _Tool] = []
        self._tools: dict[str, _Tool] = {}
        self._updates = 0
        self.truncated = False

    def add(self, params: dict[str, Any]) -> bool:
        """Fold one `session/update`. False once the cap is reached."""
        update = as_object(params.get("update")) or {}
        kind = update.get("sessionUpdate")

        if params.get("sessionId") != self._session or not isinstance(kind, str):
            raise Malformed("an update of another session, or of no kind")

        if kind in _TEXT_KINDS:
            self._said(_TEXT_KINDS[kind], _block_text(update.get("content")))
        elif kind in {"tool_call", "tool_call_update"}:
            self._tool(update)

        self._updates += 1
        self.truncated = self._updates >= MAX_UPDATES
        return not self.truncated

    def _said(self, kind: str, text: str) -> None:
        last = self._entries[-1] if self._entries else None

        if not isinstance(last, _Said) or last.kind != kind:
            last = _Said(kind)
            self._entries.append(last)

        last.add(text)

    def _tool(self, update: dict[str, Any]) -> None:
        call_id = _name(update.get("toolCallId"))
        tool = self._tools.get(call_id)

        if tool is None:
            tool = self._tools[call_id] = _Tool(call_id)
            self._entries.append(tool)

        if "title" in update:
            tool.title = _optional(update["title"])

        status = update.get("status")

        if status is not None:
            if status not in _TOOL_STATUSES:
                raise Malformed("a tool call status that ACP does not name")

            tool.status = str(status)

        if update.get("content") is not None:
            blocks = as_array(update["content"])

            if blocks is None:
                raise Malformed("tool call content that is not a list")

            texts = [_block_text((as_object(one) or {}).get("content")) for one in blocks]
            tool.result = cut_bytes("".join(texts), MAX_TEXT_BYTES)

    def page(self, page: Page) -> dict[str, Any]:
        """The entries from `page.start`, at most `page.limit` and the byte cap."""
        chosen: list[dict[str, Any]] = []
        room = MAX_PAGE_BYTES
        position = page.start

        for entry in self._entries[page.start : page.start + page.limit]:
            view = entry.view(position)
            size = len(json.dumps(view, ensure_ascii=False).encode("utf-8"))

            if chosen and size > room:
                break

            chosen.append(view)
            room -= size
            position += 1

        more = position < len(self._entries)
        return {
            "session_id": self._session,
            "entries": chosen,
            "next": position if more else None,
            "truncated": self.truncated,
        }


def _block_text(value: object) -> str:
    """The text of one content block. A block of another type has none."""
    block = as_object(value)

    if block is None or not isinstance(block.get("type"), str):
        raise Malformed("a content block with no type")

    if block["type"] != "text":
        return ""

    text = block.get("text")

    if not isinstance(text, str):
        raise Malformed("a text block with no text")

    return _clean(text)


def _line(message: dict[str, Any]) -> str:
    return json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n"
