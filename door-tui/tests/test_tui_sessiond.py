"""The door's client of `sessiond` (contract 02 §5), request by request.

The door is a terminal program, so this client is synchronous: it makes a
call, then blocks on a foreground child. Every call it makes is asserted
here against a transport that records the request and answers a scripted
body. Nothing touches a live service and nothing needs the host.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from agent_door_tui.config import TuiConfig
from agent_door_tui.errors import Exit
from agent_door_tui.sessiond import (
    DOOR_INSTANCE_HEADER,
    HttpSessiond,
    Intent,
    SessiondError,
    SessionState,
    Takeover,
    refusal_of,
)
from agent_door_tui.untrusted import as_object

TOKEN = "door-tui-token-" + "t" * 32
INSTANCE = "tui.4242"
SESSION = "tui-01JBQ7WZ0X4T9V6K2H8M3N5PQR"
TURN = "01JBQ7WZ0X4T9V6K2H8M3N5PQR"

_ROW: dict[str, Any] = {
    "family": "chat",
    "session": SESSION,
    "title": "Kitchen sensor debug",
    "state": "idle",
    "updated_at": "2026-09-19T10:04:11.220Z",
    "turns_total": 3,
    "turns_running": 0,
}

Reply = Callable[[httpx.Request], tuple[int, dict[str, Any]]]


class Recorder:
    """Records every request and answers a scripted body."""

    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []
        self.reply: Reply = _default_reply

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        status, payload = self.reply(request)

        return httpx.Response(status, json=payload)

    def body(self, index: int = 0) -> dict[str, object]:
        """The request body this door sent, or an empty object."""
        raw = self.seen[index].content

        if not raw:
            return {}

        parsed: object = json.loads(raw)

        return as_object(parsed)


def _default_reply(request: httpx.Request) -> tuple[int, dict[str, Any]]:
    path = request.url.path

    if path == "/v1/sessions" and request.method == "GET":
        return 200, {"sessions": [_ROW], "next_cursor": None}

    if path.endswith("/writer"):
        return 200, {"holder": "tui", "expires_at": "2026-09-19T10:05:11Z"}

    if path.endswith("/stop"):
        return 200, {"turn": TURN, "state": "aborted"}

    return 200, dict(_ROW)


@pytest.fixture
def served(tmp_path: Path) -> tuple[HttpSessiond, Recorder]:
    recorder = Recorder()
    client = httpx.Client(
        base_url="http://sessiond", transport=httpx.MockTransport(recorder.handle)
    )

    return HttpSessiond(_config(tmp_path), client), recorder


def _config(root: Path) -> TuiConfig:
    return TuiConfig(
        sessiond_token=TOKEN,
        sessiond_url="http://sessiond",
        sessiond_socket=root / "s.sock",
        families_dir=root / "families",
        sbx="sbx",
        pi_launch="/opt/agent-supervisor/agent-pi-launch.js",
        door_instance=INSTANCE,
    )


def test_listing_sends_the_family_and_the_token(served: tuple[HttpSessiond, Recorder]) -> None:
    client, recorder = served

    rows = client.sessions("chat")

    request = recorder.seen[0]
    assert (request.method, request.url.path) == ("GET", "/v1/sessions")
    assert request.url.params["family"] == "chat"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert len(rows) == 1
    assert rows[0].session == SESSION
    assert rows[0].state is SessionState.IDLE
    assert rows[0].turns_total == 3


def test_an_unknown_state_reads_as_unknown(served: tuple[HttpSessiond, Recorder]) -> None:
    """Untrusted input: a state this door does not know never raises."""
    client, recorder = served
    recorder.reply = lambda _request: (200, {"sessions": [{**_ROW, "state": "hyperventilating"}]})

    rows = client.sessions("chat")

    assert rows[0].state is SessionState.UNKNOWN


def test_a_row_that_is_not_an_object_is_dropped(served: tuple[HttpSessiond, Recorder]) -> None:
    client, recorder = served
    recorder.reply = lambda _request: (200, {"sessions": ["nonsense", _ROW]})

    assert len(client.sessions("chat")) == 1


def test_create_names_the_session_and_the_title(served: tuple[HttpSessiond, Recorder]) -> None:
    client, recorder = served

    client.create_session("chat", SESSION, "Debug the boiler")

    body = recorder.body()
    assert recorder.seen[0].url.path == "/v1/sessions"
    assert body["family"] == "chat"
    assert body["session"] == SESSION
    assert body["title"] == "Debug the boiler"
    assert body["labels"] == {"door": "tui"}


def test_the_writer_call_names_this_terminal(served: tuple[HttpSessiond, Recorder]) -> None:
    """Contract 02 §7.1. Two terminals are two writers, so each names itself."""
    client, recorder = served

    client.take_writer("chat", SESSION, INSTANCE)

    request = recorder.seen[0]
    assert request.url.path == f"/v1/sessions/chat/{SESSION}/writer"
    assert request.headers[DOOR_INSTANCE_HEADER] == INSTANCE
    assert recorder.body() == {"holder": "tui", "force": False, "intent": "acquire"}


def test_a_renewal_says_it_is_one(served: tuple[HttpSessiond, Recorder]) -> None:
    """Contract 02 §7.4. A renew may never take a lease this door lost."""
    client, recorder = served

    client.take_writer("chat", SESSION, INSTANCE, Takeover.POLITE, Intent.RENEW)

    assert recorder.body()["intent"] == "renew"


def test_the_two_release_operations_use_their_paths(
    served: tuple[HttpSessiond, Recorder],
) -> None:
    """Contract 02 §5.10 and §5.11: release the lease, release the pi process."""
    client, recorder = served

    client.release_writer("chat", SESSION, INSTANCE)
    client.release_process("chat", SESSION, INSTANCE)

    assert recorder.seen[0].method == "DELETE"
    assert recorder.seen[0].url.path == f"/v1/sessions/chat/{SESSION}/writer"
    assert recorder.seen[1].method == "POST"
    assert recorder.seen[1].url.path == f"/v1/sessions/chat/{SESSION}/release-process"
    assert recorder.seen[1].headers[DOOR_INSTANCE_HEADER] == INSTANCE


def test_a_busy_session_raises_with_its_code(served: tuple[HttpSessiond, Recorder]) -> None:
    client, recorder = served
    recorder.reply = lambda _request: (
        409,
        {
            "error": {
                "code": "session_busy",
                "message": "another door holds the writer lease",
                "detail": {"holder": "owui", "turn": TURN},
            }
        },
    )

    with pytest.raises(SessiondError) as caught:
        client.take_writer("chat", SESSION, INSTANCE)

    assert caught.value.code == "session_busy"
    assert caught.value.detail["holder"] == "owui"


def test_stopping_a_turn_names_a_reason(served: tuple[HttpSessiond, Recorder]) -> None:
    client, recorder = served

    client.stop_turn("chat", SESSION, TURN)

    assert recorder.seen[0].url.path == f"/v1/sessions/chat/{SESSION}/turns/{TURN}/stop"
    assert recorder.body()["reason"]


def test_the_newest_unfinished_turn_is_found(served: tuple[HttpSessiond, Recorder]) -> None:
    """`GET` with `turns` is how the door learns a turn is still in flight."""
    client, recorder = served
    recorder.reply = lambda _request: (
        200,
        {
            **_ROW,
            "turns": [
                {"turn": "01JBQ7WZ0X4T9V6K2H8M3N5PQ0", "state": "settled"},
                {"turn": TURN, "state": "running"},
            ],
        },
    )

    detail = client.get_session("chat", SESSION)

    assert detail.unfinished_turn == TURN
    assert recorder.seen[0].url.params["turns"]


def test_every_turn_settled_leaves_no_unfinished_one(
    served: tuple[HttpSessiond, Recorder],
) -> None:
    client, recorder = served
    recorder.reply = lambda _request: (200, {**_ROW, "turns": [{"turn": TURN, "state": "settled"}]})

    assert client.get_session("chat", SESSION).unfinished_turn is None


def test_an_answer_with_no_error_body_still_fails(served: tuple[HttpSessiond, Recorder]) -> None:
    """Untrusted input: a malformed answer is a refusal, never a crash."""
    client, recorder = served
    recorder.reply = lambda _request: (500, {"not": "an error body"})

    with pytest.raises(SessiondError) as caught:
        client.sessions("chat")

    assert caught.value.code == "internal"


def test_a_transport_failure_reads_as_unreachable(tmp_path: Path) -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no such socket", request=request)

    client = HttpSessiond(
        _config(tmp_path),
        httpx.Client(base_url="http://sessiond", transport=httpx.MockTransport(broken)),
    )

    with pytest.raises(SessiondError) as caught:
        client.sessions("chat")

    assert caught.value.code == "unreachable"


def test_refusal_maps_a_busy_lease_to_its_own_exit() -> None:
    busy = SessiondError("session_busy", "held", 409, detail={"holder": "owui"})

    refusal = refusal_of(busy)

    assert refusal.code is Exit.SESSION_BUSY
    assert "owui" in refusal.message


def test_refusal_maps_everything_else_to_the_sessiond_exit() -> None:
    refusal = refusal_of(SessiondError("family_degraded", "a fault blocks turns", 503))

    assert refusal.code is Exit.SESSIOND
    assert "family_degraded" in refusal.message


def test_no_refusal_message_carries_the_token() -> None:
    message = refusal_of(SessiondError("unauthorized", "missing or unknown token", 401)).message

    assert TOKEN not in message
