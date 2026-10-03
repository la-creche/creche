"""The dispatch door: one family enqueues a job in another (contract 02 §13.4).

Everything here runs against a fake playpen and a temporary state root, so
no test needs the host, `sbx` or the PEP.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from attendance.auth import Principal
from attendance.clock import now, rfc3339
from attendance.config import Config
from attendance.dispatch import (
    IDEMPOTENCY_WINDOW,
    DispatchEntry,
    DispatchLedger,
    JobStatus,
    status_of,
)
from attendance.errors import ApiError, ErrorCode
from attendance.models import TriggerKind
from attendance.paths import dispatch_file
from attendance.requests import DispatchRequest, JobQuery, read_dispatch, read_jobs
from attendance.service import SessionService
from attendance.states import SessionState
from attendance_harness import FakeFleet, make_config, settle_now, wait_until, write_status

DISPATCH_DOOR = Principal.DOOR_DISPATCH
TRIGGER_DOOR = Principal.DOOR_TRIGGER
CALLER = "scrum-lead"
TARGET = "issue-worker"
TARGET_SANDBOX = "issue-worker-s1"
CHAT_FAMILY = "chat"
DELEGATION = "01K5J9QWB2M4N6Q8S0V2W4Y6A8"
SECOND_DELEGATION = "01K5J9QWB9R1V3T5Y7H9J2K4P6"
CHAIN = ("chat", "scrum-lead", "issue-worker")
MESSAGE = "Take ticket 412 and open a PR."
DOOR = "pep-1"
JOB_SESSION = "auto-01JBQ7ZZ9D6M0Q4RXT2J8HYVBK"
OLDER_SESSION = "auto-01JBQ7YY1C5L9P3QWS1H7GXUAJ"
OUTCOME_ID = "01JBQ80M4F7S2YQ1VZK6W3TDEN"


class DispatchHarness:
    """One service serving a dispatchable target and an attended family."""

    def __init__(self, config: Config, service: SessionService, fleet: FakeFleet) -> None:
        self.config = config
        self.service = service
        self.fleet = fleet

    async def enqueue(self, **changed: Any) -> dict[str, Any]:
        return await self.service.run_dispatch(DISPATCH_DOOR, _request(**changed), DOOR)

    def jobs(self, **changed: Any) -> list[dict[str, Any]]:
        query = JobQuery(caller_family=changed.pop("caller_family", CALLER), **changed)
        answer = self.service.read_jobs(DISPATCH_DOOR, query)
        listed: object = answer["jobs"]
        assert isinstance(listed, list)
        return listed  # pyright: ignore[reportUnknownVariableType]

    def entry(self, session: str, caller: str = CALLER) -> DispatchEntry:
        found = DispatchLedger(self.config.state_root).read(caller, session)
        assert found is not None
        return found

    async def settle(self, session: str) -> None:
        """Play the turn the target started, then wait for the job to end."""
        # `accepted` answers before the dial (contract 02 §5.4).
        await wait_until(lambda: TARGET_SANDBOX in self.fleet.playpens)
        playpen = self.fleet.playpen(TARGET_SANDBOX)
        started = await playpen.next_start()
        turn = str(started["turn"])
        live = self.service.live_turn(TARGET, session, turn)
        assert live is not None
        await playpen.settle(session, turn)
        await settle_now(live.done)

    async def stop(self) -> None:
        await self.service.close()
        await self.fleet.stop()


async def build(tmp_path: Path, **limits: object) -> DispatchHarness:
    config = make_config(tmp_path)
    write_status(config.state_root)
    write_status(
        config.state_root,
        family=TARGET,
        kind="autonomous",
        sandboxes=((TARGET_SANDBOX, "ready"),),
        accepts_dispatch=True,
        **limits,  # pyright: ignore[reportArgumentType]
    )
    fleet = FakeFleet()
    service = SessionService(config, factory=fleet.factory, cold_start_wait_s=1.0)
    service.start()
    return DispatchHarness(config, service, fleet)


def _request(**changed: Any) -> DispatchRequest:
    fields: dict[str, Any] = {
        "caller_family": CALLER,
        "target_family": TARGET,
        "delegation_id": DELEGATION,
        "message": MESSAGE,
        "chain": CHAIN,
    }
    fields.update(changed)
    return DispatchRequest(**fields)


def _entry(session: str = JOB_SESSION, **changed: Any) -> DispatchEntry:
    fields: dict[str, Any] = {
        "session": session,
        "caller_family": CALLER,
        "family": TARGET,
        "enqueued_at": now(),
        "delegation_id": DELEGATION,
        "chain": CHAIN,
    }
    fields.update(changed)
    return DispatchEntry(**fields)


# ------------------------------------------------------------- the request


def test_the_dispatch_body_is_read() -> None:
    """Contract 02 §13.4.1's field table."""
    found = read_dispatch(
        {
            "caller_family": CALLER,
            "target_family": TARGET,
            "delegation_id": DELEGATION,
            "chain": list(CHAIN),
            "claimed_session_id": "owui-8f1c2e",
            "idempotency_key": "morning-triage",
            "message": MESSAGE,
        }
    )

    assert found.caller_family == CALLER
    assert found.chain == CHAIN
    assert found.claimed_session_id == "owui-8f1c2e"
    assert found.idempotency_key == "morning-triage"


