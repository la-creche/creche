"""The door end to end: one HTTP request in, the right attendance call and
the right OpenAI-shaped answer out. `FakeAttendance` plays attendance's part;
`StatusFiles` is the real picker, pointed at a `tmp_path`."""

from __future__ import annotations

import json
import time
from functools import partial
from pathlib import Path
from typing import Any

import httpx
import pytest
from agent_door_owui import app as door_app
from agent_door_owui.app import create_app
from agent_door_owui.attendance import (
    AttendanceError,
    HttpAttendance,
    SettledTurn,
    StreamBroken,
)
from agent_door_owui.config import DoorConfig
from agent_door_owui.families import StatusFiles
from agent_door_owui.headers import (
    CHAT_ID_HEADER,
    MESSAGE_ID_HEADER,
    PARENT_ID_HEADER,
    TASK_HEADER,
    USER_MESSAGE_ID_HEADER,
)
from agent_door_owui.journal import JournalLine
from agent_door_owui.sse import KEEPALIVE_FRAME
from agent_door_owui.stream import with_keepalive
from fake_attendance import FakeAttendance
from starlette.testclient import TestClient

DOOR_KEY = "d" * 32
CHAT = "3f2a9c41-77b0-4a1e-9a4c-1d0e5f8b2c33"
MESSAGE = "b7c1e2d0-1f44-4c61-8a2b-9e0d3c5f7a11"


def _config(tmp_path: Path) -> DoorConfig:
    return DoorConfig(
        bind_host="127.0.0.1",
        bind_port=8340,
        door_key=DOOR_KEY,
        attendance_token="t" * 32,
        attendance_url="http://sessiond",
        attendance_socket=None,
        families_dir=tmp_path / "families",
    )


def _write_status(tmp_path: Path, family: str, **fields: Any) -> None:
    directory = tmp_path / "families" / family
    directory.mkdir(parents=True, exist_ok=True)
    document: dict[str, Any] = {
        "family": family,
        "kind": "attended",
        "state": "in_sync",
        "written_at": "2026-09-18T19:20:11Z",
    }
    document.update(fields)
    (directory / "status.json").write_text(json.dumps(document), encoding="utf-8")


def _client(tmp_path: Path, fake: FakeAttendance) -> TestClient:
    config = _config(tmp_path)
    app = create_app(config, fake, StatusFiles(config.families_dir))
    return TestClient(app)


def _headers(**overrides: str) -> dict[str, str]:
    base = {
        "Authorization": f"Bearer {DOOR_KEY}",
        CHAT_ID_HEADER: CHAT,
        MESSAGE_ID_HEADER: MESSAGE,
        USER_MESSAGE_ID_HEADER: "a1b2c3d4-5e6f-4071-8293-a4b5c6d7e8f9",
        PARENT_ID_HEADER: "",
        TASK_HEADER: "",
    }
    base.update(overrides)
    return base


def _body(*, stream: bool = False, text: str = "which sensor dropped out?") -> dict[str, Any]:
    return {
        "model": "agent:chat",
        "messages": [{"role": "user", "content": text}],
        "stream": stream,
    }


def _pi_text(delta: str) -> JournalLine:
    return JournalLine(
        kind="pi_event",
        turn="01JBQ7WZ0X4T9V6K2H8M3N5PQR",
        body={
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": delta},
        },
    )


def _turn_settled() -> JournalLine:
    return JournalLine(kind="turn_settled", turn="01JBQ7WZ0X4T9V6K2H8M3N5PQR", body={"usage": {}})


def _turn_failed(reason: str) -> JournalLine:
    return JournalLine(
        kind="turn_failed", turn="01JBQ7WZ0X4T9V6K2H8M3N5PQR", body={"reason": reason}
    )


def _success_lines() -> list[JournalLine]:
    return [_pi_text("Sensor kitchen_temp stopped reporting."), _turn_settled()]


