"""The trigger door: `door-trigger` starts one job in an autonomous family.

Each firing is one `auto-<ulid>` job in the family's `JobQueue`, beside
the dispatched jobs. Every test runs against `FakeFacade` and a temporary
state root, so no test needs the host, `sbx` or the PEP.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from attendance.acp_link import METHOD_NEW_SESSION, METHOD_PROMPT
from attendance.api import build_app
from attendance.auth import Principal, TokenBook
from attendance.dispatch import DispatchLedger, LedgerDoor
from attendance.errors import ApiError, ErrorCode
from attendance.models import TriggerKind
from attendance.paths import outcome_file
from attendance.requests import JobQuery, TriggerRequest, read_trigger, read_trigger_jobs
from attendance.service import SessionService
from attendance_harness import make_config, wait_until, write_status, write_tokens
from creche_vectors import meta_key
from fake_facade import FacadeFleet, FakeFacade, PromptMode

TRIGGER_DOOR = Principal.DOOR_TRIGGER
FAMILY = "ha-review"
SANDBOX = "ha-review-s1"
CHAT_FAMILY = "chat"
WEBHOOK = "deploy-notify"
MESSAGE = "This turn was started by an autonomous trigger."
KEY = "01JBQ7ZZ9D6M0Q4RXT2J8HYVBK"
SECOND_KEY = "01JBQ7YY1C5L9P3QWS1H7GXUAJ"
HTTP_OK = 200
HTTP_FORBIDDEN = 403


class TriggerHarness:
    """One service that serves one autonomous family and one attended family."""

    def __init__(self, service: SessionService, facades: FacadeFleet, state_root: Path) -> None:
        self.service = service
        self.facades = facades
        self.state_root = state_root

    async def fire(self, **changed: Any) -> dict[str, Any]:
        return await self.service.run_trigger(TRIGGER_DOOR, _request(**changed))

    def jobs(self, **changed: Any) -> list[dict[str, Any]]:
        query = JobQuery(caller_family=changed.pop("family", FAMILY), **changed)
        listed: object = self.service.read_trigger_jobs(TRIGGER_DOOR, query)["jobs"]
        assert isinstance(listed, list)
        return listed  # pyright: ignore[reportUnknownVariableType]

    def facade(self) -> FakeFacade:
        return self.facades.facades[SANDBOX]

    def received(self, method: str) -> list[dict[str, Any]]:
        return [one["params"] for one in self.facade().received if one["method"] == method]

    async def held(self, count: int) -> list[str]:
        await wait_until(lambda: SANDBOX in self.facades.facades)
        await wait_until(lambda: len(self.facade().held) == count)
        return list(self.facade().held)

    async def ended(self, session: str) -> dict[str, Any]:
        await wait_until(lambda: self.jobs(session=session)[0]["outcome"] is not None)
        outcome: object = self.jobs(session=session)[0]["outcome"]
        assert isinstance(outcome, dict)
        return outcome  # pyright: ignore[reportUnknownVariableType]

    async def stop(self) -> None:
        await self.service.close()
        await self.facades.stop()


async def build(
    tmp_path: Path, mode: PromptMode = PromptMode.HOLD, **limits: object
) -> TriggerHarness:
    config = make_config(tmp_path)
    write_status(config.state_root, family=CHAT_FAMILY)
    write_status(
        config.state_root,
        family=FAMILY,
        kind="autonomous",
        sandboxes=((SANDBOX, "ready"),),
        **limits,  # pyright: ignore[reportArgumentType]
    )
    facades = FacadeFleet(prompt_mode=mode)
    service = SessionService(
        config, acp_factory=facades.factory, cold_start_wait_s=1.0, cancel_wait_s=0.2
    )
    service.start()
    return TriggerHarness(service, facades, config.state_root)


@pytest.fixture
async def harness(tmp_path: Path) -> AsyncIterator[TriggerHarness]:
    built = await build(tmp_path)
    yield built
    await built.stop()


def _request(**changed: Any) -> TriggerRequest:
    fields: dict[str, Any] = {
        "family": FAMILY,
        "kind": TriggerKind.TIMER,
        "name": None,
        "message": MESSAGE,
        "idempotency_key": KEY,
    }
    fields.update(changed)
    return TriggerRequest(**fields)


# ------------------------------------------------------------- the service


async def test_a_trigger_posts_one_job(harness: TriggerHarness) -> None:
    """One firing is one `auto-<ulid>` job: one `session/new`, one prompt."""
    answer = await harness.fire()
    session = str(answer["session_id"])
    await harness.held(1)

    assert session.startswith("auto-")
    assert answer == {"session_id": session, "status": "running", "created": True}
    assert harness.received(METHOD_NEW_SESSION) == [
        {**harness.received(METHOD_NEW_SESSION)[0], "_meta": {meta_key("job"): session}}
    ]
    [prompt] = harness.received(METHOD_PROMPT)
    assert prompt["prompt"] == [{"type": "text", "text": MESSAGE}]
    trigger = prompt["_meta"][meta_key("trigger")]
    assert trigger == {"kind": "timer", "name": None, "fired_at": trigger["fired_at"]}
    assert [job["status"] for job in harness.jobs()] == ["running"]


async def test_a_repeat_with_the_same_key_enqueues_nothing(harness: TriggerHarness) -> None:
    first = await harness.fire()
    second = await harness.fire()
    await harness.held(1)

    assert second == {**first, "created": False}
    assert len(harness.received(METHOD_NEW_SESSION)) == 1
    assert len(harness.jobs()) == 1


async def test_each_firing_is_a_fresh_session(harness: TriggerHarness) -> None:
    first = await harness.fire()
    second = await harness.fire(idempotency_key=SECOND_KEY)
    await harness.held(2)

    assert first["session_id"] != second["session_id"]


async def test_a_firing_past_the_limit_is_queued(tmp_path: Path) -> None:
    """The family's `max_running_turns` holds for a trigger as for a dispatch."""
    harness = await build(tmp_path, max_running_turns=1)

    try:
        first = await harness.fire()
        second = await harness.fire(idempotency_key=SECOND_KEY)
        await harness.held(1)

        assert first["status"] == "running"
        assert second["status"] == "queued"
        assert [job["status"] for job in harness.jobs()] == ["queued", "running"]
    finally:
        await harness.stop()


