"""The family's own two reads, as MCP tool calls at the chaperone, with the
family's own token (contract 01 §3.15 rule 4):

    tools/call <board>__survey_board {}              -> fingerprint
    tools/call attendance__job_status {"limit": 200} -> live, ended

The front door of the chaperone serves MCP over streamable HTTP at `/mcp`
(chaperone `docs/design.md` §7.6). One exchange is three POSTs:
`initialize`, the `notifications/initialized` notification, then one
`tools/call` for each read. Each POST after `initialize` carries the session
ID that `initialize` answered. An answer is one JSON-RPC message, as JSON or
as an SSE stream. The chaperone drops an idle session by itself, so this
client never closes one.

The token is `pep_token` in the family's `creds.json` (contract 03 §12.2),
the file `caregiver` writes and the sandbox reads. It rides in the
Authorization header and nowhere else (invariant 13).

Every failure answers None, and the gate wakes on it: a check that cannot
see is not quiet."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol

import httpx
from agent_family import CALL_SEPARATOR, SURVEY_TOOL, Verb

from ..untrusted import as_object, field_text, is_list, is_object
from .board import fingerprint
from .decide import Jobs

_LOG = logging.getLogger(__name__)

#: The path of the front door (chaperone `docs/design.md` §7.6).
MCP_PATH: Final = "/mcp"

#: The MCP version this client asks for. The front door serves it with a
#: session ID.
PROTOCOL_VERSION: Final = "2025-06-18"

SESSION_HEADER: Final = "Mcp-Session-Id"
PROTOCOL_HEADER: Final = "MCP-Protocol-Version"
ACCEPT: Final = "application/json, text/event-stream"

_OK: Final = 200
_ACCEPTED: Final = 202

#: The upstream that holds the verbs of `attendance`. Its tools keep the
#: names of the verbs.
ATTENDANCE: Final = "attendance"

#: `attendance`'s cap of a page. Newest first, so a job old enough to fall
#: off the page ended long ago: a turn's own limit is an hour.
JOB_LIMIT: Final = 200

#: `attendance`'s one terminal status. Every other one is live.
ENDED: Final = "ended"

CONNECT_TIMEOUT_S: Final = 5.0
#: A survey reads the whole board from Vikunja. The unit's 120 s start limit
#: must still cover the exchange and the firing after it.
READ_TIMEOUT_S: Final = 20.0

#: Contract 03 §12.2: `creds/creds.json` under the family's state directory.
CREDS_PATH: Final = "{family}/creds/creds.json"
TOKEN_FIELD: Final = "pep_token"


class FamilyReads(Protocol):
    """What the gate asks the chaperone, as the family."""

    def board(self, server: str) -> str | None:
        """The board's fingerprint, or None."""
        ...

    def jobs(self) -> Jobs | None:
        """The jobs this family enqueued, or None."""
        ...


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One `tools/call`: the exposed name `<upstream>__<tool>` and its
    arguments."""

    name: str
    arguments: Mapping[str, object] = field(default_factory=dict[str, object])

    @classmethod
    def survey(cls, server: str) -> ToolCall:
        return cls(f"{server}{CALL_SEPARATOR}{SURVEY_TOOL}")

    @classmethod
    def job_status(cls) -> ToolCall:
        return cls(f"{ATTENDANCE}{CALL_SEPARATOR}{Verb.JOB_STATUS}", {"limit": JOB_LIMIT})


@dataclass(frozen=True, slots=True)
class ToolText:
    """A tool result that is not an error: the text of its text blocks."""

    text: str


class Failure(StrEnum):
    """Why one read got no result."""

    NO_TOKEN = "no token"
    UNREACHABLE = "the chaperone did not answer"
    STATUS = "the chaperone answered an HTTP error"
    MALFORMED = "the answer is not one this client reads"
    RPC_ERROR = "the answer is a JSON-RPC error"
    TOOL_ERROR = "the tool answered an error"


@dataclass(frozen=True, slots=True)
class McpFailure:
    """A failure, and what the log may say about it. Never a body: a body
    can hold the family's own data."""

    failure: Failure
    detail: str = ""


@dataclass(frozen=True, slots=True)
class _Session:
    """An open exchange. None when the front door gave no session ID."""

    id: str | None


@dataclass(frozen=True, slots=True)
class _Reply:
    """The `result` of one answer, and the session ID it carried."""

    result: dict[str, object]
    session_id: str | None


