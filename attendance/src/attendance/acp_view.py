"""The read-only view of one family's sessions, for the noticeboard.

    GET /families/{f}/sessions        vmm list, no boot
    GET /families/{f}/sessions/{id}   vmm ACP stream: initialize, session/load, close

The page of sessions comes from the host session list, with no boot
(contract 02 §7 rule 1). A transcript comes from the facade of the
session: the view opens the session's vmm stream, sends `initialize` and
`session/load`, and closes the stream. So the read boots or restores the
VM, and it can meet `host_full` or `busy` (decision CH21-9, CH83 rule 4).
It never sends `session/prompt`.

Every line from the stream is a claim. A line that is not one strict
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

from creche_wire import (
    AutoOrigin,
    DelegateOrigin,
    FamilyName,
    ListCursor,
    ListRow,
    ListSessions,
    TimerTrigger,
    WebhookTrigger,
)

from .acp_link import (
    JSONRPC,
    MAX_ACP_LINE_BYTES,
    METHOD_UPDATE,
    AcpHandshakeError,
    Malformed,
    Notice,
    RpcError,
    handshake,
    parse_message,
)
from .atomic import as_array, as_object
from .channel import Channel, ChannelClosed
from .clock import from_ms, rfc3339
from .errors import ApiError, ErrorCode
from .lines import cut_bytes
from .vmm import NOT_FOUND, Vmm, VmmRefused

METHOD_LOAD = "session/load"

#: The cap of one text of the transcript: the facade's own forward cap.
MAX_TEXT_BYTES = 65_536
#: The cap of an id, a title, a time or a cursor.
MAX_NAME_BYTES = 256
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

#: The vmm code of a session whose stream another holder holds (decision
#: CH83 rule 4).
_BUSY = "busy"

_TOOL_STATUSES = frozenset({"pending", "in_progress", "completed", "failed"})
_TEXT_KINDS = {
    "user_message_chunk": "user",
    "agent_message_chunk": "agent",
    "agent_thought_chunk": "thought",
}
#: The decimal text of the vmm's `ListCursor`, 1 to 2^53 - 1.
_CURSOR_RE = re.compile(r"^[1-9][0-9]{0,15}\Z")
_CURSOR_MAX = 2**53 - 1
_COUNT_RE = re.compile(r"^[0-9]{1,9}\Z")
_LOAD_ID = 1


@dataclass(slots=True, frozen=True)
class Page:
    """Which entries of a transcript one answer holds."""

    start: int = 0
    limit: int = DEFAULT_PAGE_ENTRIES


def read_cursor(query: Mapping[str, str]) -> ListCursor | None:
    """The cursor of a page that this view gave: the `next` of the vmm."""
    cursor = query.get("cursor")

    if cursor is None:
        return None

    if not _CURSOR_RE.match(cursor) or int(cursor) > _CURSOR_MAX:
        raise ApiError(ErrorCode.BAD_REQUEST, "cursor is not one that a page gave")

    return ListCursor(int(cursor))


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


async def list_sessions(vmm: Vmm, family: str, cursor: ListCursor | None) -> dict[str, Any]:
    """One page of the host list, newest created first (contract 02 §7 rule 1)."""
    query = ListSessions(cursor=cursor, cwd=None, family=FamilyName(family))

    try:
        page = await vmm.list_page(query)
    except VmmRefused as error:
        raise _unavailable(family, error.code) from error

    cursor_text = str(page.next.root) if page.next is not None else None
    return {"sessions": [_view_row(row) for row in page.rows], "next_cursor": cursor_text}


async def load_transcript(
    vmm: Vmm, family: str, session: str, cwd: str, page: Page
) -> dict[str, Any]:
    """The replay of `session/load`, folded into entries, one page of them."""
    try:
        stream = await vmm.open_acp(family, session, MAX_ACP_LINE_BYTES)
    except VmmRefused as error:
        raise _vmm_refusal(family, error) from error

    transcript = Transcript(session)
    params: dict[str, Any] = {"sessionId": session, "cwd": cwd, "mcpServers": []}

    try:
        await _read(stream, family, params, transcript.add)
    finally:
        await stream.close()

    return transcript.page(page)


async def _read(
    stream: Channel,
    family: str,
    params: dict[str, Any],
    on_update: Callable[[dict[str, Any]], bool],
) -> None:
    """`initialize`, then `session/load`, until the answer or the update cap."""
    try:
        async with asyncio.timeout(READ_DEADLINE_S):
            await _exchange(stream, params, on_update)
    except (Malformed, AcpHandshakeError, ChannelClosed, TimeoutError) as error:
        raise _unavailable(family, str(error) or type(error).__name__) from error
    except _Refused as refused:
        raise _refusal(family, refused.error) from refused


async def _exchange(
    stream: Channel, params: dict[str, Any], on_update: Callable[[dict[str, Any]], bool]
) -> None:
    await handshake(stream)
    await stream.send(
        _line({"jsonrpc": JSONRPC, "id": _LOAD_ID, "method": METHOD_LOAD, "params": params})
    )

    while True:
        message = parse_message(await _next(stream))

        if isinstance(message, Notice):
            if message.method != METHOD_UPDATE:
                raise Malformed(f"a notification this read does not take: {message.method}")

            if not on_update(message.params):
                return

            continue

        if message.id != _LOAD_ID:
            raise Malformed("an answer to no request")

        if message.error is not None:
            raise _Refused(message.error)

        return


async def _next(stream: Channel) -> str:
    raw = await stream.receive()

    if raw is None:
        raise ChannelClosed("the stream ended")

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


def _vmm_refusal(family: str, error: VmmRefused) -> ApiError:
    """`not_found` is 404 with no boot (decision CH21-9). `busy` is 409
    (decision CH83 rule 4). Each other refusal is `sandbox_unavailable`."""
    if error.code == NOT_FOUND:
        return ApiError(ErrorCode.NOT_FOUND, "no such session", family=family)

    if error.code == _BUSY:
        return ApiError(ErrorCode.BUSY, "another holder holds the session", family=family)

    return _unavailable(family, error.code)


def _unavailable(family: str, why: str) -> ApiError:
    return ApiError(
        ErrorCode.SANDBOX_UNAVAILABLE,
        f"family {family} gave no answer to read",
        family=family,
        detail={"reason": _clean(cut_bytes(why, MAX_NAME_BYTES))},
    )


# ------------------------------------------------------------ the host list


def _view_row(row: ListRow) -> dict[str, Any]:
    """Contract 02 §7 rule 2: one view row of one host row."""
    origin = row.origin.root
    job: str | None = None
    delegation: str | None = None
    trigger: dict[str, Any] | None = None

    if isinstance(origin, DelegateOrigin):
        job = origin.delegate.job.root
        delegation = origin.delegate.delegation.root
    elif isinstance(origin, AutoOrigin):
        job = origin.auto.job.root
        trigger = _trigger(origin)

    return {
        "id": row.id.root,
        "title": row.title.root if row.title is not None else None,
        "created": rfc3339(from_ms(row.created_at.root)),
        "updated": rfc3339(from_ms(row.updated_at.root)),
        "job_id": job,
        "delegation": delegation,
        "trigger": trigger,
    }


def _trigger(origin: AutoOrigin) -> dict[str, Any]:
    """The trigger of contract 02 §13.2 with no `fired_at`: the `created`
    time stands in for it (decision CH21-4a)."""
    fired = origin.auto.trigger.root

    if isinstance(fired, TimerTrigger):
        return {"kind": "timer", "name": None}

    if isinstance(fired, WebhookTrigger):
        return {"kind": "webhook", "name": fired.webhook.name.root}

    dispatch = fired.dispatch
    chain = [name.root for name in dispatch.chain.root]
    return {"kind": "dispatch", "name": dispatch.name.root, "chain": chain}


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
