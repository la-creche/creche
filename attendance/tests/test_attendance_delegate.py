"""The delegate door: one thin job, one turn, nothing left behind.

Contract 02 §12 and §12.1, contract 04 §7.3. The PEP is the one client.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from attendance.auth import Principal
from attendance.errors import ApiError, ErrorCode
from attendance.ids import is_ulid, new_ulid
from attendance.jobs import DEFAULT_JOB_TIMEOUT_S, MESSAGE_MAX_BYTES, JobStatus
from attendance.paths import session_dir
from attendance.requests import CreateRequest, DelegateRequest, read_delegate
from attendance.workspace import CODE_SANDBOX_FAMILY, WORKSPACE_KIND, owner_dir
from attendance_harness import (
    CHAT_SESSION,
    FAMILY,
    FakeFleet,
    make_config,
    wait_until,
    write_status,
)

from attendance.service import SessionService  # isort: skip

DELEGATE = Principal.DOOR_DELEGATE
OWUI = Principal.DOOR_OWUI
DOOR = "chaperone-1"

ORACLE = "vault-oracle"
ORACLE_SANDBOX = "vault-oracle-s1"
CODE_SANDBOX = "code-sandbox-s1"

MESSAGE = "Which note holds the boiler service date?"
ANSWER = "The boiler was serviced on 2026-03-11, per house/boiler.md."


class DelegateHarness:
    """One service with a thin family beside the attended one."""

    def __init__(self, tmp_path: Path, job_timeout_s: int | None = DEFAULT_JOB_TIMEOUT_S) -> None:
        self.config = make_config(tmp_path)
        write_status(self.config.state_root)
        write_status(
            self.config.state_root,
            family=ORACLE,
            kind="thin",
            sandboxes=((ORACLE_SANDBOX, "ready"),),
            job_timeout_s=job_timeout_s,
        )
        write_status(
            self.config.state_root,
            family=CODE_SANDBOX_FAMILY,
            kind="thin",
            sandboxes=((CODE_SANDBOX, "ready"),),
            job_timeout_s=job_timeout_s,
        )
        self.fleet = FakeFleet()
        self.service = SessionService(
            self.config, factory=self.fleet.factory, cold_start_wait_s=1.0
        )
        self.service.start()

    def call(
        self,
        target: str = ORACLE,
        owner: str | None = CHAT_SESSION,
        message: str = MESSAGE,
        delegation_id: str | None = None,
    ) -> DelegateRequest:
        return DelegateRequest(
            caller_family=FAMILY,
            target_family=target,
            delegation_id=delegation_id if delegation_id is not None else new_ulid(),
            claimed_session_id=owner,
            message=message,
        )

    async def delegate(
        self,
        request: DelegateRequest,
        sandbox: str = ORACLE_SANDBOX,
        answer: str = ANSWER,
    ) -> dict[str, object]:
        """Run one job to settled, playing the playpen's side."""
        task = asyncio.create_task(self.service.run_delegate(DELEGATE, request, DOOR))
        started = await self.next_start(sandbox)
        playpen = self.fleet.playpen(sandbox)
        await playpen.play_turn(str(started["session"]), str(started["turn"]), answer)
        await playpen.settle(str(started["session"]), str(started["turn"]))
        return await asyncio.wait_for(task, 2.0)

    async def next_start(self, sandbox: str = ORACLE_SANDBOX) -> dict[str, object]:
        await wait_until(lambda: sandbox in self.fleet.playpens)
        return await self.fleet.playpen(sandbox).next_start()

    def sessions_of(self, family: str) -> list[str]:
        return self.service.store.session_ids(family)

    async def stop(self) -> None:
        await self.service.close()
        await self.fleet.stop()


async def test_a_delegate_call_answers_and_deletes_the_session(tmp_path: Path) -> None:
    """Contract 04 §7.3 and contract 02 §12 rules 5 and 6."""
    harness = DelegateHarness(tmp_path)
    body = await harness.delegate(harness.call())

    assert body["status"] == JobStatus.OK.value
    assert body["content"] == ANSWER
    assert body["error"] is None
    assert str(body["session_id"]).startswith("job-")
    assert is_ulid(str(body["session_id"])[len("job-") :])

    usage = body["usage"]
    assert isinstance(usage, dict)
    assert usage["cost_usd"] == pytest.approx(0.014)

    # §12 rule 5: the answer travels in the response and the session goes.
    assert harness.sessions_of(ORACLE) == []
    await harness.stop()