def test_a_delegation_id_that_is_no_ulid_is_refused() -> None:
    with pytest.raises(ApiError) as raised:
        read_dispatch(
            {
                "caller_family": CALLER,
                "target_family": TARGET,
                "delegation_id": "not-a-ulid",
                "message": MESSAGE,
            }
        )

    assert raised.value.code is ErrorCode.BAD_REQUEST


def test_a_chain_entry_that_is_no_family_is_refused() -> None:
    """§13.2 rule 6. It reaches a durable record, so it is validated."""
    with pytest.raises(ApiError):
        read_dispatch(
            {
                "caller_family": CALLER,
                "target_family": TARGET,
                "delegation_id": DELEGATION,
                "chain": ["chat", "NOT A FAMILY"],
                "message": MESSAGE,
            }
        )


def test_an_over_long_idempotency_key_is_refused() -> None:
    """Contract 04 §4.1's own 128 character cap."""
    with pytest.raises(ApiError):
        read_dispatch(
            {
                "caller_family": CALLER,
                "target_family": TARGET,
                "delegation_id": DELEGATION,
                "idempotency_key": "k" * 129,
                "message": MESSAGE,
            }
        )


def test_the_job_query_is_read() -> None:
    """Contract 02 §13.4.2's field table."""
    found = read_jobs({"caller_family": CALLER, "session": JOB_SESSION, "limit": 5})

    assert found.caller_family == CALLER
    assert found.session == JOB_SESSION
    assert found.limit == 5


def test_a_job_query_limit_outside_the_range_is_refused() -> None:
    """§13.4.2's field table: 1 to 200. This module refuses rather than
    clamps, so a caller learns its number was not the one used."""
    with pytest.raises(ApiError):
        read_jobs({"caller_family": CALLER, "limit": 9_000})


# -------------------------------------------------------------- the ledger


def test_an_entry_survives_a_round_trip(tmp_path: Path) -> None:
    """Contract 02 §13.4.3. The ledger outlives its session."""
    ledger = DispatchLedger(tmp_path)
    ledger.write(_entry())

    found = ledger.read(CALLER, JOB_SESSION)

    assert found is not None
    assert found.family == TARGET
    assert found.chain == CHAIN
    assert found.outcome_id is None


def test_another_family_reads_nothing(tmp_path: Path) -> None:
    """§13.4.2 rule 1. A caller reaches its own directory only."""
    ledger = DispatchLedger(tmp_path)
    ledger.write(_entry())

    assert ledger.read(CHAT_FAMILY, JOB_SESSION) is None
    assert ledger.list(CHAT_FAMILY) == []