async def test_the_outcome_record_names_the_webhook(tmp_path: Path) -> None:
    """The quiet check reads this record under `outcomes/<family>/`."""
    harness = await build(tmp_path, PromptMode.OK)

    try:
        answer = await harness.fire(kind=TriggerKind.WEBHOOK, name=WEBHOOK)
        session = str(answer["session_id"])
        outcome = await harness.ended(session)

        assert outcome["session"] == session
        assert outcome["status"] == "ok"
        assert outcome["trigger"]["kind"] == "webhook"
        assert outcome["trigger"]["name"] == WEBHOOK
        assert "chain" not in outcome["trigger"]
        assert outcome_file(harness.state_root, FAMILY, str(outcome["id"])).is_file()
        assert harness.jobs()[0]["status"] == "ended"
    finally:
        await harness.stop()


async def test_a_trigger_job_is_not_a_dispatch_of_its_family(harness: TriggerHarness) -> None:
    """The trigger ledger is apart: `/dispatch/jobs` of the family lists nothing."""
    await harness.fire()

    assert DispatchLedger(harness.state_root).list(FAMILY) == []
    assert len(DispatchLedger(harness.state_root, LedgerDoor.TRIGGER).list(FAMILY)) == 1


async def test_an_attended_family_is_refused(harness: TriggerHarness) -> None:
    """The trigger door's grant is `autonomous` (`auth.py`)."""
    with pytest.raises(ApiError) as raised:
        await harness.fire(family=CHAT_FAMILY)

    assert raised.value.code is ErrorCode.FORBIDDEN
    assert harness.jobs(family=CHAT_FAMILY) == []


