"""`quiet/chaperone.py` against `httpx.MockTransport`: the two MCP tool calls
the gate makes as the family, and what each failure answers."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from agent_door_trigger.quiet.board import fingerprint
from agent_door_trigger.quiet.chaperone import (
    JOB_LIMIT,
    MCP_PATH,
    PROTOCOL_VERSION,
    HttpFamilyReads,
    family_token,
)
from agent_door_trigger.quiet.decide import Jobs

TOKEN = "ab" * 32
SESSION = "6f1c0a52-90d4-4d4b-8d1e-3f0a1d7c2b19"
#: More levels than the JSON parser of each supported Python reads. No reader
#: of this module has a size cap, so the text reaches the parser.
TOO_DEEP = 400_000
DEEP_JSON = "[" * TOO_DEEP + "]" * TOO_DEEP

SURVEY: dict[str, Any] = {
    "project": "House",
    "columns": {"TODO": [{"id": 12, "points": 3, "refined": True, "epic": "finance"}]},
}

#: The first event of each stream of the front door: no data, a retry hint.
PRIMING = "id: 0\nretry: 3000\ndata: \n\n"


def _text_result(text: str, *, is_error: bool = False) -> dict[str, object]:
    """An MCP `CallToolResult` with one text block, as the front door renders it."""
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


@dataclass
class FrontDoor:
    """The chaperone's front door: `initialize`, the `initialized`
    notification, then `tools/call`. Each answer is an SSE stream unless
    `as_json` is set."""

    results: dict[str, dict[str, object]] = field(default_factory=dict[str, dict[str, object]])
    as_json: bool = False
    seen: list[httpx.Request] = field(default_factory=list[httpx.Request])

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        message = json.loads(request.content)
        method = message["method"]

        if method == "notifications/initialized":
            return httpx.Response(202)

        if method == "initialize":
            info: dict[str, object] = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "rmcp", "version": "3.7.0"},
            }
            return self._answer(message["id"], info, {"Mcp-Session-Id": SESSION})

        return self._answer(message["id"], self.results[message["params"]["name"]], {})

    def methods(self) -> list[str]:
        return [json.loads(one.content)["method"] for one in self.seen]

    def _answer(
        self, ident: int, result: dict[str, object], headers: dict[str, str]
    ) -> httpx.Response:
        text = json.dumps({"jsonrpc": "2.0", "id": ident, "result": result})
        if self.as_json:
            return httpx.Response(200, text=text, headers=headers | _type("application/json"))

        stream = f"{PRIMING}data: {text}\n\n"
        return httpx.Response(200, text=stream, headers=headers | _type("text/event-stream"))


def _type(value: str) -> dict[str, str]:
    return {"Content-Type": value}


def _reads(
    handler: Callable[[httpx.Request], httpx.Response], token: str | None = TOKEN
) -> HttpFamilyReads:
    client = httpx.Client(base_url="http://chaperone", transport=httpx.MockTransport(handler))
    return HttpFamilyReads(client, token)


def _jobs_door(jobs: list[dict[str, object]]) -> FrontDoor:
    return FrontDoor(results={"attendance__job_status": _text_result(json.dumps({"jobs": jobs}))})


def _survey_door(text: str, **flags: bool) -> FrontDoor:
    return FrontDoor(results={"board-lead__survey_board": _text_result(text, **flags)})


def test_quiet_calls_survey_over_mcp() -> None:
    door = _survey_door(json.dumps(SURVEY, indent=2))

    assert _reads(door).board("board-lead") == fingerprint(SURVEY)

    assert door.methods() == ["initialize", "notifications/initialized", "tools/call"]
    assert all(one.url.path == MCP_PATH for one in door.seen)
    assert all(one.headers["authorization"] == f"Bearer {TOKEN}" for one in door.seen)
    assert all("text/event-stream" in one.headers["accept"] for one in door.seen)

    initialize, initialized, call = (json.loads(one.content) for one in door.seen)
    assert initialize["params"]["protocolVersion"] == PROTOCOL_VERSION
    assert "id" not in initialized
    assert call["params"] == {"name": "board-lead__survey_board", "arguments": {}}
    for later in door.seen[1:]:
        assert later.headers["mcp-session-id"] == SESSION
        assert later.headers["mcp-protocol-version"] == PROTOCOL_VERSION


def test_the_jobs_split_into_live_and_ended() -> None:
    door = _jobs_door(
        [
            {"session": "auto-A", "status": "ended", "outcome": {"status": "ok"}},
            {"session": "auto-B", "status": "queued", "outcome": None},
            {"session": "auto-C", "status": "running", "outcome": None},
        ]
    )

    assert _reads(door).jobs() == Jobs(
        live=frozenset({"auto-B", "auto-C"}), ended=frozenset({"auto-A"})
    )
    call = json.loads(door.seen[-1].content)
    assert call["params"] == {"name": "attendance__job_status", "arguments": {"limit": JOB_LIMIT}}


def test_both_reads_share_one_session() -> None:
    door = _jobs_door([])
    door.results |= _survey_door(json.dumps(SURVEY)).results
    reads = _reads(door)

    reads.board("board-lead")
    reads.jobs()

    assert door.methods() == [
        "initialize",
        "notifications/initialized",
        "tools/call",
        "tools/call",
    ]


def test_a_json_answer_is_read_too() -> None:
    door = _survey_door(json.dumps(SURVEY))
    door.as_json = True

    assert _reads(door).board("board-lead") == fingerprint(SURVEY)


def test_a_denied_call_reads_nothing() -> None:
    """The front door opens a session for any bearer. It denies an unknown
    bearer at the call, as an error result."""
    door = _survey_door("chaperone denied this call: unknown_credential", is_error=True)

    assert _reads(door).board("board-lead") is None


def test_an_http_error_reads_nothing_and_asks_once() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(401, text="unauthorized")

    reads = _reads(handler)
    assert reads.board("board-lead") is None
    assert reads.jobs() is None
    assert len(seen) == 1


def test_an_rpc_error_reads_nothing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        error = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32600, "message": "no"}}
        return httpx.Response(200, json=error)

    assert _reads(handler).board("board-lead") is None


def test_a_stream_with_no_answer_reads_nothing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=PRIMING, headers=_type("text/event-stream"))

    assert _reads(handler).jobs() is None


def test_a_front_door_that_does_not_answer_reads_nothing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    assert _reads(handler).board("board-lead") is None


def test_no_token_asks_nothing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no call without a token")

    reads = _reads(handler, token=None)
    assert reads.board("board-lead") is None
    assert reads.jobs() is None


def test_a_board_answer_that_is_not_a_survey_reads_nothing() -> None:
    door = _survey_door('{"refused": true, "reason": "no board"}')

    assert _reads(door).board("board-lead") is None


def test_a_board_answer_that_nests_too_deep_reads_nothing() -> None:
    assert _reads(_survey_door(DEEP_JSON)).board("board-lead") is None


def test_an_answer_that_nests_too_deep_reads_nothing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text=f"data: {DEEP_JSON}\n\n", headers=_type("text/event-stream")
        )

    reads = _reads(handler)
    assert reads.board("board-lead") is None
    assert reads.jobs() is None


def test_a_job_row_with_no_status_reads_nothing() -> None:
    assert _reads(_jobs_door([{"session": "auto-A"}])).jobs() is None


def test_the_token_comes_from_creds_json(tmp_path: Path) -> None:
    creds = tmp_path / "scrum-lead" / "creds" / "creds.json"
    creds.parent.mkdir(parents=True)
    creds.write_text(json.dumps({"pep_token": TOKEN, "litellm_key": "k"}), encoding="utf-8")

    assert family_token(tmp_path, "scrum-lead") == TOKEN


def test_no_creds_is_no_token(tmp_path: Path) -> None:
    assert family_token(tmp_path, "scrum-lead") is None


def test_a_creds_file_that_nests_too_deep_is_no_token(tmp_path: Path) -> None:
    creds = tmp_path / "scrum-lead" / "creds" / "creds.json"
    creds.parent.mkdir(parents=True)
    creds.write_text(DEEP_JSON, encoding="utf-8")

    assert family_token(tmp_path, "scrum-lead") is None
