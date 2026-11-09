"""The HTTP surface: auth, the routes that remain, and the refusal body."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from attendance.api import build_app
from attendance.auth import Principal, TokenBook
from attendance.config import Config
from attendance.service import SessionService
from attendance_harness import FAMILY, make_config, make_service, write_status, write_tokens
from fake_facade import PromptMode
from fake_vmm import FakeVmm

JOBS = "/dispatch/jobs"
SWITCH = "/internal/switch-sandbox"
HTTP_OK = 200
HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403
HTTP_NOT_FOUND = 404
HTTP_INTERNAL = 500


@dataclass(slots=True)
class Rig:
    """The app, the service and the tokens each door uses."""

    config: Config
    service: SessionService
    client: httpx.AsyncClient
    tokens: dict[Principal, str]
    vmm: FakeVmm

    def head(self, principal: Principal) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens[principal]}"}

    async def stop(self) -> None:
        await self.client.aclose()
        await self.service.close()
        await self.vmm.stop()


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    config = make_config(tmp_path)
    write_status(config.state_root)
    made = write_tokens(config.state_root)
    book = TokenBook(config.state_root)
    book.load()

    # A held prompt keeps a dispatched job running while a test reads it.
    vmm = FakeVmm(prompt_mode=PromptMode.HOLD)
    service = make_service(config, vmm)
    service.start()
    app = build_app(service, book)
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://sessiond")
    built = Rig(config, service, client, made, vmm)

    yield built

    await built.stop()


async def test_a_request_without_a_token_is_unauthorized(rig: Rig) -> None:
    answer = await rig.client.post(JOBS)

    assert answer.status_code == HTTP_UNAUTHORIZED
    assert answer.json()["error"]["code"] == "unauthorized"


async def test_the_view_token_reaches_no_route(rig: Rig) -> None:
    answer = await rig.client.post(
        JOBS, json={"caller_family": FAMILY}, headers=rig.head(Principal.NOTICEBOARD)
    )

    assert answer.status_code == HTTP_FORBIDDEN


@pytest.mark.parametrize(
    "path", ["/v1/sessions", f"/v1/sessions/{FAMILY}/owui-a", f"/v1/sessions/{FAMILY}/owui-a/turns"]
)
async def test_the_session_api_is_gone(rig: Rig, path: str) -> None:
    """Phase 2 plan, row 21. An ACP client speaks to `WS /acp/{family}`."""
    listed = await rig.client.get(path, headers=rig.head(Principal.NOTICEBOARD))
    posted = await rig.client.post(path, json={}, headers=rig.head(Principal.DOOR_TRIGGER))

    assert listed.status_code == HTTP_NOT_FOUND
    assert posted.status_code == HTTP_NOT_FOUND


async def test_a_malformed_body_is_bad_request(rig: Rig) -> None:
    answer = await rig.client.post(
        JOBS, content=b"{nope", headers=rig.head(Principal.CHAPERONE_DISPATCH)
    )

    assert answer.status_code == HTTP_BAD_REQUEST
    assert answer.json()["error"]["code"] == "bad_request"


async def test_a_body_nested_too_deep_is_bad_request(rig: Rig) -> None:
    """The JSON reader raises RecursionError on this body, not ValueError."""
    answer = await rig.client.post(
        JOBS, content=b"[" * 200_000, headers=rig.head(Principal.CHAPERONE_DISPATCH)
    )

    assert answer.status_code == HTTP_BAD_REQUEST
    assert answer.json()["error"]["code"] == "bad_request"


async def test_an_exception_no_route_expects_is_internal(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contract 02 §14. `internal` has the body of each other failure, and the
    body holds no text of the exception."""

    def failing(*_: object) -> None:
        raise RuntimeError("the listing failed")

    monkeypatch.setattr(rig.service, "read_jobs", failing)
    book = TokenBook(rig.config.state_root)
    book.load()
    # The rig's own transport raises an exception of the app into the test.
    # This one answers as a server does.
    transport = httpx.ASGITransport(app=build_app(rig.service, book), raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://sessiond") as client:
        answer = await client.post(
            JOBS, json={"caller_family": FAMILY}, headers=rig.head(Principal.CHAPERONE_DISPATCH)
        )

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


@pytest.mark.parametrize("principal", list(Principal))
async def test_the_switch_route_is_gone(rig: Rig, principal: Principal) -> None:
    """Contract 05 minor 18 dropped the switch call (decision CH21-1)."""
    body = {"family": FAMILY, "to": "chat-s2", "mode": "drain"}
    answer = await rig.client.post(SWITCH, json=body, headers=rig.head(principal))

    assert answer.status_code == HTTP_NOT_FOUND


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
JOB_FAMILY = "issue-worker"
DELEGATION = {
    "caller_family": FAMILY,
    "target_family": FAMILY,
    "delegation_id": "01JBQ7WZ0X4T9V6K2H8M3N5PQR",
    "message": "Summarise the ticket.",
    "chain": [FAMILY, JOB_FAMILY],
}


def _body(**fields: object) -> bytes:
    """A JSON body. `@` in a text stands for one half of a surrogate pair."""
    return json.dumps(fields).replace("@", HALF_PAIR).encode()


# One route, the token that reaches it and one body for each text of the
# route. No such body needs a session.
OTHER_TEXTS: dict[str, tuple[str, Principal, dict[str, object]]] = {
    "delegate_message": ("/delegate", Principal.CHAPERONE_DELEGATE, {**DELEGATION, "message": "@"}),
    "delegate_caller": (
        "/delegate",
        Principal.CHAPERONE_DELEGATE,
        {**DELEGATION, "caller_family": "chat@"},
    ),
    "dispatch_message": ("/dispatch", Principal.CHAPERONE_DISPATCH, {**DELEGATION, "message": "@"}),
    "dispatch_target": (
        "/dispatch",
        Principal.CHAPERONE_DISPATCH,
        {**DELEGATION, "target_family": "chat@"},
    ),
    "jobs_caller": ("/dispatch/jobs", Principal.CHAPERONE_DISPATCH, {"caller_family": "chat@"}),
    "jobs_session": (
        "/dispatch/jobs",
        Principal.CHAPERONE_DISPATCH,
        {"caller_family": FAMILY, "session": "auto-@"},
    ),
}


@pytest.mark.parametrize("call", list(OTHER_TEXTS.values()), ids=list(OTHER_TEXTS))
async def test_a_text_with_no_utf8_form_is_refused_on_each_route(
    rig: Rig, call: tuple[str, Principal, dict[str, object]]
) -> None:
    path, principal, fields = call
    answer = await rig.client.post(path, content=_body(**fields), headers=rig.head(principal))

    assert answer.status_code == HTTP_BAD_REQUEST
    assert answer.json()["error"] == NOT_JSON_ERROR


# One text reaches a file only, and its file holds the escape. The service
# took such a text before, and it still does.


async def test_a_dispatch_key_with_no_utf8_form_is_read(rig: Rig) -> None:
    """A repeat with the same key finds the job, and the job list answers."""
    write_status(
        rig.config.state_root,
        family=JOB_FAMILY,
        kind="autonomous",
        accepts_dispatch=True,
    )
    head = rig.head(Principal.CHAPERONE_DISPATCH)
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
    write_status(
        rig.config.state_root,
        family=JOB_FAMILY,
        kind="autonomous",
        accepts_dispatch=True,
    )
    fields = {**DELEGATION, "target_family": JOB_FAMILY, "message": "smile @"}
    body = json.dumps(fields).replace("@", "\\ud83d\\ude00").encode()
    answer = await rig.client.post(
        "/dispatch", content=body, headers=rig.head(Principal.CHAPERONE_DISPATCH)
    )

    assert answer.status_code == HTTP_OK
    assert answer.json()["created"] is True
