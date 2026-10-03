"""`quiet/chaperone.py` against `httpx.MockTransport`: the two calls the gate
makes as the family, and what each failure answers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
from agent_door_trigger.quiet.board import fingerprint
from agent_door_trigger.quiet.chaperone import JOB_LIMIT, HttpFamilyReads, family_token
from agent_door_trigger.quiet.decide import Jobs

TOKEN = "family-token-value"
SURVEY: dict[str, Any] = {
    "project": "House",
    "columns": {"TODO": [{"id": 12, "points": 3, "refined": True, "epic": "finance"}]},
}


def _reads(handler: Any, token: str | None = TOKEN) -> HttpFamilyReads:
    client = httpx.Client(base_url="http://chaperone", transport=httpx.MockTransport(handler))
    return HttpFamilyReads(client, token)


def _ok(result: object) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result})


def test_the_board_is_the_surveys_fingerprint() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        # An MCP tool answers text: the survey's own JSON.
        return _ok(json.dumps(SURVEY, indent=2))

    assert _reads(handler).board("board-lead") == fingerprint(SURVEY)
    assert seen["path"] == "/call"
    assert seen["auth"] == f"Bearer {TOKEN}"
    assert seen["body"] == {"tool": "board-lead__survey_board", "args": {}}


def test_the_jobs_split_into_live_and_ended() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        jobs = [
            {"session": "auto-A", "status": "ended", "outcome": {"status": "ok"}},
            {"session": "auto-B", "status": "waiting-approval", "outcome": None},
            {"session": "auto-C", "status": "dispatched", "outcome": None},
        ]
        return _ok({"jobs": jobs})

    assert _reads(handler).jobs() == Jobs(
        live=frozenset({"auto-B", "auto-C"}), ended=frozenset({"auto-A"})
    )
    assert seen["body"] == {"tool": "job_status", "args": {"limit": JOB_LIMIT}}


def test_a_refusal_reads_nothing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"ok": False, "reason": "tool_not_granted"})

    reads = _reads(handler)
    assert reads.board("board-lead") is None
    assert reads.jobs() is None


def test_a_pep_that_does_not_answer_reads_nothing() -> None:
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
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok('{"refused": true, "reason": "no board"}')

    assert _reads(handler).board("board-lead") is None


def test_a_job_row_with_no_status_reads_nothing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok({"jobs": [{"session": "auto-A"}]})

    assert _reads(handler).jobs() is None


def test_the_token_comes_from_creds_json(tmp_path: Path) -> None:
    creds = tmp_path / "scrum-lead" / "creds" / "creds.json"
    creds.parent.mkdir(parents=True)
    creds.write_text(json.dumps({"pep_token": TOKEN, "litellm_key": "k"}), encoding="utf-8")

    assert family_token(tmp_path, "scrum-lead") == TOKEN


def test_no_creds_is_no_token(tmp_path: Path) -> None:
    assert family_token(tmp_path, "scrum-lead") is None
