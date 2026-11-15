"""The release page, end to end through the app.

The approvals Node-RED is a fake behind `httpx.MockTransport`: it holds
open release cards, checks the bearer and answers `release-pending` and
`release-resend` with the statuses of the flow. No socket, no Node-RED.
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
from noticeboard.config import DEFAULT_PAGE_SIZE, Config, ConfigError, from_env
from noticeboard.releases import KIND_PENDING, KIND_RESEND, ReleaseChannel, is_release
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
HOOK = "http://hook.example.test/hook/approval"
BEARER = "p" * 40
RELEASE = "01jbxyzq7k8m9n0p1q2r3s4t5v"
TITLE = "Release Approval"
MESSAGE = "Components: caregiver 0.1.90 -> 0.1.94\nUnsafe: none"


class FakeFlow:
    """The two page kinds of the hook, with the statuses of the flow."""

    def __init__(self) -> None:
        self.cards: dict[str, dict[str, Any]] = {}
        self.bodies: list[dict[str, Any]] = []
        self.bearers: list[str] = []
        self.pushes: list[str] = []
        self.urls: list[str] = []
        self.too_soon = False
        self.down = False

    def add(self, release: str = RELEASE, decision: str = "pending", **extra: Any) -> None:
        self.cards[release] = {
            "family": release,
            "title": TITLE,
            "message": MESSAGE,
            "decision": decision,
            "expires_in_s": 421,
            **extra,
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("refused", request=request)

        self.urls.append(str(request.url))
        self.bearers.append(request.headers.get("authorization", ""))

        if request.headers.get("authorization") != f"Bearer {BEARER}":
            return httpx.Response(401, json={"error": "unauthorized"})

        body = json.loads(request.content)
        self.bodies.append(body)
        card = self.cards.get(body.get("family", ""))

        if body.get("kind") == KIND_PENDING:
            if card is None:
                return httpx.Response(404, json={"error": "no open release"})

            return httpx.Response(200, json=card)

        if body.get("kind") == KIND_RESEND:
            return self._resend(card)

        return httpx.Response(400, json={"error": "unknown kind"})

    def _resend(self, card: dict[str, Any] | None) -> httpx.Response:
        if card is None:
            return httpx.Response(404, json={"error": "no open release"})

        if card["decision"] != "pending":
            return httpx.Response(409, json={"error": "decided"})

        if self.too_soon:
            return httpx.Response(429, json={"error": "sent a moment ago"})

        self.pushes.append(card["family"])

        return httpx.Response(200, json={"status": "sent", "kind": KIND_RESEND})


class Harness:
    def __init__(self, client: TestClient, flow: FakeFlow) -> None:
        self.client = client
        self.flow = flow

    def get(self, path: str):
        return self.client.get(path, headers={ACCESS_HEADER: KEY})

    def post(self, path: str, *, token: str | None = None):
        csrf = token if token is not None else (self.client.cookies.get(CSRF_COOKIE) or "")
        headers = {
            ACCESS_HEADER: KEY,
            "Origin": f"http://{HOST}",
            "Host": HOST,
            "Content-Type": "application/x-www-form-urlencoded",
        }

        return self.client.post(
            path, data={CSRF_FIELD: csrf}, headers=headers, follow_redirects=False
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


def _harness(tmp_path: Path, channel: ReleaseChannel, flow: FakeFlow) -> Iterator[Harness]:
    config = _config(tmp_path)
    reader = SessionReader(transport=FakeAttendance(), token_file=write_token(config.state_root))
    app = build_app(config, reader, lambda: NOW, release_cards=channel)

    with TestClient(app, base_url=f"http://{HOST}") as client:
        yield Harness(client, flow)


def _channel(tmp_path: Path, flow: FakeFlow, bearer: str = BEARER) -> ReleaseChannel:
    token = tmp_path / "release.token"
    token.write_text(f"{bearer}\n", encoding="utf-8")
    client = httpx.Client(transport=httpx.MockTransport(flow.handle))

    return ReleaseChannel(client=client, url=HOOK, token_file=token)


@pytest.fixture
def board(tmp_path: Path) -> Iterator[Harness]:
    flow = FakeFlow()
    yield from _harness(tmp_path, _channel(tmp_path, flow), flow)


def test_an_open_card_shows_the_release_and_one_button(board: Harness) -> None:
    board.flow.add()

    answer = board.get(f"/releases/{RELEASE}")

    assert answer.status_code == 200
    assert "<h1>Release Approval</h1>" in answer.text
    assert "Components: caregiver 0.1.90 -&gt; 0.1.94\nUnsafe: none" in answer.text
    assert "Send the card again" in answer.text
    assert "times out in 7 min 1 s" in answer.text
    assert board.flow.bodies == [{"kind": KIND_PENDING, "family": RELEASE}]
    assert board.flow.urls == [HOOK]
    assert board.flow.bearers == [f"Bearer {BEARER}"]
    assert BEARER not in answer.text


def test_the_page_holds_no_approve_and_no_deny(board: Harness) -> None:
    board.flow.add()

    answer = board.get(f"/releases/{RELEASE}")

    assert 'name="decision"' not in answer.text
    assert "cannot decide a release" in answer.text


def test_a_gone_release_is_a_404_page_with_no_button(board: Harness) -> None:
    answer = board.get(f"/releases/{RELEASE}")

    assert answer.status_code == 404
    assert "This release is gone" in answer.text
    assert "<button" not in answer.text


def test_text_that_is_not_a_release_id_never_reaches_the_flow(board: Harness) -> None:
    for bad in ("-" * 26, "x" * 26, "8" + "0" * 25, RELEASE.upper(), RELEASE + "0"):
        assert board.get(f"/releases/{bad}").status_code == 404
        assert board.post(f"/releases/{bad}").status_code == 404

    assert board.flow.bearers == []


def test_a_get_never_pushes_a_card(board: Harness) -> None:
    board.flow.add()

    board.get(f"/releases/{RELEASE}")
    board.get(f"/releases/{RELEASE}")

    assert board.flow.pushes == []


def test_send_again_pushes_the_card_and_says_long_press(board: Harness) -> None:
    board.flow.add()
    board.get(f"/releases/{RELEASE}")

    answer = board.post(f"/releases/{RELEASE}")

    assert answer.status_code == 200
    assert "The card is on your phone again. Long-press it" in answer.text
    assert board.flow.pushes == [RELEASE]
    assert board.flow.bodies[1] == {"kind": KIND_RESEND, "family": RELEASE}


def test_a_post_without_the_csrf_token_pushes_nothing(board: Harness) -> None:
    board.flow.add()
    board.get(f"/releases/{RELEASE}")

    answer = board.post(f"/releases/{RELEASE}", token="forged")

    assert answer.status_code == 403
    assert board.flow.pushes == []


def test_a_tapped_card_shows_the_tap_and_no_button(board: Harness) -> None:
    board.flow.add(decision="approve")

    answer = board.get(f"/releases/{RELEASE}")

    assert answer.status_code == 200
    assert "Approved on the phone." in answer.text
    assert "<button" not in answer.text


def test_send_again_after_a_tap_says_decided(board: Harness) -> None:
    board.flow.add()
    board.get(f"/releases/{RELEASE}")
    # The long press lands between the page's read and its post.
    board.flow.cards[RELEASE]["decision"] = "deny"

    answer = board.post(f"/releases/{RELEASE}")

    assert "A tap answered this release first." in answer.text
    assert "Denied on the phone." in answer.text
    assert board.flow.pushes == []


def test_send_again_after_the_timeout_says_gone(board: Harness) -> None:
    board.get(f"/releases/{RELEASE}")

    answer = board.post(f"/releases/{RELEASE}")

    assert answer.status_code == 200
    assert "This release is gone. Its gate timed out" in answer.text
    assert "<button" not in answer.text


def test_send_again_too_soon_says_wait(board: Harness) -> None:
    board.flow.add()
    board.flow.too_soon = True
    board.get(f"/releases/{RELEASE}")

    answer = board.post(f"/releases/{RELEASE}")

    assert "The card went out a moment ago." in answer.text
    assert board.flow.pushes == []


def test_an_answer_for_another_release_is_a_problem_and_no_button(board: Harness) -> None:
    board.flow.add()
    board.flow.cards[RELEASE]["family"] = "0" * 26

    answer = board.get(f"/releases/{RELEASE}")

    assert answer.status_code == 200
    assert "names another release" in answer.text
    assert "<button" not in answer.text


@pytest.mark.parametrize(
    ("field", "value", "problem"),
    [
        ("decision", "allow", "has no known decision"),
        ("title", "", "has no card text"),
        ("message", "  ", "has no card text"),
    ],
)
def test_an_answer_that_is_not_a_card_is_a_problem(
    board: Harness, field: str, value: str, problem: str
) -> None:
    board.flow.add(**{field: value})

    answer = board.get(f"/releases/{RELEASE}")

    assert problem in answer.text
    assert "<button" not in answer.text


def test_long_card_text_is_cut_to_the_caps_of_the_flow(board: Harness) -> None:
    board.flow.add(title="t" * 100, message="m" * 1000)

    answer = board.get(f"/releases/{RELEASE}")

    assert f"<h1>{'t' * 60}</h1>" in answer.text
    assert "m" * 900 in answer.text
    assert "m" * 901 not in answer.text


def test_a_flow_that_does_not_answer_is_a_problem(board: Harness) -> None:
    board.flow.down = True

    answer = board.get(f"/releases/{RELEASE}")

    assert "cannot reach the approvals Node-RED: ConnectError" in answer.text
    assert "<button" not in answer.text


def test_a_wrong_bearer_is_a_problem_that_names_no_bearer(tmp_path: Path) -> None:
    flow = FakeFlow()
    flow.add()
    channel = _channel(tmp_path, flow, bearer="w" * 40)

    for board in _harness(tmp_path, channel, flow):
        answer = board.get(f"/releases/{RELEASE}")

        assert "answered HTTP 401" in answer.text
        assert "w" * 40 not in answer.text


def test_a_channel_that_is_not_set_up_calls_nothing(tmp_path: Path) -> None:
    flow = FakeFlow()
    flow.add()
    channel = ReleaseChannel(client=None, url="", token_file=None)

    for board in _harness(tmp_path, channel, flow):
        answer = board.get(f"/releases/{RELEASE}")

        assert "the release page is not set up on this host" in answer.text
        assert "<button" not in answer.text
        assert flow.bearers == []


def test_a_token_file_that_is_missing_calls_nothing(tmp_path: Path) -> None:
    flow = FakeFlow()
    flow.add()
    channel = _channel(tmp_path, flow)
    assert channel.token_file is not None
    channel.token_file.unlink()

    for board in _harness(tmp_path, channel, flow):
        answer = board.get(f"/releases/{RELEASE}")

        assert "the release token file cannot be read" in answer.text
        assert flow.bearers == []


def test_the_release_form_matches_the_card() -> None:
    assert is_release(RELEASE)
    assert not is_release(RELEASE.upper())
    assert not is_release("i" * 26)


def test_the_environment_names_the_channel(tmp_path: Path) -> None:
    token = tmp_path / "release.token"
    token.write_text(BEARER, encoding="utf-8")
    config = from_env(
        {
            "NOTICEBOARD_BIND": "127.0.0.1",
            "NOTICEBOARD_RELEASE_HOOK_URL": HOOK,
            "NOTICEBOARD_RELEASE_TOKEN_FILE": str(token),
        }
    )

    assert config.release_hook_url == HOOK
    assert config.release_token_file == token

    bare = from_env({"NOTICEBOARD_BIND": "127.0.0.1"})

    assert bare.release_hook_url == ""
    assert bare.release_token_file is None


@pytest.mark.parametrize(
    "named",
    [
        {"NOTICEBOARD_RELEASE_HOOK_URL": HOOK},
        {"NOTICEBOARD_RELEASE_TOKEN_FILE": "/nowhere/release.token"},
        {
            "NOTICEBOARD_RELEASE_HOOK_URL": HOOK,
            "NOTICEBOARD_RELEASE_TOKEN_FILE": "/nowhere/release.token",
        },
    ],
)
def test_a_half_set_channel_refuses_to_start(named: dict[str, str]) -> None:
    with pytest.raises(ConfigError, match="RELEASE"):
        from_env({"NOTICEBOARD_BIND": "127.0.0.1", **named})