async def test_the_job_directory_is_gone_from_disk(tmp_path: Path) -> None:
    """§12 rule 6: no journal, no transcript, no scratch."""
    harness = DelegateHarness(tmp_path)
    body = await harness.delegate(harness.call())
    job = str(body["session_id"])

    assert not session_dir(harness.config.sessions_root, ORACLE, job).exists()
    await harness.stop()


async def test_a_delegate_timeout_answers_timeout(tmp_path: Path) -> None:
    """§12 rule 4: past the deadline the turn aborts and the door says so."""
    harness = DelegateHarness(tmp_path, job_timeout_s=1)
    request = harness.call()
    task = asyncio.create_task(harness.service.run_delegate(DELEGATE, request, DOOR))
    started = await harness.next_start()

    assert started["deadline_s"] == 1

    body = await asyncio.wait_for(task, 5.0)

    assert body["status"] == JobStatus.TIMEOUT.value
    assert body["content"] is None
    assert body["error"] is not None
    assert harness.sessions_of(ORACLE) == []
    await harness.stop()


async def test_a_failed_turn_answers_failed(tmp_path: Path) -> None:
    harness = DelegateHarness(tmp_path)
    request = harness.call()
    task = asyncio.create_task(harness.service.run_delegate(DELEGATE, request, DOOR))
    started = await harness.next_start()
    await harness.fleet.playpen(ORACLE_SANDBOX).fail(
        str(started["session"]), str(started["turn"]), "pi_rejected_prompt", "the model refused"
    )
    body = await asyncio.wait_for(task, 2.0)

    assert body["status"] == JobStatus.FAILED.value
    assert body["content"] is None
    assert "model_error" in str(body["error"])
    assert harness.sessions_of(ORACLE) == []
    await harness.stop()


async def test_a_non_thin_family_is_refused(tmp_path: Path) -> None:
    """Contract 02 §3.1's kind column: only a thin family takes a delegate call."""
    harness = DelegateHarness(tmp_path)

    with pytest.raises(ApiError) as refused:
        await harness.service.run_delegate(DELEGATE, harness.call(target=FAMILY), DOOR)

    assert refused.value.code is ErrorCode.FORBIDDEN
    assert harness.sessions_of(FAMILY) == []
    await harness.stop()


async def test_a_family_of_no_known_kind_takes_no_delegate_call(tmp_path: Path) -> None:
    """Contract 02 §3.1. A status document that states no known kind proves no
    thin family. The refusal comes before the work directory exists."""
    harness = DelegateHarness(tmp_path)
    write_status(
        harness.config.state_root,
        family=CODE_SANDBOX_FAMILY,
        kind="robot",
        sandboxes=((CODE_SANDBOX, "ready"),),
    )

    with pytest.raises(ApiError) as refused:
        await harness.service.run_delegate(DELEGATE, harness.call(target=CODE_SANDBOX_FAMILY), DOOR)

    assert refused.value.code is ErrorCode.FORBIDDEN
    assert refused.value.detail == {"kind": None}
    assert harness.sessions_of(CODE_SANDBOX_FAMILY) == []
    assert not owner_dir(harness.config.work_root, CHAT_SESSION).exists()
    await harness.stop()


async def test_only_the_delegate_door_may_call_it(tmp_path: Path) -> None:
    """Contract 02 §3.1. The PEP is the one client of this path."""
    harness = DelegateHarness(tmp_path)

    with pytest.raises(ApiError) as refused:
        await harness.service.run_delegate(OWUI, harness.call(), DOOR)

    assert refused.value.code is ErrorCode.FORBIDDEN
    await harness.stop()


@pytest.mark.parametrize("owner", ["../../etc", "a/b", ".."])
async def test_a_hostile_calling_session_id_is_refused(tmp_path: Path, owner: str) -> None:
    """It becomes a path component, so it is checked before anything runs."""
    harness = DelegateHarness(tmp_path)

    with pytest.raises(ApiError) as refused:
        await harness.service.run_delegate(
            DELEGATE, harness.call(target=CODE_SANDBOX_FAMILY, owner=owner), DOOR
        )

    assert refused.value.code is ErrorCode.BAD_REQUEST
    assert harness.sessions_of(CODE_SANDBOX_FAMILY) == []
    assert not (harness.config.work_root / CODE_SANDBOX_FAMILY).exists()
    await harness.stop()


