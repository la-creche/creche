"""The approval page, end to end through the app.

The approval plugin is a fake behind `httpx.MockTransport`: it holds open
gates, checks the bearer and answers `/pending` and `/resume` with the
statuses of the real plugin. No socket, no chaperone.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from noticeboard.app import build_app
from noticeboard.approvals import ApprovalChannel, is_job
from noticeboard.config import DEFAULT_PAGE_SIZE, Config, ConfigError, from_env
from noticeboard.security import ACCESS_HEADER, CSRF_COOKIE, CSRF_FIELD
from noticeboard.sessions import SessionReader
from noticeboard_helpers import (
    AUDIT_FILE,
    NOW,
    FakeAttendance,
    make_registry,
    make_state_root,
    write_token,
)

KEY = "k" * 40
HOST = "noticeboard.example.test"
BEARER = "b" * 40
JOB = "7zzz0123456789abcdefghjkmn"
GATE = "ff00123456789abc"
SUMMARY = 'vikunja__create_task {"title":"buy milk"}'


class FakePlugin:
    """The two routes of `ha-approval`, with its statuses."""

    def __init__(self) -> None:
        self.open: dict[str, dict[str, Any]] = {}
        self.decided: dict[str, str] = {}
        self.resumed: list[dict[str, Any]] = []
        self.bearers: list[str] = []
        self.down = False

    def add(self, job: str = JOB, gate: str = GATE, **extra: Any) -> None:
        self.open[job] = {
            "job": job,
            "gate": gate,
            "agent": "librechat",
            "summary": SUMMARY,
            "expires_in_s": 421,
            **extra,
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("refused", request=request)

        self.bearers.append(request.headers.get("authorization", ""))

        if request.headers.get("authorization") != f"Bearer {BEARER}":
            return httpx.Response(401, json={"status": "unauthorized"})

        if request.method == "GET" and request.url.path.startswith("/pending/"):
            job = request.url.path.removeprefix("/pending/")
            found = self.open.get(job)

            if found is None:
                return httpx.Response(404, json={"status": "unknown gate"})

            return httpx.Response(200, json=found)

        if request.method == "POST" and request.url.path == "/resume":
            return self._resume(json.loads(request.content))

        return httpx.Response(404, json={"status": "no route"})

    def _resume(self, body: dict[str, Any]) -> httpx.Response:
        self.resumed.append(body)
        job, decision = body["job"], body["decision"]
        earlier = self.decided.get(job)

        if earlier is not None:
            if earlier == decision:
                return httpx.Response(200, json={"status": "already_decided"})

            return httpx.Response(409, json={"status": "already_decided_otherwise"})

        if job not in self.open:
            return httpx.Response(404, json={"status": "unknown gate"})

        self.decided[job] = decision
        del self.open[job]

        return httpx.Response(200, json={"status": "resumed"})


class Harness:
    def __init__(self, client: TestClient, plugin: FakePlugin) -> None:
        self.client = client
        self.plugin = plugin

    def get(self, path: str):
        return self.client.get(path, headers={ACCESS_HEADER: KEY})

    def post(self, path: str, body: dict[str, str], *, token: str | None = None):
        csrf = token if token is not None else (self.client.cookies.get(CSRF_COOKIE) or "")
        headers = {
            ACCESS_HEADER: KEY,
            "Origin": f"http://{HOST}",
            "Host": HOST,
            "Content-Type": "application/x-www-form-urlencoded",
        }

        return self.client.post(
            path, data={CSRF_FIELD: csrf, **body}, headers=headers, follow_redirects=False
        )


def _config(tmp_path: Path) -> Config:
    state = make_state_root(tmp_path)

    return Config(
        bind="127.0.0.1",
        port=8370,
        state_root=state,
        registry_dir=make_registry(tmp_path),
        attendance_socket=None,
        attendance_url="http://sessiond",
        page_size=DEFAULT_PAGE_SIZE,
        audit_file=state / AUDIT_FILE,
        cookie_secure=False,
        access_key=KEY,
    )


def _harness(tmp_path: Path, channel: ApprovalChannel, plugin: FakePlugin) -> Iterator[Harness]:
    config = _config(tmp_path)
    reader = SessionReader(transport=FakeAttendance(), token_file=write_token(config.state_root))
    app = build_app(config, reader, lambda: NOW, channel)

    with TestClient(app, base_url=f"http://{HOST}") as client:
        yield Harness(client, plugin)


def _channel(tmp_path: Path, plugin: FakePlugin, bearer: str = BEARER) -> ApprovalChannel:
    token = tmp_path / "approval.token"
    token.write_text(f"{bearer}\n", encoding="utf-8")
    client = httpx.Client(
        base_url="http://plugin.example.test", transport=httpx.MockTransport(plugin.handle)
    )

    return ApprovalChannel(client=client, token_file=token)


@pytest.fixture
def board(tmp_path: Path) -> Iterator[Harness]:
    plugin = FakePlugin()
    yield from _harness(tmp_path, _channel(tmp_path, plugin), plugin)


def test_an_open_gate_shows_the_request_and_two_buttons(board: Harness) -> None:
    board.plugin.add()

    answer = board.get(f"/approvals/{JOB}")

    assert answer.status_code == 200
    assert "librechat needs approval" in answer.text
    assert "vikunja__create_task" in answer.text
    assert 'value="approve"' in answer.text
    assert 'value="deny"' in answer.text
    assert "expires in 7 min 1 s" in answer.text
    assert BEARER not in answer.text
    assert board.plugin.bearers == [f"Bearer {BEARER}"]


def test_a_gone_request_is_a_404_page_with_no_button(board: Harness) -> None:
    answer = board.get(f"/approvals/{JOB}")

    assert answer.status_code == 404
    assert "This request is gone" in answer.text
    assert "<button" not in answer.text


def test_text_that_is_not_a_job_never_reaches_the_plugin(board: Harness) -> None:
    for bad in ("-" * 26, "x" * 26, "8" + "0" * 25, JOB.upper(), JOB + "0"):
        assert board.get(f"/approvals/{bad}").status_code == 404

    assert board.plugin.bearers == []


def test_approve_sends_the_tap_and_the_page_is_then_gone(board: Harness) -> None:
    board.plugin.add()
    board.get(f"/approvals/{JOB}")

    answer = board.post(f"/approvals/{JOB}", {"decision": "approve"})

    assert answer.status_code == 200
    assert "Approved." in answer.text
    assert board.plugin.resumed == [{"job": JOB, "gate": GATE, "decision": "approve"}]
    assert board.get(f"/approvals/{JOB}").status_code == 404


def test_deny_sends_deny(board: Harness) -> None:
    board.plugin.add()
    board.get(f"/approvals/{JOB}")

    answer = board.post(f"/approvals/{JOB}", {"decision": "deny"})

    assert "Denied." in answer.text
    assert board.plugin.resumed == [{"job": JOB, "gate": GATE, "decision": "deny"}]


def test_a_post_without_the_csrf_token_sends_nothing(board: Harness) -> None:
    board.plugin.add()
    board.get(f"/approvals/{JOB}")

    answer = board.post(f"/approvals/{JOB}", {"decision": "approve"}, token="forged")

    assert answer.status_code == 403
    assert board.plugin.resumed == []


def test_a_post_with_another_word_sends_nothing(board: Harness) -> None:
    board.plugin.add()
    board.get(f"/approvals/{JOB}")

    answer = board.post(f"/approvals/{JOB}", {"decision": "allow"})

    assert answer.status_code == 400
    assert board.plugin.resumed == []


def test_a_post_after_the_card_answered_says_gone_and_sends_nothing(board: Harness) -> None:
    board.get(f"/approvals/{JOB}")

    answer = board.post(f"/approvals/{JOB}", {"decision": "approve"})

    assert answer.status_code == 200
    assert "This request is gone" in answer.text
    assert board.plugin.resumed == []


def test_a_contradicting_tap_that_came_first_stands(board: Harness) -> None:
    board.plugin.add()
    board.get(f"/approvals/{JOB}")
    # The card's Deny lands between the page's read and its post.
    board.plugin.decided[JOB] = "deny"

    answer = board.post(f"/approvals/{JOB}", {"decision": "approve"})

    assert "The other answer came first, and it stands." in answer.text


def test_an_answer_for_another_job_is_a_problem_and_no_button(board: Harness) -> None:
    board.plugin.open[JOB] = {
        "job": "0" * 26,
        "gate": GATE,
        "agent": "librechat",
        "summary": SUMMARY,
        "expires_in_s": 30,
    }

    answer = board.get(f"/approvals/{JOB}")

    assert answer.status_code == 200
    assert "names another gate" in answer.text
    assert "<button" not in answer.text


def test_a_plugin_that_does_not_answer_is_a_problem(board: Harness) -> None:
    board.plugin.down = True

    answer = board.get(f"/approvals/{JOB}")

    assert answer.status_code == 200
    assert "cannot reach the approval plugin: ConnectError" in answer.text
    assert "<button" not in answer.text


def test_a_wrong_bearer_is_a_problem_that_names_no_bearer(tmp_path: Path) -> None:
    plugin = FakePlugin()
    plugin.add()
    channel = _channel(tmp_path, plugin, bearer="w" * 40)

    for board in _harness(tmp_path, channel, plugin):
        answer = board.get(f"/approvals/{JOB}")

        assert "answered HTTP 401" in answer.text
        assert "w" * 40 not in answer.text


def test_a_channel_that_is_not_set_up_decides_nothing(tmp_path: Path) -> None:
    plugin = FakePlugin()
    plugin.add()
    channel = ApprovalChannel(client=None, token_file=None)

    for board in _harness(tmp_path, channel, plugin):
        answer = board.get(f"/approvals/{JOB}")

        assert "the approval page is not set up on this host" in answer.text
        assert "<button" not in answer.text
        assert plugin.bearers == []


def test_a_token_file_that_is_missing_decides_nothing(tmp_path: Path) -> None:
    plugin = FakePlugin()
    plugin.add()
    channel = _channel(tmp_path, plugin)
    assert channel.token_file is not None
    channel.token_file.unlink()

    for board in _harness(tmp_path, channel, plugin):
        answer = board.get(f"/approvals/{JOB}")

        assert "the approval token file cannot be read" in answer.text
        assert plugin.bearers == []


def test_the_job_form_matches_the_plugin() -> None:
    assert is_job(JOB)
    assert is_job("0" * 26)
    assert not is_job("i" * 26)
    assert not is_job("8" + "0" * 25)


def test_the_environment_names_the_channel(tmp_path: Path) -> None:
    token = tmp_path / "approval.token"
    token.write_text(BEARER, encoding="utf-8")
    config = from_env(
        {
            "NOTICEBOARD_BIND": "127.0.0.1",
            "NOTICEBOARD_APPROVAL_URL": "http://192.0.2.10:8310",
            "NOTICEBOARD_APPROVAL_TOKEN_FILE": str(token),
        }
    )

    assert config.approval_url == "http://192.0.2.10:8310"
    assert config.approval_token_file == token

    bare = from_env({"NOTICEBOARD_BIND": "127.0.0.1"})

    assert bare.approval_url == ""
    assert bare.approval_token_file is None


@pytest.mark.parametrize(
    "named",
    [
        {"NOTICEBOARD_APPROVAL_URL": "http://192.0.2.10:8310"},
        {"NOTICEBOARD_APPROVAL_TOKEN_FILE": "/nowhere/approval.token"},
        {
            "NOTICEBOARD_APPROVAL_URL": "http://192.0.2.10:8310",
            "NOTICEBOARD_APPROVAL_TOKEN_FILE": "/nowhere/approval.token",
        },
    ],
)
def test_a_half_set_channel_refuses_to_start(named: dict[str, str]) -> None:
    with pytest.raises(ConfigError, match="APPROVAL"):
        from_env({"NOTICEBOARD_BIND": "127.0.0.1", **named})


def test_an_empty_token_file_refuses_to_start(tmp_path: Path) -> None:
    token = tmp_path / "approval.token"
    token.write_text("\n", encoding="utf-8")
    named = {
        "NOTICEBOARD_BIND": "127.0.0.1",
        "NOTICEBOARD_APPROVAL_URL": "http://192.0.2.10:8310",
        "NOTICEBOARD_APPROVAL_TOKEN_FILE": str(token),
    }

    with pytest.raises(ConfigError, match="unreadable or empty"):
        from_env(named)
