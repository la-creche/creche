"""The door's two routes: `GET /healthz` and `WS /acp/<family>`.

Each refusal comes before the upgrade, as an HTTP status. A connection that
passes every check is accepted, then closed with the "not wired" code.
"""

from __future__ import annotations

import pytest
from agent_door_acp.app import FORWARD_AUTH_HEADER, NOT_WIRED_CODE, NOT_WIRED_REASON, create_app
from agent_door_acp.config import DoorConfig
from starlette.testclient import TestClient, WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

KEY = "k" * 32
GOOD = {"Authorization": f"Bearer {KEY}", FORWARD_AUTH_HEADER: "operator"}


@pytest.fixture
def client() -> TestClient:
    config = DoorConfig(bind_host="127.0.0.1", bind_port=8390, door_key=KEY.encode("ascii"))
    return TestClient(create_app(config))


def _refused(client: TestClient, path: str, headers: dict[str, str]) -> int:
    with (
        pytest.raises(WebSocketDenialResponse) as refused,
        client.websocket_connect(path, headers=headers),
    ):
        pass

    return refused.value.status_code


def test_healthz_answers_ok(client: TestClient) -> None:
    answer = client.get("/healthz")

    assert answer.status_code == 200
    assert answer.json() == {"status": "ok"}


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {FORWARD_AUTH_HEADER: "operator"},
        {"Authorization": f"Bearer {'x' * 32}", FORWARD_AUTH_HEADER: "operator"},
        {"Authorization": KEY, FORWARD_AUTH_HEADER: "operator"},
        {"Authorization": f"Bearer {KEY}x", FORWARD_AUTH_HEADER: "operator"},
        {"Authorization": f"Bearer {KEY}"},
        {"Authorization": f"Bearer {KEY}", FORWARD_AUTH_HEADER: " "},
    ],
    ids=["none", "no-bearer", "wrong", "no-scheme", "longer", "no-forward-auth", "blank-user"],
)
def test_a_bad_auth_is_refused(client: TestClient, headers: dict[str, str]) -> None:
    assert _refused(client, "/acp/chat", headers) == 401


@pytest.mark.parametrize(
    "path",
    [
        "/acp/",
        "/acp/c",
        "/acp/Chat",
        "/acp/1chat",
        "/acp/chat_x",
        "/acp/chat/x",
        "/acp/chat%0A",
        "/acp/" + "c" * 32,
    ],
)
def test_a_bad_family_is_refused_before_the_upgrade(client: TestClient, path: str) -> None:
    """`%0A` is the case for the path itself: Starlette's route regex ends in
    `$`, so its path parameter drops a final newline."""
    assert _refused(client, path, GOOD) == 404


@pytest.mark.parametrize("family", ["ch", "chat", "code-sandbox", "c" * 31])
def test_a_good_connection_closes_as_not_wired(client: TestClient, family: str) -> None:
    with (
        client.websocket_connect(f"/acp/{family}", headers=GOOD) as socket,
        pytest.raises(WebSocketDisconnect) as closed,
    ):
        socket.receive_text()

    assert (closed.value.code, closed.value.reason) == (NOT_WIRED_CODE, NOT_WIRED_REASON)