async def test_code_sandbox_works_in_the_calling_chats_directory(tmp_path: Path) -> None:
    """Contract 02 §12.1 rules 2 and 3, contract 03 §7.2."""
    harness = DelegateHarness(tmp_path)
    task = asyncio.create_task(
        harness.service.run_delegate(DELEGATE, harness.call(target=CODE_SANDBOX_FAMILY), DOOR)
    )
    started = await harness.next_start(CODE_SANDBOX)

    assert started["workspace"] == {"kind": WORKSPACE_KIND, "owner_session": CHAT_SESSION}

    playpen = harness.fleet.playpen(CODE_SANDBOX)
    await playpen.settle(str(started["session"]), str(started["turn"]))
    await asyncio.wait_for(task, 2.0)

    assert owner_dir(harness.config.work_root, CHAT_SESSION).is_dir()
    await harness.stop()


async def test_a_thin_family_without_a_work_root_carries_no_workspace(tmp_path: Path) -> None:
    """Only `code-sandbox` works in the caller's directory (`docs/rework/spec.md` §7.2)."""
    harness = DelegateHarness(tmp_path)
    task = asyncio.create_task(harness.service.run_delegate(DELEGATE, harness.call(), DOOR))
    started = await harness.next_start()

    assert "workspace" not in started

    playpen = harness.fleet.playpen(ORACLE_SANDBOX)
    await playpen.settle(str(started["session"]), str(started["turn"]))
    await asyncio.wait_for(task, 2.0)

    assert not (harness.config.work_root / CODE_SANDBOX_FAMILY).exists()
    await harness.stop()


async def test_the_chat_directory_outlives_the_job(tmp_path: Path) -> None:
    """§12.1 rule 4. The directory belongs to the chat, not to the job."""
    harness = DelegateHarness(tmp_path)
    await harness.delegate(harness.call(target=CODE_SANDBOX_FAMILY), sandbox=CODE_SANDBOX)

    made = owner_dir(harness.config.work_root, CHAT_SESSION)
    (made / "clean.py").write_text("import pandas", encoding="utf-8")

    await harness.delegate(harness.call(target=CODE_SANDBOX_FAMILY), sandbox=CODE_SANDBOX)

    assert harness.sessions_of(CODE_SANDBOX_FAMILY) == []
    assert (made / "clean.py").read_text(encoding="utf-8") == "import pandas"
    await harness.stop()


async def test_deleting_the_chat_session_removes_the_directory(tmp_path: Path) -> None:
    """§12.1 rule 4 and contract 02 §5.8's table, last row."""
    harness = DelegateHarness(tmp_path)
    harness.service.create_or_find(OWUI, CreateRequest(family=FAMILY, session=CHAT_SESSION))
    await harness.delegate(harness.call(target=CODE_SANDBOX_FAMILY), sandbox=CODE_SANDBOX)

    made = owner_dir(harness.config.work_root, CHAT_SESSION)
    assert made.is_dir()

    await harness.service.delete_session(OWUI, FAMILY, CHAT_SESSION)

    assert not made.exists()
    await harness.stop()


async def test_the_delegation_reaches_the_channel(tmp_path: Path) -> None:
    """Contract 03 §7.4 rule 4. Two fields, and the playpen takes no other.

    `caller_family` was a third. The playpen's reader keeps `id` and
    `caller_session` and discards the rest, and no header carries a family,
    so it was weight on a per-turn message and nothing more.
    """
    harness = DelegateHarness(tmp_path)
    delegation_id = new_ulid()
    task = asyncio.create_task(
        harness.service.run_delegate(DELEGATE, harness.call(delegation_id=delegation_id), DOOR)
    )
    started = await harness.next_start()

    assert started["delegation"] == {"id": delegation_id, "caller_session": CHAT_SESSION}

    playpen = harness.fleet.playpen(ORACLE_SANDBOX)
    await playpen.settle(str(started["session"]), str(started["turn"]))
    await asyncio.wait_for(task, 2.0)
    await harness.stop()