def test_entries_are_listed_newest_first(tmp_path: Path) -> None:
    ledger = DispatchLedger(tmp_path)
    ledger.write(_entry(OLDER_SESSION))
    ledger.write(_entry(JOB_SESSION))

    assert [one.session for one in ledger.list(CALLER)] == [JOB_SESSION, OLDER_SESSION]


def test_a_limit_stops_the_listing(tmp_path: Path) -> None:
    ledger = DispatchLedger(tmp_path)
    ledger.write(_entry(OLDER_SESSION))
    ledger.write(_entry(JOB_SESSION))

    assert [one.session for one in ledger.list(CALLER, limit=1)] == [JOB_SESSION]


def test_since_drops_an_older_entry(tmp_path: Path) -> None:
    ledger = DispatchLedger(tmp_path)
    ledger.write(_entry(OLDER_SESSION, enqueued_at=datetime(2026, 9, 1, tzinfo=UTC)))
    ledger.write(_entry(JOB_SESSION))

    found = ledger.list(CALLER, since=now() - timedelta(minutes=5))

    assert [one.session for one in found] == [JOB_SESSION]


def test_an_outcome_id_is_noted(tmp_path: Path) -> None:
    ledger = DispatchLedger(tmp_path)
    ledger.write(_entry())

    ledger.note_outcome(CALLER, JOB_SESSION, OUTCOME_ID)

    found = ledger.read(CALLER, JOB_SESSION)
    assert found is not None
    assert found.outcome_id == OUTCOME_ID


def test_a_repeat_inside_the_window_is_found(tmp_path: Path) -> None:
    """Contract 02 §13.4.4 rule 2."""
    ledger = DispatchLedger(tmp_path)
    ledger.write(_entry(idempotency_key="morning"))

    found = ledger.find_repeat(CALLER, "morning")

    assert found is not None
    assert found.session == JOB_SESSION


def test_a_repeat_past_the_window_is_not_found(tmp_path: Path) -> None:
    """Rule 4. Past the hour the key is free."""
    ledger = DispatchLedger(tmp_path)
    stale = now() - IDEMPOTENCY_WINDOW - timedelta(minutes=1)
    ledger.write(_entry(idempotency_key="morning", enqueued_at=stale))

    assert ledger.find_repeat(CALLER, "morning") is None


def test_a_damaged_entry_is_skipped(tmp_path: Path) -> None:
    """Invariant 12. A file on disk is still another process's input."""
    ledger = DispatchLedger(tmp_path)
    ledger.write(_entry())
    dispatch_file(tmp_path, CALLER, JOB_SESSION).write_text("{}", encoding="utf-8")

    assert ledger.list(CALLER) == []


# ------------------------------------------------------------ the statuses


def test_an_outcome_id_ends_the_job() -> None:
    """§13.4.2 rule 4. The record wins over a session still being deleted."""
    assert status_of(SessionState.RUNNING, OUTCOME_ID) is JobStatus.ENDED


def test_a_missing_session_ends_the_job() -> None:
    assert status_of(None, None) is JobStatus.ENDED


def test_a_live_state_becomes_its_own_status() -> None:
    assert status_of(SessionState.QUEUED, None) is JobStatus.QUEUED
    assert status_of(SessionState.WAITING_APPROVAL, None) is JobStatus.WAITING_APPROVAL


def test_an_idle_session_reads_as_dispatched() -> None:
    """Rule 3. `running` would be a claim no turn supports."""
    assert status_of(SessionState.IDLE, None) is JobStatus.DISPATCHED


# ------------------------------------------------------------- the service


@pytest.mark.asyncio
async def test_a_dispatch_creates_one_job(tmp_path: Path) -> None:
    """Contract 02 §13.4.1 rule 1."""
    harness = await build(tmp_path)

    try:
        answer = await harness.enqueue()

        session: object = answer["session_id"]
        assert isinstance(session, str)
        assert session.startswith("auto-")
        assert answer["created"] is True
        assert harness.entry(session).family == TARGET
    finally:
        await harness.stop()