def test_the_picker_lists_attended_families(tmp_path: Path) -> None:
    _write_status(tmp_path, "chat")
    _write_status(tmp_path, "vault-oracle", kind="thin")
    client = _client(tmp_path, FakeAttendance())

    response = client.get("/v1/models", headers={"Authorization": f"Bearer {DOOR_KEY}"})

    assert response.status_code == 200
    assert [entry["id"] for entry in response.json()["data"]] == ["agent:chat"]


def test_models_needs_the_doors_own_key(tmp_path: Path) -> None:
    _write_status(tmp_path, "chat")
    client = _client(tmp_path, FakeAttendance())

    response = client.get("/v1/models")

    assert response.status_code == 401


def test_a_non_streamed_turn_answers_with_the_text(tmp_path: Path) -> None:
    fake = FakeAttendance()
    fake.settled = SettledTurn(
        turn="01T",
        state="settled",
        text="Sensor kitchen_temp stopped reporting.",
        usage={},
        reason="",
    )
    client = _client(tmp_path, fake)

    response = client.post("/v1/chat/completions", headers=_headers(), json=_body())

    assert response.status_code == 200
    body = response.json()
    assert body["choices"][0]["message"]["content"] == "Sensor kitchen_temp stopped reporting."
    assert body["object"] == "chat.completion"
    assert fake.ensured == [("chat", f"owui-{CHAT}")]
    assert fake.requests[0].idempotency_key == MESSAGE