async def test_a_call_with_no_caller_session_still_sends_the_delegation_id(
    tmp_path: Path,
) -> None:
    """Contract 03 §7.4 rule 4 item 5.

    Contract 04 §7.3 makes `claimed_session_id` advisory, so the PEP may send
    none. That is a delegation with no caller, not a malformed one: the id
    still reaches the playpen and the PEP's own chain (contract 04 §6.3)
    still runs through this turn.
    """
    harness = DelegateHarness(tmp_path)
    delegation_id = new_ulid()
    task = asyncio.create_task(
        harness.service.run_delegate(
            DELEGATE, harness.call(owner=None, delegation_id=delegation_id), DOOR
        )
    )
    started = await harness.next_start()

    assert started["delegation"] == {"id": delegation_id, "caller_session": None}

    playpen = harness.fleet.playpen(ORACLE_SANDBOX)
    await playpen.settle(str(started["session"]), str(started["turn"]))
    await asyncio.wait_for(task, 2.0)
    await harness.stop()


async def test_two_jobs_share_the_thin_familys_sandbox(tmp_path: Path) -> None:
    """`docs/rework/spec.md` §7.1 and §7.4. No limit and no queue for a thin family."""
    harness = DelegateHarness(tmp_path)
    first = asyncio.create_task(harness.service.run_delegate(DELEGATE, harness.call(), DOOR))
    started_first = await harness.next_start()
    second = asyncio.create_task(harness.service.run_delegate(DELEGATE, harness.call(), DOOR))
    started_second = await harness.next_start()

    assert started_first["session"] != started_second["session"]

    playpen = harness.fleet.playpen(ORACLE_SANDBOX)

    for started in (started_first, started_second):
        await playpen.settle(str(started["session"]), str(started["turn"]))

    bodies = await asyncio.wait_for(asyncio.gather(first, second), 2.0)

    assert [body["status"] for body in bodies] == [JobStatus.OK.value] * 2
    assert harness.fleet.dials == [ORACLE_SANDBOX]
    await harness.stop()


def _body(**over: object) -> dict[str, object]:
    raw: dict[str, object] = {
        "caller_family": FAMILY,
        "target_family": ORACLE,
        "delegation_id": new_ulid(),
        "claimed_session_id": CHAT_SESSION,
        "message": MESSAGE,
    }
    raw.update(over)
    return raw


def test_a_good_delegate_body_parses() -> None:
    parsed = read_delegate(_body())

    assert parsed.target_family == ORACLE
    assert parsed.claimed_session_id == CHAT_SESSION
    assert parsed.message == MESSAGE


def test_an_absent_calling_session_id_is_allowed() -> None:
    """Contract 04 §3.1 rule 4: the PEP drops a malformed header and sends none."""
    raw = _body()
    del raw["claimed_session_id"]

    assert read_delegate(raw).claimed_session_id is None


@pytest.mark.parametrize(
    "over",
    [
        {"delegation_id": "not-a-ulid"},
        {"delegation_id": "01jbq7wz0x4t9v6k2h8m3n5pqr"},
        {"caller_family": "Chat"},
        {"target_family": "vault_oracle"},
        {"claimed_session_id": "../../etc"},
        {"message": ""},
    ],
)
def test_a_bad_delegate_body_is_refused(over: dict[str, object]) -> None:
    with pytest.raises(ApiError) as refused:
        read_delegate(_body(**over))

    assert refused.value.code is ErrorCode.BAD_REQUEST


def test_a_message_over_the_cap_is_refused() -> None:
    """Contract 04 §7.1: 1 to 64 KiB."""
    with pytest.raises(ApiError) as refused:
        read_delegate(_body(message="x" * (MESSAGE_MAX_BYTES + 1)))

    assert refused.value.code is ErrorCode.PAYLOAD_TOO_LARGE


async def test_the_job_timeout_falls_back_to_the_contract_default(tmp_path: Path) -> None:
    """Contract 02 §12 rule 3. 120 seconds when the document names none."""
    harness = DelegateHarness(tmp_path, job_timeout_s=None)
    task = asyncio.create_task(harness.service.run_delegate(DELEGATE, harness.call(), DOOR))
    started = await harness.next_start()

    assert started["deadline_s"] == DEFAULT_JOB_TIMEOUT_S

    playpen = harness.fleet.playpen(ORACLE_SANDBOX)
    await playpen.settle(str(started["session"]), str(started["turn"]))
    await asyncio.wait_for(task, 2.0)
    await harness.stop()
