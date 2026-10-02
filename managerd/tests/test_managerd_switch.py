"""The one call `managerd` makes to `sessiond` (contract 05 §5).

Everything else the two services share travels through the status
document, so this is the whole client surface."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from agent_family import SwitchMode
from agent_managerd.switch import (
    SWITCH_PATH,
    FakeSwitchClient,
    HttpSwitchClient,
    SwitchError,
    SwitchRequest,
    read_token,
)

TOKEN = "a" * 43


def a_request(mode: SwitchMode = SwitchMode.DRAIN) -> SwitchRequest:
    return SwitchRequest(
        family="chat",
        outgoing="chat-s3",
        to="chat-s4",
        mode=mode,
        reason="mounts changed: added /srv/agents/work rw",
    )


def client_answering(handler: Any) -> HttpSwitchClient:
    transport = httpx.MockTransport(handler)
    return HttpSwitchClient("http://sessiond.test", TOKEN, client=httpx.Client(transport=transport))


# --- the request ------------------------------------------------------------


def test_it_posts_the_contract_fields() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"switched": True, "outcome": "drained"})

    client_answering(handler).switch(a_request())

    assert seen["url"] == f"http://sessiond.test{SWITCH_PATH}"
    assert seen["body"]["family"] == "chat"
    assert seen["body"]["from"] == "chat-s3"
    assert seen["body"]["to"] == "chat-s4"
    assert seen["body"]["mode"] == "drain"
    assert seen["body"]["deadline_s"] == 300


def test_a_first_create_sends_a_null_from() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"switched": True, "outcome": "drained"})

    request = SwitchRequest(
        family="chat", outgoing=None, to="chat-s1", mode=SwitchMode.DRAIN, reason="first sandbox"
    )
    client_answering(handler).switch(request)

    assert seen["body"]["from"] is None


def test_the_token_never_rides_in_the_url() -> None:
    """Invariant 13. A bearer header is the one channel that keeps a
    secret out of the URL, out of argv and out of a log line."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"switched": True, "outcome": "drained"})

    client_answering(handler).switch(a_request())

    assert TOKEN not in seen["url"]
    assert seen["auth"] == f"Bearer {TOKEN}"


# --- the answer ---------------------------------------------------------------


def test_it_reads_the_counts_back() -> None:
    body = {
        "switched": True,
        "outcome": "drained",
        "turns_running_at_start": 2,
        "turns_finished": 2,
        "turns_aborted": 0,
        "sessions": 11,
    }
    result = client_answering(lambda _: httpx.Response(200, json=body)).switch(a_request())
    assert result.switched is True
    assert result.outcome == "drained"
    assert result.turns_finished == 2
    assert result.sessions == 11


def test_a_missing_count_reads_zero() -> None:
    """A field the answer leaves out is not a reason to fail a switch that
    said `switched: true`."""
    body = {"switched": True, "outcome": "interrupted"}
    result = client_answering(lambda _: httpx.Response(200, json=body)).switch(a_request())
    assert result.turns_aborted == 0


def test_a_not_implemented_answer_raises() -> None:
    """A `sessiond` without the handler answers 501 here. `managerd` must see a
    refusal, not a silent success that would destroy a live sandbox."""
    body = {"error": "not_implemented"}
    client = client_answering(lambda _: httpx.Response(501, json=body))
    with pytest.raises(SwitchError):
        client.switch(a_request())


def test_a_bad_request_answer_raises() -> None:
    client = client_answering(lambda _: httpx.Response(400, json={"error": "bad_request"}))
    with pytest.raises(SwitchError):
        client.switch(a_request())


def test_a_transport_failure_raises_switch_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to sessiond")

    with pytest.raises(SwitchError):
        client_answering(handler).switch(a_request())


def test_an_answer_that_did_not_switch_raises() -> None:
    """`switched: false` means new turns still go to the old sandbox.
    Destroying it then would end every session in the family."""
    body = {"switched": False, "outcome": "deadline_hit"}
    client = client_answering(lambda _: httpx.Response(200, json=body))
    with pytest.raises(SwitchError):
        client.switch(a_request())


# --- the token file -------------------------------------------------------------


def test_it_reads_the_token_from_a_file(tmp_path: Path) -> None:
    path = tmp_path / "managerd.token"
    path.write_text(f"{TOKEN}\n", encoding="utf-8")
    assert read_token(path) == TOKEN


def test_a_missing_token_file_raises(tmp_path: Path) -> None:
    with pytest.raises(SwitchError):
        read_token(tmp_path / "absent.token")


def test_an_empty_token_file_raises(tmp_path: Path) -> None:
    """An empty key once turned a LAN admin surface into an open one. A
    caller that would send `Bearer ` fails before it sends anything."""
    path = tmp_path / "managerd.token"
    path.write_text("\n", encoding="utf-8")
    with pytest.raises(SwitchError):
        read_token(path)


# --- the fake ---------------------------------------------------------------------


def test_the_fake_records_every_request() -> None:
    fake = FakeSwitchClient()
    fake.switch(a_request())
    fake.switch(a_request(SwitchMode.INTERRUPT))
    assert [one.mode for one in fake.requests] == [SwitchMode.DRAIN, SwitchMode.INTERRUPT]


def test_the_fake_can_refuse() -> None:
    fake = FakeSwitchClient(refuse="switch-sandbox is not implemented")
    with pytest.raises(SwitchError):
        fake.switch(a_request())
