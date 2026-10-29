"""The door's two routes: `GET /healthz` and `WS /acp/<family>`.

Each refusal comes before the upgrade, as an HTTP status. A connection that
passes every check is accepted. Here no attendance listens, so the door
closes it with the fixed code before any frame.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from agent_door_acp.app import FORWARD_AUTH_HEADER, create_app
from agent_door_acp.config import DoorConfig
from agent_door_acp.forward import UNREACHABLE_REASON, Close
from starlette.testclient import TestClient, WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

KEY = "k" * 32
GOOD = {"Authorization": f"Bearer {KEY}", FORWARD_AUTH_HEADER: "operator"}


def _config(tmp_path: Path, *, require_authentik: bool = True) -> DoorConfig:
    return DoorConfig(
        bind_host="127.0.0.1",
        bind_port=8390,
        door_key=KEY.encode("ascii"),
        attendance_socket=tmp_path / "absent.sock",
        attendance_token=b"t" * 32,
        require_authentik=require_authentik,
    )


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(_config(tmp_path)))


def _closed(client: TestClient, path: str, headers: dict[str, str]) -> tuple[int, str]:
    with (
        client.websocket_connect(path, headers=headers) as socket,
        pytest.raises(WebSocketDisconnect) as closed,
    ):
        socket.receive_text()

    return closed.value.code, closed.value.reason


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


@pytest.mark.parametrize("family", ["ch", "chat", "coding-sandbox", "c" * 31])
def test_an_unreachable_attendance_closes_with_the_fixed_code(
    client: TestClient, family: str
) -> None:
    assert _closed(client, f"/acp/{family}", GOOD) == (Close.UNREACHABLE, UNREACHABLE_REASON)


def test_a_terminal_client_needs_no_authentik_header_when_not_required(tmp_path: Path) -> None:
    client = TestClient(create_app(_config(tmp_path, require_authentik=False)))
    bearer_only = {"Authorization": f"Bearer {KEY}"}

    assert _refused(client, "/acp/chat", {}) == 401
    assert _closed(client, "/acp/chat", bearer_only)[0] == Close.UNREACHABLE