@pytest.mark.parametrize("principal", [Principal.DOOR_DISPATCH, Principal.DOOR_ACP])
async def test_only_the_trigger_door_fires(harness: TriggerHarness, principal: Principal) -> None:
    with pytest.raises(ApiError) as raised:
        await harness.service.run_trigger(principal, _request())

    assert raised.value.code is ErrorCode.FORBIDDEN
    assert harness.jobs() == []


# ------------------------------------------------------------- the request


def test_the_trigger_body_is_read() -> None:
    found = read_trigger(
        {
            "family": FAMILY,
            "kind": "webhook",
            "name": WEBHOOK,
            "message": MESSAGE,
            "idempotency_key": KEY,
        }
    )

    assert found == _request(kind=TriggerKind.WEBHOOK, name=WEBHOOK)


BAD_BODIES: dict[str, dict[str, object]] = {
    "dispatch_kind": {"kind": "dispatch"},
    "webhook_no_name": {"kind": "webhook"},
    "webhook_bad_name": {"kind": "webhook", "name": "Deploy Notify"},
    "timer_with_name": {"name": WEBHOOK},
    "name_not_text": {"name": 7},
    "no_key": {"idempotency_key": None},
    "long_key": {"idempotency_key": "k" * 129},
    "bad_family": {"family": "HA"},
    "no_message": {"message": ""},
}


@pytest.mark.parametrize("changed", list(BAD_BODIES.values()), ids=list(BAD_BODIES))
def test_a_bad_trigger_body_is_refused(changed: dict[str, object]) -> None:
    body: dict[str, object] = {
        "family": FAMILY,
        "kind": "timer",
        "message": MESSAGE,
        "idempotency_key": KEY,
    }
    body.update(changed)

    with pytest.raises(ApiError) as raised:
        read_trigger({name: value for name, value in body.items() if value is not None})

    assert raised.value.code is ErrorCode.BAD_REQUEST


def test_the_trigger_job_query_is_read() -> None:
    found = read_trigger_jobs({"family": FAMILY, "session": "auto-" + KEY, "limit": 5})

    assert found == JobQuery(caller_family=FAMILY, session="auto-" + KEY, limit=5)


# ---------------------------------------------------------------- the HTTP


async def test_the_routes_answer_json(tmp_path: Path, harness: TriggerHarness) -> None:
    """`POST /trigger`, then `POST /trigger/jobs`, with the door's token."""
    tokens = write_tokens(harness.state_root)
    book = TokenBook(harness.state_root)
    book.load()
    transport = httpx.ASGITransport(app=build_app(harness.service, book))
    head = {"Authorization": f"Bearer {tokens[TRIGGER_DOOR]}"}
    body = {"family": FAMILY, "kind": "timer", "message": MESSAGE, "idempotency_key": KEY}

    async with httpx.AsyncClient(transport=transport, base_url="http://sessiond") as client:
        fired = await client.post("/trigger", json=body, headers=head)
        session = fired.json()["session_id"]
        read = await client.post(
            "/trigger/jobs", json={"family": FAMILY, "session": session}, headers=head
        )
        other = {"Authorization": f"Bearer {tokens[Principal.DOOR_DISPATCH]}"}
        refused = await client.post("/trigger", json=body, headers=other)

    assert fired.status_code == HTTP_OK
    assert fired.json() == {"session_id": session, "status": "running", "created": True}
    assert read.status_code == HTTP_OK
    [job] = read.json()["jobs"]
    assert job == {
        "session": session,
        "family": FAMILY,
        "enqueued_at": job["enqueued_at"],
        "status": "running",
        "outcome": None,
    }
    assert refused.status_code == HTTP_FORBIDDEN