@pytest.mark.asyncio
async def test_the_job_carries_a_dispatch_trigger(tmp_path: Path) -> None:
    """§13.2 rule 5. `name` is the calling family, from the token."""
    harness = await build(tmp_path)

    try:
        answer = await harness.enqueue()
        session = str(answer["session_id"])
        record = harness.service.store.load(TARGET, session)

        assert record is not None
        assert record.trigger is not None
        assert record.trigger.kind is TriggerKind.DISPATCH
        assert record.trigger.name == CALLER
        assert record.trigger.chain == CHAIN
    finally:
        await harness.stop()


@pytest.mark.asyncio
async def test_a_caller_cannot_name_itself(tmp_path: Path) -> None:
    """The trigger's `name` comes from `caller_family`, never from a body."""
    harness = await build(tmp_path)

    try:
        answer = await harness.enqueue(caller_family=CHAT_FAMILY)
        session = str(answer["session_id"])
        record = harness.service.store.load(TARGET, session)

        assert record is not None
        assert record.trigger is not None
        assert record.trigger.name == CHAT_FAMILY
        assert harness.entry(session, caller=CHAT_FAMILY).caller_family == CHAT_FAMILY
    finally:
        await harness.stop()


@pytest.mark.asyncio
async def test_another_door_may_not_dispatch(tmp_path: Path) -> None:
    """Contract 02 §13.4. The write grant alone is not enough."""
    harness = await build(tmp_path)

    try:
        with pytest.raises(ApiError) as raised:
            await harness.service.run_dispatch(TRIGGER_DOOR, _request(), DOOR)

        assert raised.value.code is ErrorCode.FORBIDDEN
    finally:
        await harness.stop()


@pytest.mark.asyncio
async def test_an_attended_target_is_refused(tmp_path: Path) -> None:
    """The dispatch door's grant is `autonomous` (contract 02 §3.1)."""
    harness = await build(tmp_path)

    try:
        with pytest.raises(ApiError) as raised:
            await harness.enqueue(target_family=CHAT_FAMILY)

        assert raised.value.code is ErrorCode.FORBIDDEN
    finally:
        await harness.stop()


@pytest.mark.asyncio
async def test_a_family_without_the_trigger_is_refused(tmp_path: Path) -> None:
    """§13.4.1 rule 3. Absence is denial (invariant 11)."""
    harness = await build(tmp_path)
    write_status(
        harness.config.state_root,
        family=TARGET,
        kind="autonomous",
        sandboxes=((TARGET_SANDBOX, "ready"),),
        accepts_dispatch=False,
    )

    try:
        with pytest.raises(ApiError) as raised:
            await harness.enqueue()

        assert raised.value.code is ErrorCode.DISPATCH_NOT_DECLARED
    finally:
        await harness.stop()


@pytest.mark.asyncio
async def test_a_refused_dispatch_leaves_no_entry(tmp_path: Path) -> None:
    """§13.4.1 rule 5. Nothing is left for `job_status` to report."""
    harness = await build(tmp_path)
    write_status(
        harness.config.state_root,
        family=TARGET,
        kind="autonomous",
        sandboxes=((TARGET_SANDBOX, "ready"),),
        accepts_dispatch=False,
    )

    try:
        with pytest.raises(ApiError):
            await harness.enqueue()

        assert harness.jobs() == []
    finally:
        await harness.stop()


@pytest.mark.asyncio
async def test_a_repeat_starts_no_second_job(tmp_path: Path) -> None:
    """Contract 02 §13.4.4 rule 2."""
    harness = await build(tmp_path)

    try:
        first = await harness.enqueue(idempotency_key="morning")
        second = await harness.enqueue(idempotency_key="morning", delegation_id=SECOND_DELEGATION)

        assert second["session_id"] == first["session_id"]
        assert second["created"] is False
        assert len(harness.jobs()) == 1
    finally:
        await harness.stop()