def test_a_streamed_turn_carries_the_answer_as_sse(tmp_path: Path) -> None:
    fake = FakeAttendance(lines=_success_lines())
    client = _client(tmp_path, fake)

    with client.stream(
        "POST", "/v1/chat/completions", headers=_headers(), json=_body(stream=True)
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["x-accel-buffering"] == "no"
        out = "".join(response.iter_text())

    assert "Sensor kitchen_temp stopped reporting." in out
    assert '"status":"done"' in out
    assert out.endswith("data: [DONE]\n\n")


def test_an_empty_chat_id_is_a_hard_refusal(tmp_path: Path) -> None:
    fake = FakeAttendance()
    client = _client(tmp_path, fake)

    response = client.post(
        "/v1/chat/completions", headers=_headers(**{CHAT_ID_HEADER: ""}), json=_body()
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "missing_chat_id"
    assert fake.ensured == []


def test_a_crafted_chat_id_is_refused(tmp_path: Path) -> None:
    fake = FakeAttendance()
    client = _client(tmp_path, fake)

    response = client.post(
        "/v1/chat/completions",
        headers=_headers(**{CHAT_ID_HEADER: "../../etc/passwd"}),
        json=_body(),
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_id"
    assert fake.ensured == []


def test_a_repeat_message_id_returns_the_existing_turn(tmp_path: Path) -> None:
    # Contract 02 §6: a second `run turn` with the same key on the same
    # session returns the existing turn rather than running it again. The
    # door itself does not special-case this — the point of the test is
    # that it does not get in the way of attendance's own idempotency.
    fake = FakeAttendance()
    client = _client(tmp_path, fake)

    first = client.post("/v1/chat/completions", headers=_headers(), json=_body())
    second = client.post("/v1/chat/completions", headers=_headers(), json=_body())

    assert first.status_code == second.status_code == 200
    assert first.json()["choices"] == second.json()["choices"]
    assert len(fake.requests) == 2  # the door called through both times...
    assert (
        fake.requests[0].idempotency_key == fake.requests[1].idempotency_key
    )  # ...attendance's job


def test_a_repeat_with_a_different_prompt_is_refused(tmp_path: Path) -> None:
    fake = FakeAttendance()
    client = _client(tmp_path, fake)

    client.post("/v1/chat/completions", headers=_headers(), json=_body(text="first question"))
    second = client.post(
        "/v1/chat/completions", headers=_headers(), json=_body(text="a different question")
    )

    assert second.status_code == 400
    assert second.json()["error"]["code"] == "idempotency_mismatch"


def test_a_second_writer_gets_409(tmp_path: Path) -> None:
    fake = FakeAttendance()
    fake.turn_error = AttendanceError("session_busy", "another door holds the writer lease", 409)
    client = _client(tmp_path, fake)

    response = client.post("/v1/chat/completions", headers=_headers(), json=_body())

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "session_busy"


def test_a_second_writer_gets_409_even_when_streaming(tmp_path: Path) -> None:
    # This is the case that motivates priming the relay before returning a
    # StreamingResponse: Starlette locks in the status code the instant
    # the response object is constructed.
    fake = FakeAttendance()
    fake.turn_error = AttendanceError("session_busy", "another door holds the writer lease", 409)
    client = _client(tmp_path, fake)

    response = client.post("/v1/chat/completions", headers=_headers(), json=_body(stream=True))

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "session_busy"


def test_a_failed_non_streamed_turn_is_a_visible_error(tmp_path: Path) -> None:
    fake = FakeAttendance()
    fake.settled = SettledTurn(turn="01T", state="failed", text="", usage={}, reason="model_error")
    client = _client(tmp_path, fake)

    response = client.post("/v1/chat/completions", headers=_headers(), json=_body())

    assert response.status_code >= 400
    assert response.json()["error"]["code"] == "model_error"


def test_a_failed_streamed_turn_is_a_visible_error(tmp_path: Path) -> None:
    fake = FakeAttendance(lines=[_turn_failed("budget_exceeded")])
    client = _client(tmp_path, fake)

    with client.stream(
        "POST", "/v1/chat/completions", headers=_headers(), json=_body(stream=True)
    ) as response:
        assert response.status_code == 200
        out = "".join(response.iter_text())

    assert "budget_exceeded" in out
    assert '"status":"failed"' in out


def test_a_connection_dropped_mid_stream_is_a_visible_error(tmp_path: Path) -> None:
    # Not a turn failure attendance reported — the door's OWN read of the
    # stream broke (attendance.py turns a dropped httpx connection into
    # StreamBroken). A failure is never silent, whoever's it is: the reader
    # must still see a visible error, never a stream that quietly cuts off.
    fake = FakeAttendance(
        lines=[_pi_text("partial answer")],
        stream_error=StreamBroken("connection reset"),
    )
    client = _client(tmp_path, fake)

    with client.stream(
        "POST", "/v1/chat/completions", headers=_headers(), json=_body(stream=True)
    ) as response:
        assert response.status_code == 200
        out = "".join(response.iter_text())

    assert "partial answer" in out
    assert '"status":"failed"' in out
    assert out.endswith("data: [DONE]\n\n")


def test_client_disconnect_leaves_the_turn_running(tmp_path: Path) -> None:
    # A turn that never settles on its own: the only way this stream ends
    # is the reader going away. AttendanceClient has no "stop" or "abort"
    # method at all, so nothing the door does here could end the turn even
    # if it wanted to — the only observable effect is the fake's own
    # stream context manager closing.
    fake = FakeAttendance(lines=[_pi_text("partial answer, then nothing else ever arrives")])
    client = _client(tmp_path, fake)

    with client.stream(
        "POST", "/v1/chat/completions", headers=_headers(), json=_body(stream=True)
    ) as response:
        assert response.status_code == 200
        chunks = response.iter_text()
        first = next(chunks)
        assert "chat.completion.chunk" in first

    for _ in range(100):
        if fake.stream_closed:
            break
        time.sleep(0.02)

    assert fake.stream_closed


def test_a_background_task_request_never_starts_a_turn(tmp_path: Path) -> None:
    fake = FakeAttendance()
    client = _client(tmp_path, fake)

    response = client.post(
        "/v1/chat/completions",
        headers=_headers(**{TASK_HEADER: "title_generation"}),
        json=_body(),
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "background_task_not_supported"
    assert fake.ensured == []
    assert fake.requests == []


def test_chat_completions_needs_the_doors_own_key(tmp_path: Path) -> None:
    fake = FakeAttendance()
    client = _client(tmp_path, fake)
    headers = _headers()
    del headers["Authorization"]

    response = client.post("/v1/chat/completions", headers=headers, json=_body())

    assert response.status_code == 401
    assert fake.ensured == []


def test_a_wrong_key_is_refused(tmp_path: Path) -> None:
    fake = FakeAttendance()
    client = _client(tmp_path, fake)

    response = client.post(
        "/v1/chat/completions", headers=_headers(Authorization="Bearer wrong-key"), json=_body()
    )

    assert response.status_code == 401


def test_the_branch_fallback_retries_without_the_parent(tmp_path: Path) -> None:
    # Contract 02 §10.2: attendance answering
    # not_implemented for a request that carried parent_id falls back to a
    # plain turn, once, rather than failing the chat.
    fake = FakeAttendance()
    fake.turn_error = AttendanceError("not_implemented", "branching is not built yet", 501)
    client = _client(tmp_path, fake)

    response = client.post(
        "/v1/chat/completions",
        headers=_headers(**{PARENT_ID_HEADER: "9f8e7d6c-5b4a-4938-8271-6a5b4c3d2e1f"}),
        json=_body(),
    )

    assert response.status_code == 200
    # Two calls: the first attempt (with_parent=True) gets not_implemented,
    # and the retry (with_parent=False) is what actually answers.
    assert len(fake.requests) == 2
    assert fake.with_parent_flags == [True, False]
    assert fake.requests[0].parent_id == "9f8e7d6c-5b4a-4938-8271-6a5b4c3d2e1f"


# --- attendance does not answer ---


def _door_over(tmp_path: Path, handler: Any) -> TestClient:
    """The door with the real attendance client over a transport a test writes."""
    config = _config(tmp_path)
    transport = httpx.MockTransport(handler)
    upstream = httpx.AsyncClient(base_url=config.attendance_url, transport=transport)
    app = create_app(config, HttpAttendance(config, upstream), StatusFiles(config.families_dir))

    return TestClient(app)


def _no_answer(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("no answer", request=request)


def _no_answer_to_a_turn(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/v1/sessions":
        return httpx.Response(201, json={})

    raise httpx.ConnectError("no answer", request=request)


@pytest.mark.parametrize("handler", [_no_answer, _no_answer_to_a_turn])
@pytest.mark.parametrize("stream", [False, True])
def test_no_answer_from_attendance_is_a_502_in_the_error_shape(
    tmp_path: Path, handler: Any, stream: bool
) -> None:
    client = _door_over(tmp_path, handler)

    response = client.post("/v1/chat/completions", headers=_headers(), json=_body(stream=stream))

    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == "attendance_unreachable"
    assert error["type"] == "server_error"
    assert "cannot reach attendance" in error["message"]


# --- a refusal after the first frame ---

#: The relay sends a keepalive frame after this silence, in a test.
_SHORT_IDLE_S = 0.01
#: A stream that opens after the first keepalive frame of such a relay.
_LATE_OPEN_S = 0.2


def _with_a_short_keepalive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(door_app, "with_keepalive", partial(with_keepalive, idle_s=_SHORT_IDLE_S))


def test_a_refusal_after_the_first_frame_is_a_visible_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The status is sent with the first keepalive frame. A refusal that comes
    # later cannot be a status, so it must be a frame that the reader sees.
    _with_a_short_keepalive(monkeypatch)
    fake = FakeAttendance(open_delay_s=_LATE_OPEN_S)
    fake.turn_error = AttendanceError("sandbox_unavailable", "family chat has no sandbox", 503)
    client = _client(tmp_path, fake)

    with client.stream(
        "POST", "/v1/chat/completions", headers=_headers(), json=_body(stream=True)
    ) as response:
        assert response.status_code == 200
        out = "".join(response.iter_text())

    assert out.startswith(KEEPALIVE_FRAME)
    assert '"code":"sandbox_unavailable"' in out
    assert "no sandbox to run the turn on" in out
    assert '"status":"failed"' in out
    assert out.endswith("data: [DONE]\n\n")
    assert out.count("data: [DONE]") == 1


def test_a_late_refusal_of_the_branch_retry_is_a_visible_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_a_short_keepalive(monkeypatch)

    class _RefusesTwice(FakeAttendance):
        """Answers `not_implemented` to the branch, then refuses the retry."""

        def _take_error(self) -> AttendanceError:
            error = super()._take_error()
            if error.code == "not_implemented":
                self.turn_error = AttendanceError("session_busy", "another door writes", 409)

            return error

    fake = _RefusesTwice(open_delay_s=_LATE_OPEN_S)
    fake.turn_error = AttendanceError("not_implemented", "branching is not built yet", 501)
    client = _client(tmp_path, fake)

    with client.stream(
        "POST",
        "/v1/chat/completions",
        headers=_headers(**{PARENT_ID_HEADER: "9f8e7d6c-5b4a-4938-8271-6a5b4c3d2e1f"}),
        json=_body(stream=True),
    ) as response:
        assert response.status_code == 200
        out = "".join(response.iter_text())

    assert fake.with_parent_flags == [True, False]
    assert '"code":"session_busy"' in out
    assert '"code":"not_implemented"' not in out
    assert out.endswith("data: [DONE]\n\n")


def test_a_refusal_before_the_first_frame_is_still_a_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The relay of this test is the relay of the two tests above. With no
    # wait before the refusal, the door still answers a status and no stream.
    _with_a_short_keepalive(monkeypatch)
    fake = FakeAttendance()
    fake.turn_error = AttendanceError("sandbox_unavailable", "family chat has no sandbox", 503)
    client = _client(tmp_path, fake)

    response = client.post("/v1/chat/completions", headers=_headers(), json=_body(stream=True))

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "sandbox_unavailable"


# --- a request body that nests too deep ---


def test_a_body_that_nests_too_deep_is_a_400_in_the_error_shape(tmp_path: Path) -> None:
    # More levels than the JSON parser of each supported Python reads.
    levels = 400_000
    client = _client(tmp_path, FakeAttendance())
    headers = {**_headers(), "content-type": "application/json"}

    response = client.post(
        "/v1/chat/completions", headers=headers, content="[" * levels + "]" * levels
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_body"


# --- a model with no UTF-8 form ---


def test_a_model_with_no_utf8_form_is_a_400_in_the_error_shape(tmp_path: Path) -> None:
    # A JSON escape names one half of a surrogate pair.
    body = '{"model": "agent:\\ud800", "messages": [{"role": "user", "content": "go"}]}'
    client = _client(tmp_path, FakeAttendance())
    headers = {**_headers(), "content-type": "application/json"}

    response = client.post("/v1/chat/completions", headers=headers, content=body)

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "bad_model"
    assert error["type"] == "invalid_request_error"


# --- a failure that no handler names ---


class _BrokenAttendance(FakeAttendance):
    """Raises an error that the door has no handler for."""

    async def ensure_session(self, family: str, session: str) -> None:
        raise RuntimeError("a defect of the door")


@pytest.mark.parametrize("stream", [False, True])
def test_an_unexpected_failure_is_a_500_in_the_error_shape(tmp_path: Path, stream: bool) -> None:
    config = _config(tmp_path)
    app = create_app(config, _BrokenAttendance(), StatusFiles(config.families_dir))
    # The server raises the error again after the answer, so that its log
    # holds the traceback. The test reads the answer.
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post("/v1/chat/completions", headers=_headers(), json=_body(stream=stream))

    assert response.status_code == 500
    error = response.json()["error"]
    assert error["code"] == "internal"
    assert error["type"] == "server_error"
    assert "a defect of the door" not in error["message"]
