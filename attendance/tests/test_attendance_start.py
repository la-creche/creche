"""The drop of each job row at the start of attendance.

Contract 02 §6 rule 5 and decision CH83 rule 5: no job outlives a restart,
so a row whose origin is `delegate` or `auto` drops before the first
request. A `door` row stays.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from attendance.ids import new_session_id, new_ulid
from attendance.service import SessionService
from attendance.vmm import UNREACHABLE
from attendance_harness import FAMILY, make_config, make_service, write_status
from creche_wire import (
    Auto,
    AutoJobId,
    AutoOrigin,
    CreateSession,
    Delegate,
    DelegateOrigin,
    DelegationId,
    Door,
    DoorOrigin,
    FamilyName,
    JobId,
    SessionCwd,
    SessionId,
    SessionOrigin,
    Timer,
    TimerTrigger,
    Trigger,
    UnixMillis,
)
from fake_vmm import PAGE_ROWS, FakeVmm

THIN = "home-assistant-worker"
CWD = "/srv/agents/sessions/chat"
CREATED_MS = 1_794_153_600_000


def door() -> SessionOrigin:
    return SessionOrigin(DoorOrigin(door=Door()))


def delegate() -> SessionOrigin:
    job = Delegate(delegation=DelegationId(new_ulid()), job=JobId(f"job-{new_ulid()}"))
    return SessionOrigin(DelegateOrigin(delegate=job))


def auto() -> SessionOrigin:
    trigger = Trigger(TimerTrigger(timer=Timer()))
    return SessionOrigin(
        AutoOrigin(auto=Auto(job=AutoJobId(f"auto-{new_ulid()}"), trigger=trigger))
    )


async def add_row(vmm: FakeVmm, family: str, origin: SessionOrigin) -> str:
    session = new_session_id()
    await vmm.create(
        CreateSession(
            created_at=UnixMillis(CREATED_MS),
            cwd=SessionCwd(CWD),
            family=FamilyName(family),
            id=SessionId(session),
            origin=origin,
        )
    )
    return session


def service_of(tmp_path: Path, vmm: FakeVmm) -> SessionService:
    """A service over `vmm`, with an attended and a thin family."""
    config = make_config(tmp_path)
    write_status(config.state_root)
    write_status(config.state_root, family=THIN, kind="thin")
    return make_service(config, vmm)


async def test_each_job_row_drops_and_each_door_row_stays(tmp_path: Path) -> None:
    """More rows than one page, so the sweep reads each page first."""
    vmm = FakeVmm()
    kept = [await add_row(vmm, FAMILY, door()), await add_row(vmm, FAMILY, door())]
    jobs = [
        await add_row(vmm, FAMILY, auto()),
        await add_row(vmm, THIN, delegate()),
        await add_row(vmm, THIN, delegate()),
        await add_row(vmm, THIN, delegate()),
    ]
    assert len(jobs) > PAGE_ROWS
    service = service_of(tmp_path, vmm)

    await service.drop_job_rows()

    assert sorted(vmm.rows) == sorted(kept)
    assert sorted(session for _, session in vmm.drops) == sorted(jobs)
    assert all(query.cwd is None for query in vmm.lists)
    assert vmm.opened == []


@pytest.mark.parametrize("route", ["list", "drop"])
async def test_a_vmm_refusal_leaves_the_rows(tmp_path: Path, route: str) -> None:
    """The CONTRACT-QUESTION of `drop_job_rows`: the log names the family,
    and the next start tries again."""
    vmm = FakeVmm()
    job = await add_row(vmm, THIN, delegate())
    vmm.refuse[route] = UNREACHABLE
    service = service_of(tmp_path, vmm)

    await service.drop_job_rows()

    assert list(vmm.rows) == [job]
