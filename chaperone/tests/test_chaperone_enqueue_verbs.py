"""`enqueue` and `job_status` on the family path (contract 04 §4.1).

The decision core stays pure and the executor drives a fake door, so nothing
here needs `attendance`, a socket or the host.
"""

from __future__ import annotations

from typing import Any

import pytest
from chaperone.dispatch import (
    DispatchDoor,
    DispatchRefused,
    DispatchReply,
    DispatchRequest,
    JobQuery,
    JobsReply,
)
from chaperone.family_app import FamilyDeps, FamilyGate
from chaperone.family_audit import FamilyAudit
from chaperone.family_decisions import Executor, decide_family, manifest_actions
from chaperone.family_grants import FamilyStore
from chaperone.faults import FaultWriter
from chaperone.verbs import ENQUEUE, JOB_STATUS
from chaperone_family_helpers import FAMILY_TOKEN, make_grants, write_grants

LEAD = "scrum-lead"
WORKER = "issue-worker"
SESSION = "auto-01JBQ7ZZ9D6M0Q4RXT2J8HYVBK"
MESSAGE = "Take ticket 412."
VERBS: dict[str, Any] = {
    "enqueue": {"targets": [WORKER]},
    "job_status": {},
}


class FakeDispatchDoor:
    """A door that records what it was asked and answers what it was told."""

    def __init__(self, refuse: DispatchRefused | None = None) -> None:
        self.enqueued: list[DispatchRequest] = []
        self.queried: list[JobQuery] = []
        self.rows: list[dict[str, object]] = []
        self._refuse = refuse

    async def enqueue(self, request: DispatchRequest) -> DispatchReply:
        if self._refuse is not None:
            raise self._refuse

        self.enqueued.append(request)
        return DispatchReply(session=SESSION, status="queued", created=True)

    async def jobs(self, query: JobQuery) -> JobsReply:
        if self._refuse is not None:
            raise self._refuse

        self.queried.append(query)
        return JobsReply(jobs=list(self.rows))


def make_gate(tmp_path: Any, door: DispatchDoor | None) -> FamilyGate:
    grants_dir = tmp_path / "grants"
    write_grants(grants_dir, make_grants(family=LEAD, verbs=VERBS))

    return FamilyGate(
        FamilyDeps(
            store=FamilyStore(grants_dir, FaultWriter(tmp_path / "faults")),
            audit=FamilyAudit(tmp_path / "audit"),
            rate=lambda _key, _limit: False,
            pool=lambda: None,
            client=lambda: None,
            dispatch_door=door,
        )
    )


def grants_of() -> Any:
    return make_grants(family=LEAD, verbs=VERBS)


# ---- the decision ----------------------------------------------------------


def test_enqueue_is_a_seam_without_a_door() -> None:
    """A PEP with no dispatch door never allows a call it cannot make."""
    args = {"family": WORKER, "message": MESSAGE}
    found = decide_family(grants_of(), ENQUEUE, args, rate_exceeded=False)

    assert not found.allow
    assert found.reason == "not_implemented"


def test_enqueue_is_allowed_with_a_door() -> None:
    found = decide_family(
        grants_of(),
        ENQUEUE,
        {"family": WORKER, "message": MESSAGE},
        rate_exceeded=False,
        dispatch_ready=True,
    )

    assert found.allow
    assert found.executor is Executor.VERB


def test_a_target_outside_the_fence_is_refused() -> None:
    """Contract 04 §4.1: the verb was granted and the target was not."""
    found = decide_family(
        grants_of(),
        ENQUEUE,
        {"family": "finance-worker", "message": MESSAGE},
        rate_exceeded=False,
        dispatch_ready=True,
    )

    assert not found.allow
    assert found.reason == "tool_not_granted"


def test_job_status_is_allowed_with_a_door() -> None:
    found = decide_family(grants_of(), JOB_STATUS, {}, rate_exceeded=False, dispatch_ready=True)

    assert found.allow


def test_job_status_is_a_seam_without_a_door() -> None:
    found = decide_family(grants_of(), JOB_STATUS, {}, rate_exceeded=False)

    assert not found.allow
    assert found.reason == "not_implemented"


def test_the_manifest_offers_both_when_the_door_exists() -> None:
    offered = manifest_actions(grants_of(), frozenset(), dispatch_ready=True)

    assert ENQUEUE in offered
    assert JOB_STATUS in offered


def test_the_manifest_hides_both_without_a_door() -> None:
    """§4 rule 4: the manifest advertises only what `/call` can execute."""
    offered = manifest_actions(grants_of(), frozenset())

    assert ENQUEUE not in offered
    assert JOB_STATUS not in offered