class McpClient:
    """`tools/call` at the front door. The first call opens the exchange, and
    each later call reuses it, or its failure."""

    def __init__(self, client: httpx.Client, token: str | None) -> None:
        self._client = client
        self._token = token
        self._opened: _Session | McpFailure | None = None
        self._next_id = 0

    def call(self, call: ToolCall) -> ToolText | McpFailure:
        session = self._open()
        if isinstance(session, McpFailure):
            return session

        params: dict[str, object] = {"name": call.name, "arguments": dict(call.arguments)}
        answer = self._request("tools/call", params, session)
        if isinstance(answer, McpFailure):
            return answer

        return _tool_text(answer.result)

    def _open(self) -> _Session | McpFailure:
        if self._opened is None:
            self._opened = self._initialize()

        return self._opened

    def _initialize(self) -> _Session | McpFailure:
        if self._token is None:
            return McpFailure(Failure.NO_TOKEN)

        params: dict[str, object] = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "agent-trigger", "version": "0"},
        }
        answer = self._request("initialize", params, _Session(id=None))
        if isinstance(answer, McpFailure):
            return answer

        session = _Session(id=answer.session_id)
        notified = self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
        if isinstance(notified, McpFailure):
            return notified

        return session

    def _request(
        self, method: str, params: dict[str, object], session: _Session
    ) -> _Reply | McpFailure:
        ident = self._ident()
        message: dict[str, object] = {"jsonrpc": "2.0", "id": ident, "method": method}
        message["params"] = params
        response = self._post(message, session)
        if isinstance(response, McpFailure):
            return response

        result = _result(response, ident)
        if isinstance(result, McpFailure):
            return result

        return _Reply(result=result, session_id=response.headers.get(SESSION_HEADER))

    def _post(self, message: dict[str, object], session: _Session) -> httpx.Response | McpFailure:
        headers = {"Authorization": f"Bearer {self._token}", "Accept": ACCEPT}
        if session.id is not None:
            headers[SESSION_HEADER] = session.id
            headers[PROTOCOL_HEADER] = PROTOCOL_VERSION

        try:
            response = self._client.post(
                MCP_PATH,
                json=message,
                headers=headers,
                timeout=httpx.Timeout(CONNECT_TIMEOUT_S, read=READ_TIMEOUT_S),
            )
        except httpx.HTTPError as exc:
            return McpFailure(Failure.UNREACHABLE, type(exc).__name__)

        if response.status_code not in (_OK, _ACCEPTED):
            return McpFailure(Failure.STATUS, str(response.status_code))

        return response

    def _ident(self) -> int:
        self._next_id += 1
        return self._next_id


class HttpFamilyReads:
    """`FamilyReads` over MCP. A None token reads nothing."""

    def __init__(self, client: httpx.Client, token: str | None) -> None:
        self._mcp = McpClient(client, token)

    def board(self, server: str) -> str | None:
        answer = self._text(ToolCall.survey(server))
        if answer is None:
            return None

        # The tool answers text: the survey's own JSON.
        survey = _json(answer)
        return fingerprint(survey) if survey is not None else None

    def jobs(self) -> Jobs | None:
        answer = self._text(ToolCall.job_status())
        rows = as_object(_json(answer) if answer is not None else None).get("jobs")
        if not is_list(rows):
            return None

        live: set[str] = set()
        ended: set[str] = set()
        for row in rows:
            body = as_object(row)
            session = field_text(body, "session")
            status = field_text(body, "status")
            if not session or not status:
                return None

            (ended if status == ENDED else live).add(session)

        return Jobs(live=frozenset(live), ended=frozenset(ended))

    def _text(self, call: ToolCall) -> str | None:
        answer = self._mcp.call(call)
        if isinstance(answer, McpFailure):
            _LOG.warning("quiet: %s: %s %s", call.name, answer.failure, answer.detail)
            return None

        return answer.text


def family_token(families_dir: Path, family: str) -> str | None:
    """The family's chaperone bearer, or None. Never logged, never raised."""
    path = families_dir / CREDS_PATH.format(family=family)
    try:
        body: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
        _LOG.warning("quiet: %s: no readable creds.json (%s)", family, type(exc).__name__)
        return None

    return field_text(as_object(body), TOKEN_FIELD) or None


def _result(response: httpx.Response, ident: int) -> dict[str, object] | McpFailure:
    """The `result` of the answer with the id `ident`."""
    if response.headers.get("content-type", "").startswith("application/json"):
        texts: Iterator[str] = iter((response.text,))
    else:
        texts = _sse_data(response.text)

    for text in texts:
        message = as_object(_json(text))
        if message.get("id") != ident:
            continue

        if "error" in message:
            code = as_object(message["error"]).get("code")
            return McpFailure(Failure.RPC_ERROR, str(code))

        result = message.get("result")
        return result if is_object(result) else McpFailure(Failure.MALFORMED, "no result")

    return McpFailure(Failure.MALFORMED, f"no answer with the id {ident}")


def _sse_data(stream: str) -> Iterator[str]:
    """The data of each event of an SSE stream. LF ends a line, and a CR
    before it goes. U+2028 is legal inside a JSON string, so a generic line
    reader would split one message into two."""
    data: list[str] = []
    for raw in stream.split("\n"):
        line = raw.removesuffix("\r")
        if not line:
            if data:
                yield "\n".join(data)
            data = []
            continue

        name, _, value = line.partition(":")
        if name == "data":
            data.append(value.removeprefix(" "))

    if data:
        yield "\n".join(data)


def _tool_text(result: dict[str, object]) -> ToolText | McpFailure:
    """The text blocks of a `CallToolResult`, joined. The front door renders
    a denial, a gate and a fault as an error result."""
    if result.get("isError") is True:
        return McpFailure(Failure.TOOL_ERROR)

    blocks = result.get("content")
    if not is_list(blocks):
        return McpFailure(Failure.MALFORMED, "no content")

    texts = [
        field_text(block, "text")
        for block in map(as_object, blocks)
        if field_text(block, "type") == "text"
    ]
    return ToolText("".join(texts)) if texts else McpFailure(Failure.MALFORMED, "no text")


def _json(text: str) -> object | None:
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        # RecursionError: a text that nests too deep is not a ValueError.
        return None