@pytest.mark.asyncio
async def test_a_key_reused_on_another_family_is_refused(tmp_path: Path) -> None:
    """Rule 3. Answering with a job of another family would be a lie."""
    harness = await build(tmp_path)
    write_status(
        harness.config.state_root,
        family="finance-worker",
        kind="autonomous",
        sandboxes=(("finance-worker-s1", "ready"),),
        accepts_dispatch=True,
    )

    try:
        await harness.enqueue(idempotency_key="morning")

        with pytest.raises(ApiError) as raised:
            await harness.enqueue(idempotency_key="morning", target_family="finance-worker")

        assert raised.value.code is ErrorCode.IDEMPOTENCY_MISMATCH
    finally:
        await harness.stop()


@pytest.mark.asyncio
async def test_the_targets_queue_holds(tmp_path: Path) -> None:
    """§13.4.1 rule 4. The queue is the target's, and a full one refuses."""
    harness = await build(tmp_path, max_running_turns=1)

    try:
        await harness.enqueue()
        second = await harness.enqueue(delegation_id=SECOND_DELEGATION)

        assert second["status"] == "queued"
        assert harness.service.queue_depth(TARGET) == 1
    finally:
        await harness.stop()


# ----------------------------------------------------------------- the read


@pytest.mark.asyncio
async def test_a_live_job_reads_its_state(tmp_path: Path) -> None:
    """§13.4.2 rule 3."""
    harness = await build(tmp_path)

    try:
        answer = await harness.enqueue()
        listed = harness.jobs()

        assert len(listed) == 1
        assert listed[0]["session"] == answer["session_id"]
        assert listed[0]["family"] == TARGET
        assert listed[0]["status"] in {"running", "dispatched"}
        assert listed[0]["outcome"] is None
    finally:
        await harness.stop()


@pytest.mark.asyncio
async def test_another_callers_job_is_absent(tmp_path: Path) -> None:
    """§13.4.2 rule 2. An id leaks nothing, not even that it exists."""
    harness = await build(tmp_path)

    try:
        answer = await harness.enqueue()
        session = str(answer["session_id"])

        assert harness.jobs(caller_family=CHAT_FAMILY, session=session) == []
        assert harness.jobs(session="auto-01JBQ7WZ0X4T9V6K2H8M3N5PQR") == []
    finally:
        await harness.stop()


@pytest.mark.asyncio
async def test_a_finished_job_reads_its_outcome(tmp_path: Path) -> None:
    """§13.4.2 rules 3 and 5, and §13.4.3's `outcome_id`."""
    harness = await build(tmp_path)

    try:
        answer = await harness.enqueue()
        session = str(answer["session_id"])
        await harness.settle(session)
        await wait_until(lambda: harness.jobs()[0]["status"] == "ended")

        found = harness.jobs()[0]
        outcome: object = found["outcome"]

        assert isinstance(outcome, dict)
        assert outcome["session"] == session  # pyright: ignore[reportUnknownArgumentType]
        assert outcome["status"] == "ok"  # pyright: ignore[reportUnknownArgumentType]
        assert outcome["trigger"]["kind"] == "dispatch"  # pyright: ignore[reportUnknownArgumentType, reportIndexIssue]
        assert outcome["trigger"]["chain"] == list(CHAIN)  # pyright: ignore[reportUnknownArgumentType, reportIndexIssue]
    finally:
        await harness.stop()


@pytest.mark.asyncio
async def test_an_unreadable_outcome_still_ends_the_job(tmp_path: Path) -> None:
    """§13.4.2 rule 4. The job is over either way."""
    harness = await build(tmp_path)

    try:
        answer = await harness.enqueue()
        session = str(answer["session_id"])
        await harness.settle(session)
        await wait_until(lambda: harness.entry(session).outcome_id is not None)
        _remove_outcomes(harness.config.state_root)

        found = harness.jobs()[0]

        assert found["status"] == "ended"
        assert found["outcome"] is None
    finally:
        await harness.stop()


def _remove_outcomes(state_root: Path) -> None:
    for path in (state_root / "outcomes" / TARGET).glob("*.json"):
        path.unlink()


def _read(path: Path) -> dict[str, Any]:
    parsed: object = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed  # pyright: ignore[reportUnknownVariableType]


def _stamp(moment: datetime) -> str:
    return rfc3339(moment)
