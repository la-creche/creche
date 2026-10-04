"""The HTTP surface: auth, the eleven operations and the three wait modes."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from attendance.api import DOOR_INSTANCE_HEADER, NDJSON, build_app
from attendance.auth import Principal, TokenBook
from attendance.config import Config
from attendance.service import SessionService
from attendance.wire import MAX_PROMPT_BYTES
from attendance_harness import (
    CHAT_SESSION,
    FAMILY,
    SANDBOX,
    FakeFleet,
    FakePlaypen,
    make_config,
    write_status,
    write_tokens,
)

PROMPT = "Which sensor dropped out last night?"
ANSWER = "Sensor kitchen_temp stopped reporting at 02:14."
SESSIONS = "/v1/sessions"
HTTP_OK = 200
HTTP_CREATED = 201
HTTP_ACCEPTED = 202
HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403
HTTP_NOT_FOUND = 404
HTTP_TOO_LARGE = 413
HTTP_INTERNAL = 500


@dataclass(slots=True)
class Rig:
    """The app, the service and the tokens each door uses."""

    config: Config
    service: SessionService
    fleet: FakeFleet
    client: httpx.AsyncClient
    tokens: dict[Principal, str]

    def head(self, principal: Principal, door: str = "door-1") -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.tokens[principal]}",
            DOOR_INSTANCE_HEADER: door,
        }

    def path(self, session: str = CHAT_SESSION) -> str:
        return f"{SESSIONS}/{FAMILY}/{session}"

    async def create(self, session: str = CHAT_SESSION) -> httpx.Response:
        return await self.client.post(
            SESSIONS,
            json={"family": FAMILY, "session": session, "title": "Kitchen debug"},
            headers=self.head(Principal.DOOR_OWUI),
        )

    async def stop(self) -> None:
        await self.client.aclose()
        await self.service.close()
        await self.fleet.stop()


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    config = make_config(tmp_path)
    write_status(config.state_root)
    made = write_tokens(config.state_root)
    book = TokenBook(config.state_root)
    book.load()

    fleet = FakeFleet()
    service = SessionService(config, factory=fleet.factory, cold_start_wait_s=1.0)
    service.start()
    app = build_app(service, book)
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://sessiond")
    built = Rig(config, service, fleet, client, made)

    yield built

    await built.stop()


async def test_a_request_without_a_token_is_unauthorized(rig: Rig) -> None:
    answer = await rig.client.get(SESSIONS)

    assert answer.status_code == HTTP_UNAUTHORIZED
    assert answer.json()["error"]["code"] == "unauthorized"


async def test_the_view_token_cannot_create(rig: Rig) -> None:
    answer = await rig.client.post(
        SESSIONS,
        json={"family": FAMILY, "session": CHAT_SESSION},
        headers=rig.head(Principal.VIEW_RO),
    )

    assert answer.status_code == HTTP_FORBIDDEN


async def test_create_then_find(rig: Rig) -> None:
    first = await rig.create()
    second = await rig.create()

    assert first.status_code == HTTP_CREATED
    assert second.status_code == HTTP_OK
    assert first.json()["title"] == "Kitchen debug"
    assert first.json()["state"] == "idle"


async def test_a_malformed_body_is_bad_request(rig: Rig) -> None:
    answer = await rig.client.post(
        SESSIONS, content=b"{nope", headers=rig.head(Principal.DOOR_OWUI)
    )

    assert answer.status_code == HTTP_BAD_REQUEST
    assert answer.json()["error"]["code"] == "bad_request"


async def test_a_body_nested_too_deep_is_bad_request(rig: Rig) -> None:
    """The JSON reader raises RecursionError on this body, not ValueError."""
    answer = await rig.client.post(
        SESSIONS, content=b"[" * 200_000, headers=rig.head(Principal.DOOR_OWUI)
    )

    assert answer.status_code == HTTP_BAD_REQUEST
    assert answer.json()["error"]["code"] == "bad_request"


async def test_an_unknown_session_is_not_found(rig: Rig) -> None:
    answer = await rig.client.get(rig.path("owui-nope"), headers=rig.head(Principal.DOOR_OWUI))

    assert answer.status_code == HTTP_NOT_FOUND
    assert answer.json()["error"]["code"] == "not_found"


async def test_an_exception_no_route_expects_is_internal(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contract 02 §14. `internal` has the body of each other failure, and the
    body holds no text of the exception."""

    def failing(*_: object) -> None:
        raise RuntimeError("the listing failed")

    monkeypatch.setattr(rig.service, "list_sessions", failing)
    book = TokenBook(rig.config.state_root)
    book.load()
    # The rig's own transport raises an exception of the app into the test.
    # This one answers as a server does.
    transport = httpx.ASGITransport(app=build_app(rig.service, book), raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://sessiond") as client:
        answer = await client.get(SESSIONS, headers=rig.head(Principal.VIEW_RO))

    assert answer.status_code == HTTP_INTERNAL
    assert answer.json() == {
        "error": {
            "code": "internal",
            "message": "an error in attendance stopped this request",
            "family": None,
            "session": None,
            "turn": None,
            "detail": {},
        },
        "retry_after_s": 5,
    }


async def test_an_oversized_prompt_is_refused(rig: Rig) -> None:
    await rig.create()
    answer = await rig.client.post(
        f"{rig.path()}/turns",
        json={"prompt": "x" * (MAX_PROMPT_BYTES + 1), "wait": "accepted"},
        headers=rig.head(Principal.DOOR_OWUI),
    )

    assert answer.status_code == HTTP_TOO_LARGE
    assert answer.json()["error"]["code"] == "payload_too_large"


async def test_a_bad_attachment_name_is_refused(rig: Rig) -> None:
    await rig.create()
    answer = await rig.client.post(
        f"{rig.path()}/turns",
        json={"prompt": PROMPT, "wait": "accepted", "attachments": ["../escape"]},
        headers=rig.head(Principal.DOOR_OWUI),
    )

    assert answer.status_code == HTTP_BAD_REQUEST


async def test_wait_accepted_answers_at_once(rig: Rig) -> None:
    await rig.create()
    answer = await rig.client.post(
        f"{rig.path()}/turns",
        json={"prompt": PROMPT, "wait": "accepted"},
        headers=rig.head(Principal.DOOR_OWUI),
    )
    body = answer.json()

    assert answer.status_code == HTTP_ACCEPTED
    assert body["state"] == "running"
    assert body["journal_seq"] >= 1


async def test_wait_settled_answers_with_the_text(rig: Rig) -> None:
    await rig.create()
    calling = asyncio.create_task(
        rig.client.post(
            f"{rig.path()}/turns",
            json={"prompt": PROMPT, "wait": "settled"},
            headers=rig.head(Principal.DOOR_OWUI),
        )
    )
    playpen = await _await_playpen(rig)
    sent = await playpen.next_start()
    await playpen.play_turn(CHAT_SESSION, sent["turn"], ANSWER)
    await playpen.settle(CHAT_SESSION, sent["turn"])
    answer = await asyncio.wait_for(calling, 3.0)
    body = answer.json()

    assert answer.status_code == HTTP_OK
    assert body["state"] == "settled"
    assert body["text"] == ANSWER
    assert body["usage"]["cost_usd"] == pytest.approx(0.014)


async def test_wait_stream_yields_ndjson_to_the_terminal_line(rig: Rig) -> None:
    await rig.create()
    lines: list[dict[str, int]] = []

    async def read() -> None:
        async with rig.client.stream(
            "POST",
            f"{rig.path()}/turns",
            json={"prompt": PROMPT, "wait": "stream"},
            headers=rig.head(Principal.DOOR_OWUI),
        ) as answer:
            assert answer.status_code == HTTP_OK
            assert answer.headers["content-type"].startswith(NDJSON)

            async for raw in answer.aiter_lines():
                if raw:
                    lines.append(json.loads(raw))

    reading = asyncio.create_task(read())
    playpen = await _await_playpen(rig)
    sent = await playpen.next_start()
    await playpen.play_turn(CHAT_SESSION, sent["turn"], ANSWER)
    await playpen.settle(CHAT_SESSION, sent["turn"])
    await asyncio.wait_for(reading, 3.0)

    seqs = [line["journal_seq"] for line in lines]

    assert lines[0]["kind"] == "turn_started"
    assert lines[-1]["kind"] == "turn_settled"
    assert seqs == sorted(seqs)


async def test_the_event_stream_replays(rig: Rig) -> None:
    await rig.create()
    answer = await rig.client.get(
        f"{rig.path()}/events?follow=false", headers=rig.head(Principal.VIEW_RO)
    )
    lines = [json.loads(raw) for raw in answer.text.splitlines() if raw]

    assert answer.status_code == HTTP_OK
    assert lines[0]["kind"] == "session_created"
    assert lines[0]["journal_seq"] == 1


async def test_an_idle_lease_passes_to_the_terminal(rig: Rig) -> None:
    """Contract 02 §7.3 rule 5. This is invariant 3's "continue it here"."""
    await rig.create()
    first = await rig.client.post(
        f"{rig.path()}/writer",
        json={"holder": "owui", "force": False},
        headers=rig.head(Principal.DOOR_OWUI, "owui-1"),
    )
    second = await rig.client.post(
        f"{rig.path()}/writer",
        json={"holder": "tui", "force": False},
        headers=rig.head(Principal.DOOR_TUI, "tui.4021"),
    )

    assert first.status_code == HTTP_OK
    assert first.json()["holder"] == "owui"
    assert second.status_code == HTTP_OK
    assert second.json()["holder"] == "tui"


async def test_a_renew_that_lost_the_lease_is_told_so(rig: Rig) -> None:
    """Contract 02 §7.4 rule 2. A renew never takes a session back."""
    await rig.create()
    await rig.client.post(
        f"{rig.path()}/writer",
        json={"holder": "owui"},
        headers=rig.head(Principal.DOOR_OWUI, "owui-1"),
    )
    await rig.client.post(
        f"{rig.path()}/writer",
        json={"holder": "tui"},
        headers=rig.head(Principal.DOOR_TUI, "tui.4021"),
    )
    renew = await rig.client.post(
        f"{rig.path()}/writer",
        json={"holder": "owui", "intent": "renew"},
        headers=rig.head(Principal.DOOR_OWUI, "owui-1"),
    )
    body = renew.json()

    assert renew.status_code == 409
    assert body["error"]["code"] == "lease_taken_over"
    assert body["error"]["detail"]["holder"] == "tui"


async def test_an_unknown_intent_is_refused(rig: Rig) -> None:
    await rig.create()
    answer = await rig.client.post(
        f"{rig.path()}/writer",
        json={"holder": "owui", "intent": "borrow"},
        headers=rig.head(Principal.DOOR_OWUI, "owui-1"),
    )

    assert answer.status_code == HTTP_BAD_REQUEST


async def test_releasing_the_lease_twice_is_not_an_error(rig: Rig) -> None:
    """Contract 02 §5.10. A door on its way out never has to ask first."""
    await rig.create()
    await rig.client.post(
        f"{rig.path()}/writer",
        json={"holder": "owui"},
        headers=rig.head(Principal.DOOR_OWUI, "owui-1"),
    )
    first = await rig.client.delete(
        f"{rig.path()}/writer", headers=rig.head(Principal.DOOR_OWUI, "owui-1")
    )
    second = await rig.client.delete(
        f"{rig.path()}/writer", headers=rig.head(Principal.DOOR_OWUI, "owui-1")
    )

    assert first.status_code == HTTP_OK
    assert first.json() == {"released": True}
    assert second.json() == {"released": False}


async def test_releasing_a_process_no_sandbox_holds(rig: Rig) -> None:
    """Contract 02 §5.11 rule 4. The normal answer before a first turn."""
    await rig.create()
    answer = await rig.client.post(
        f"{rig.path()}/release-process", headers=rig.head(Principal.DOOR_TUI, "tui.4021")
    )

    assert answer.status_code == HTTP_OK
    assert answer.json() == {"released": False}


async def test_a_door_cannot_take_the_lease_as_another_door(rig: Rig) -> None:
    await rig.create()
    answer = await rig.client.post(
        f"{rig.path()}/writer",
        json={"holder": "tui"},
        headers=rig.head(Principal.DOOR_OWUI),
    )

    assert answer.status_code == HTTP_FORBIDDEN


async def test_list_and_get_carry_the_contract_fields(rig: Rig) -> None:
    await rig.create()
    listing = await rig.client.get(
        f"{SESSIONS}?family={FAMILY}&limit=10", headers=rig.head(Principal.VIEW_RO)
    )
    one = await rig.client.get(f"{rig.path()}?turns=5", headers=rig.head(Principal.VIEW_RO))
    body = one.json()

    assert listing.json()["sessions"][0]["session"] == CHAT_SESSION
    assert listing.json()["next_cursor"] is None
    assert body["kind"] == "attended"
    assert body["writer"] is None
    assert body["turns"] == []


async def test_stop_then_delete(rig: Rig) -> None:
    await rig.create()
    started = await rig.client.post(
        f"{rig.path()}/turns",
        json={"prompt": PROMPT, "wait": "accepted"},
        headers=rig.head(Principal.DOOR_OWUI),
    )
    turn = started.json()["turn"]

    stopped = await rig.client.post(
        f"{rig.path()}/turns/{turn}/stop",
        json={"reason": "user_stopped"},
        headers=rig.head(Principal.DOOR_OWUI),
    )
    deleted = await rig.client.request("DELETE", rig.path(), headers=rig.head(Principal.DOOR_OWUI))

    assert stopped.json()["state"] == "aborted"
    assert deleted.json() == {"deleted": True}
    assert not rig.service.store.exists(FAMILY, CHAT_SESSION)


async def test_stopping_an_unknown_turn_is_turn_not_found(rig: Rig) -> None:
    await rig.create()
    answer = await rig.client.post(
        f"{rig.path()}/turns/01JBQ7WZ0X4T9V6K2H8M3N5PQR/stop",
        json={},
        headers=rig.head(Principal.DOOR_OWUI),
    )

    assert answer.status_code == HTTP_NOT_FOUND
    assert answer.json()["error"]["code"] == "turn_not_found"


async def test_switch_sandbox_takes_only_the_caregiver_token(rig: Rig) -> None:
    """Contract 02 §3.1: no door reaches `/internal/*`."""
    body = {"family": FAMILY, "from": SANDBOX, "to": "chat-s2", "mode": "drain", "reason": "x"}
    refused = await rig.client.post(
        "/internal/switch-sandbox", json=body, headers=rig.head(Principal.DOOR_OWUI)
    )
    # The rig publishes one sandbox, so `to` is not one this service would
    # dial and contract 05 §5.3 rule 8 refuses it. The token reached it.
    reached = await rig.client.post(
        "/internal/switch-sandbox", json=body, headers=rig.head(Principal.CAREGIVER)
    )

    assert refused.status_code == HTTP_FORBIDDEN
    assert reached.status_code == HTTP_BAD_REQUEST
    assert reached.json()["error"]["code"] == "bad_request"


# One half of a surrogate pair, as the JSON escape of a body. A text that
# holds it has no UTF-8 form, so no answer and no line can hold the text.
HALF_PAIR = "\\ud800"
NOT_JSON_ERROR = {
    "code": "bad_request",
    "message": "body is not JSON",
    "family": None,
    "session": None,
    "turn": None,
    "detail": {},
}
OTHER_SANDBOX = "chat-s2"
JOB_FAMILY = "issue-worker"
JOB_SANDBOX = "issue-worker-s1"
JOB_SESSION = "auto-01JBQ7ZZ9D6M0Q4RXT2J8HYVBK"
DELEGATION = {
    "caller_family": FAMILY,
    "target_family": FAMILY,
    "delegation_id": "01JBQ7WZ0X4T9V6K2H8M3N5PQR",
    "message": "Summarise the ticket.",
}


def _body(**fields: object) -> bytes:
    """A JSON body. `@` in a text stands for one half of a surrogate pair."""
    return json.dumps(fields).replace("@", HALF_PAIR).encode()


CREATE_TEXTS: dict[str, dict[str, object]] = {
    "title": {"title": "Kitchen@"},
    "label_value": {"labels": {"room": "@"}},
    "label_key": {"labels": {"@": "kitchen"}},
    "family": {"family": "chat@"},
    "session": {"session": "owui-@"},
    "owner_session": {"owner_session": "owui-@"},
}

TURN_TEXTS: dict[str, dict[str, object]] = {
    "prompt": {"prompt": "Which sensor@"},
    "idempotency_key": {"idempotency_key": "key-@"},
    "persona_text": {"persona_text": "You answer@"},
    "label_value": {"labels": {"room": "@"}},
    "label_key": {"labels": {"@": "kitchen"}},
    "owui_chat_id": {"owui": {"chat_id": "@", "message_id": "m"}},
    "owui_message_id": {"owui": {"chat_id": "c", "message_id": "@"}},
    "owui_user_message_id": {"owui": {"chat_id": "c", "message_id": "m", "user_message_id": "@"}},
    "owui_parent_id": {"owui": {"chat_id": "c", "message_id": "m", "parent_id": "@"}},
    "wait": {"wait": "accepted@"},
}

# One route, the token that reaches it and one body for each text of the
# route. No such body needs a session.
OTHER_TEXTS: dict[str, tuple[str, Principal, dict[str, object]]] = {
    "delegate_message": ("/delegate", Principal.DOOR_DELEGATE, {**DELEGATION, "message": "@"}),
    "delegate_caller": (
        "/delegate",
        Principal.DOOR_DELEGATE,
        {**DELEGATION, "caller_family": "chat@"},
    ),
    "dispatch_message": ("/dispatch", Principal.DOOR_DISPATCH, {**DELEGATION, "message": "@"}),
    "dispatch_target": (
        "/dispatch",
        Principal.DOOR_DISPATCH,
        {**DELEGATION, "target_family": "chat@"},
    ),
    "jobs_caller": ("/dispatch/jobs", Principal.DOOR_DISPATCH, {"caller_family": "chat@"}),
    "jobs_session": (
        "/dispatch/jobs",
        Principal.DOOR_DISPATCH,
        {"caller_family": FAMILY, "session": "auto-@"},
    ),
    "switch_family": (
        "/internal/switch-sandbox",
        Principal.CAREGIVER,
        {"family": "chat@", "to": SANDBOX, "mode": "drain"},
    ),
}


@pytest.mark.parametrize("fields", list(CREATE_TEXTS.values()), ids=list(CREATE_TEXTS))
async def test_a_create_text_with_no_utf8_form_is_refused(
    rig: Rig, fields: dict[str, object]
) -> None:
    """The parser refuses the text. No session with such a text reaches the
    store, so each later list of sessions has an answer."""
    body = _body(**{"family": FAMILY, "session": CHAT_SESSION, **fields})
    answer = await rig.client.post(SESSIONS, content=body, headers=rig.head(Principal.DOOR_OWUI))
    listed = await rig.client.get(SESSIONS, headers=rig.head(Principal.VIEW_RO))

    assert answer.status_code == HTTP_BAD_REQUEST
    assert answer.json()["error"] == NOT_JSON_ERROR
    assert listed.status_code == HTTP_OK
    assert listed.json()["sessions"] == []


@pytest.mark.parametrize("fields", list(TURN_TEXTS.values()), ids=list(TURN_TEXTS))
async def test_a_turn_text_with_no_utf8_form_is_refused(
    rig: Rig, fields: dict[str, object]
) -> None:
    """No turn starts, and the session still has an answer."""
    await rig.create()
    body = _body(**{"prompt": PROMPT, "wait": "accepted", **fields})
    answer = await rig.client.post(
        f"{rig.path()}/turns", content=body, headers=rig.head(Principal.DOOR_OWUI)
    )
    session = await rig.client.get(rig.path(), headers=rig.head(Principal.DOOR_OWUI))

    assert answer.status_code == HTTP_BAD_REQUEST
    assert answer.json()["error"] == NOT_JSON_ERROR
    assert session.status_code == HTTP_OK
    assert session.json()["turns_total"] == 0


@pytest.mark.parametrize("call", list(OTHER_TEXTS.values()), ids=list(OTHER_TEXTS))
async def test_a_text_with_no_utf8_form_is_refused_on_each_route(
    rig: Rig, call: tuple[str, Principal, dict[str, object]]
) -> None:
    path, principal, fields = call
    answer = await rig.client.post(path, content=_body(**fields), headers=rig.head(principal))

    assert answer.status_code == HTTP_BAD_REQUEST
    assert answer.json()["error"] == NOT_JSON_ERROR


async def test_a_steer_text_with_no_utf8_form_is_refused(rig: Rig) -> None:
    """The turn runs on."""
    await rig.create()
    head = rig.head(Principal.DOOR_OWUI)
    started = await rig.client.post(
        f"{rig.path()}/turns", json={"prompt": PROMPT, "wait": "accepted"}, headers=head
    )
    turn = f"{rig.path()}/turns/{started.json()['turn']}"
    steered = await rig.client.post(f"{turn}/steer", content=_body(message="@"), headers=head)
    session = await rig.client.get(rig.path(), headers=head)

    assert steered.status_code == HTTP_BAD_REQUEST
    assert steered.json()["error"] == NOT_JSON_ERROR
    assert session.json()["state"] == "running"


# Four texts reach a file and the event stream only, and each of those holds
# the escape. The service took such a text before, and it still does.


async def test_a_stop_reason_with_no_utf8_form_is_read(rig: Rig) -> None:
    """The stop ends the turn, and each later read of the session answers."""
    await rig.create()
    head = rig.head(Principal.DOOR_OWUI)
    started = await rig.client.post(
        f"{rig.path()}/turns", json={"prompt": PROMPT, "wait": "accepted"}, headers=head
    )
    turn = f"{rig.path()}/turns/{started.json()['turn']}"
    stopped = await rig.client.post(f"{turn}/stop", content=_body(reason="enough@"), headers=head)
    session = await rig.client.get(rig.path(), headers=head)
    events = await rig.client.get(f"{rig.path()}/events?follow=false", headers=head)

    assert stopped.status_code == HTTP_OK
    assert stopped.json()["state"] == "aborted"
    assert session.status_code == HTTP_OK
    assert events.status_code == HTTP_OK
    assert f'"message":"enough{HALF_PAIR}"' in events.text


async def test_a_switch_reason_with_no_utf8_form_is_read(rig: Rig) -> None:
    """The switch ends the turn of the old sandbox with that reason."""
    await rig.create()
    head = rig.head(Principal.DOOR_OWUI)
    await rig.client.post(
        f"{rig.path()}/turns", json={"prompt": PROMPT, "wait": "accepted"}, headers=head
    )
    await (await _await_playpen(rig)).next_start()
    write_status(rig.config.state_root, sandboxes=((SANDBOX, "ready"), (OTHER_SANDBOX, "ready")))
    body = _body(family=FAMILY, to=OTHER_SANDBOX, mode="interrupt", reason="removed@")
    body = body.replace(b'{"family"', b'{"from":"' + SANDBOX.encode() + b'","family"')
    switched = await rig.client.post(
        "/internal/switch-sandbox", content=body, headers=rig.head(Principal.CAREGIVER)
    )
    session = await rig.client.get(rig.path(), headers=head)
    events = await rig.client.get(f"{rig.path()}/events?follow=false", headers=head)

    assert switched.status_code == HTTP_OK
    assert switched.json()["turns_aborted"] == 1
    assert session.status_code == HTTP_OK
    assert events.status_code == HTTP_OK
    assert f'"message":"removed{HALF_PAIR}"' in events.text


async def test_a_trigger_name_with_no_utf8_form_is_read(rig: Rig) -> None:
    """The turn starts, and the session and the list still answer."""
    write_status(
        rig.config.state_root,
        family=JOB_FAMILY,
        kind="autonomous",
        sandboxes=((JOB_SANDBOX, "ready"),),
    )
    head = rig.head(Principal.DOOR_TRIGGER)
    path = f"{SESSIONS}/{JOB_FAMILY}/{JOB_SESSION}"
    await rig.client.post(
        SESSIONS, json={"family": JOB_FAMILY, "session": JOB_SESSION}, headers=head
    )
    body = _body(prompt=PROMPT, wait="accepted", trigger={"kind": "timer", "name": "nightly@"})
    started = await rig.client.post(f"{path}/turns", content=body, headers=head)
    session = await rig.client.get(path, headers=head)
    listed = await rig.client.get(SESSIONS, headers=rig.head(Principal.VIEW_RO))

    assert started.status_code == HTTP_ACCEPTED
    assert session.status_code == HTTP_OK
    assert session.json()["turns_total"] == 1
    assert listed.status_code == HTTP_OK


async def test_a_dispatch_key_with_no_utf8_form_is_read(rig: Rig) -> None:
    """A repeat with the same key finds the job, and the job list answers."""
    write_status(
        rig.config.state_root,
        family=JOB_FAMILY,
        kind="autonomous",
        sandboxes=((JOB_SANDBOX, "ready"),),
        accepts_dispatch=True,
    )
    head = rig.head(Principal.DOOR_DISPATCH)
    body = _body(**{**DELEGATION, "target_family": JOB_FAMILY, "idempotency_key": "morning-@"})
    first = await rig.client.post("/dispatch", content=body, headers=head)
    second = await rig.client.post("/dispatch", content=body, headers=head)
    jobs = await rig.client.post("/dispatch/jobs", json={"caller_family": FAMILY}, headers=head)

    assert first.status_code == HTTP_OK
    assert first.json()["created"] is True
    assert second.status_code == HTTP_OK
    assert second.json() == {**first.json(), "created": False}
    assert jobs.status_code == HTTP_OK
    assert [job["session"] for job in jobs.json()["jobs"]] == [first.json()["session_id"]]


async def test_a_text_with_a_whole_surrogate_pair_is_read(rig: Rig) -> None:
    """The escape of a pair is one character, and it has a UTF-8 form."""
    body = b'{"family":"chat","session":"' + CHAT_SESSION.encode() + b'","title":"\\ud83d\\ude00"}'
    answer = await rig.client.post(SESSIONS, content=body, headers=rig.head(Principal.DOOR_OWUI))

    assert answer.status_code == HTTP_CREATED
    assert answer.json()["title"] == "\U0001f600"


async def _await_playpen(rig: Rig) -> FakePlaypen:
    """The channel is dialled inside the request, so wait for the dial."""
    for _ in range(400):
        if SANDBOX in rig.fleet.playpens:
            return rig.fleet.playpen()

        await asyncio.sleep(0.005)

    raise AssertionError("the service never dialled a sandbox")