# ---- execution -------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_enqueue_reaches_the_door(tmp_path: Any) -> None:
    door = FakeDispatchDoor()
    gate = make_gate(tmp_path, door)
    grants = gate.lookup(FAMILY_TOKEN)
    assert grants is not None

    reply = await gate.call(grants, ENQUEUE, {"family": WORKER, "message": MESSAGE}, {})

    assert reply.status == 200
    assert door.enqueued[0].target_family == WORKER
    assert door.enqueued[0].caller_family == LEAD
    assert door.enqueued[0].message == MESSAGE


@pytest.mark.asyncio
async def test_the_answer_names_the_session(tmp_path: Any) -> None:
    gate = make_gate(tmp_path, FakeDispatchDoor())
    grants = gate.lookup(FAMILY_TOKEN)
    assert grants is not None

    reply = await gate.call(grants, ENQUEUE, {"family": WORKER, "message": MESSAGE}, {})
    result: Any = reply.payload["result"]

    assert result["session"] == SESSION
    assert result["family"] == WORKER
    assert result["status"] == "queued"
    assert result["created"] is True


@pytest.mark.asyncio
async def test_the_chain_continues_across_an_enqueue(tmp_path: Any) -> None:
    """Contract 04 §6.3. The caller's chain, then the target."""
    door = FakeDispatchDoor()
    gate = make_gate(tmp_path, door)
    grants = gate.lookup(FAMILY_TOKEN)
    assert grants is not None

    await gate.call(grants, ENQUEUE, {"family": WORKER, "message": MESSAGE}, {})

    assert door.enqueued[0].chain == (LEAD, WORKER)
    assert door.enqueued[0].delegation_id != ""


@pytest.mark.asyncio
async def test_a_refused_enqueue_is_a_denial(tmp_path: Any) -> None:
    """Nothing ran, so the audit records a deny and not an allowed call that
    failed (contract 04 §5 row 11 does not apply)."""
    refusal = DispatchRefused("tool_not_granted", "issue-worker declares no enqueue trigger")
    gate = make_gate(tmp_path, FakeDispatchDoor(refuse=refusal))
    grants = gate.lookup(FAMILY_TOKEN)
    assert grants is not None

    reply = await gate.call(grants, ENQUEUE, {"family": WORKER, "message": MESSAGE}, {})

    assert reply.status == 403
    assert reply.payload["reason"] == "tool_not_granted"


@pytest.mark.asyncio
async def test_a_full_target_queue_is_rate_limited(tmp_path: Any) -> None:
    refusal = DispatchRefused("rate_limited", "queue full")
    gate = make_gate(tmp_path, FakeDispatchDoor(refuse=refusal))
    grants = gate.lookup(FAMILY_TOKEN)
    assert grants is not None

    reply = await gate.call(grants, ENQUEUE, {"family": WORKER, "message": MESSAGE}, {})

    assert reply.status == 429


@pytest.mark.asyncio
async def test_job_status_scopes_itself_to_the_caller(tmp_path: Any) -> None:
    """Contract 04 §4.1: no `family` argument, and the scope is fixed."""
    door = FakeDispatchDoor()
    gate = make_gate(tmp_path, door)
    grants = gate.lookup(FAMILY_TOKEN)
    assert grants is not None

    await gate.call(grants, JOB_STATUS, {"session": SESSION, "limit": 5}, {})

    assert door.queried[0].caller_family == LEAD
    assert door.queried[0].session == SESSION
    assert door.queried[0].limit == 5


@pytest.mark.asyncio
async def test_job_status_serves_the_rows_through(tmp_path: Any) -> None:
    door = FakeDispatchDoor()
    door.rows = [{"session": SESSION, "status": "ended", "outcome": {"status": "ok"}}]
    gate = make_gate(tmp_path, door)
    grants = gate.lookup(FAMILY_TOKEN)
    assert grants is not None

    reply = await gate.call(grants, JOB_STATUS, {}, {})
    result: Any = reply.payload["result"]

    assert result["jobs"] == door.rows


@pytest.mark.asyncio
async def test_an_audited_enqueue_records_the_chain(tmp_path: Any) -> None:
    """Invariant 15: every decision is recorded, and §6.3's chain with it."""
    gate = make_gate(tmp_path, FakeDispatchDoor())
    grants = gate.lookup(FAMILY_TOKEN)
    assert grants is not None

    await gate.call(grants, ENQUEUE, {"family": WORKER, "message": MESSAGE}, {})

    written = list((tmp_path / "audit").glob("*.jsonl"))
    assert written
    assert ENQUEUE in written[0].read_text(encoding="utf-8")
