"""The upkeep task: the periodic sync and the gate poll (contract 02 §8.3).

One write that fails must not end the task. A task that ended would stop the
sync and the gate poll until the process restarts, and nothing would say so.

The last tests here are about each other task the service starts. A task
that ends with an error says so in the log.
"""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
from pathlib import Path
from typing import Any

import pytest
from attendance.atomic import write_json
from attendance.auth import Principal
from attendance.channel import Channel, SandboxDial
from attendance.clock import now, rfc3339
from attendance.faults import FaultCode
from attendance.journal import Journal
from attendance.models import LineKind
from attendance.paths import audit_file, fault_file
from attendance.requests import CreateRequest, RunTurnRequest
from attendance.service import SessionService
from attendance.states import SessionState
from attendance.tasks import report_failure
from attendance_harness import (
    CHAT_SESSION,
    FAMILY,
    SANDBOX,
    FakeFleet,
    make_config,
    wait_until,
    write_status,
)

OWUI = Principal.DOOR_OWUI
DOOR = "owui-1"
PROMPT = "Turn the boiler on."
OTHER_SESSION = "owui-11111111-2222-4333-8444-555555555555"

#: The upkeep interval of these tests. The real one is a second.
TICK_S = 0.01

#: How many ticks prove that the task is still alive.
TICKS_WANTED = 3


class Rig:
    """One service, one fake playpen and the audit file of the PEP."""

    def __init__(self, service: SessionService, fleet: FakeFleet, state_root: Path) -> None:
        self.service = service
        self.fleet = fleet
        self.state_root = state_root
        self.audit = audit_file(state_root, now().strftime("%Y-%m-%d"))

    async def start(self, session: str = CHAT_SESSION) -> str:
        self.service.create_or_find(OWUI, CreateRequest(family=FAMILY, session=session))
        live = await self.service.run_turn(
            OWUI, FAMILY, session, RunTurnRequest(prompt=PROMPT), DOOR
        )
        await self.fleet.playpen().next_start()
        return live.record.turn

    def open_gate(self, session: str, turn: str) -> None:
        """One `pending` audit record (contract 04 §6.4)."""
        record: dict[str, Any] = {
            "ts": rfc3339(now()),
            "family": FAMILY,
            "sandbox_id": SANDBOX,
            "tool": "ha_call",
            "decision": "pending",
            "reason": "pending",
            "waited_ms": 0,
            "gate": f"gate-{turn}",
            "claimed": {"session_id": session, "turn_id": turn, "delegation_id": None},
        }
        self.audit.parent.mkdir(parents=True, exist_ok=True)

        with self.audit.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    def state(self, session: str = CHAT_SESSION) -> SessionState:
        return SessionState(self.service.get_session(OWUI, FAMILY, session, 0)["state"])

    def fault_codes(self) -> list[str]:
        path = fault_file(self.state_root, FAMILY)

        if not path.is_file():
            return []

        payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return [str(entry["code"]) for entry in payload["faults"]]

    async def stop(self) -> None:
        await self.service.close()
        await self.fleet.stop()


async def build(tmp_path: Path) -> Rig:
    config = make_config(tmp_path)
    write_status(config.state_root)
    fleet = FakeFleet()
    service = SessionService(config, factory=fleet.factory, cold_start_wait_s=1.0)
    service.start()
    return Rig(service, fleet, config.state_root)


def _sync_fails() -> OSError:
    return OSError(errno.EIO, "the sync failed")


async def test_upkeep_goes_on_after_a_failed_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Contract 02 §8.3. The sync runs for the life of the process."""
    monkeypatch.setattr("attendance.service.FLUSH_INTERVAL_S", TICK_S)
    rig = await build(tmp_path)
    calls = 0

    def failing() -> None:
        nonlocal calls
        calls += 1
        raise _sync_fails()

    monkeypatch.setattr(rig.service.store.journal, "flush_due", failing)

    with caplog.at_level(logging.ERROR, logger="attendance"):
        rig.service.start_upkeep()
        await wait_until(lambda: calls >= TICKS_WANTED)

    assert "the journal sync" in caplog.text
    await rig.stop()


async def test_upkeep_reads_gates_after_a_failed_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sync that fails on each tick must not hide a turn that waits."""
    monkeypatch.setattr("attendance.service.FLUSH_INTERVAL_S", TICK_S)
    rig = await build(tmp_path)
    turn = await rig.start()
    rig.open_gate(CHAT_SESSION, turn)

    def failing() -> None:
        raise _sync_fails()

    monkeypatch.setattr(rig.service.store.journal, "flush_due", failing)
    rig.service.start_upkeep()
    await wait_until(lambda: rig.state() is SessionState.WAITING_APPROVAL)

    await rig.stop()


async def test_upkeep_tries_the_queues_after_a_failed_try(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Contract 02 §13 rule 3. A try of the queues that fails does not end
    the task, and the next tick tries again."""
    monkeypatch.setattr("attendance.service.FLUSH_INTERVAL_S", TICK_S)
    rig = await build(tmp_path)
    calls = 0

    def failing() -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("the try failed")

    monkeypatch.setattr(rig.service, "pump_queues", failing)

    with caplog.at_level(logging.ERROR, logger="attendance"):
        rig.service.start_upkeep()
        await wait_until(lambda: calls >= TICKS_WANTED)

    assert "the queue pump" in caplog.text
    await rig.stop()


async def test_a_gate_that_fails_does_not_drop_the_next(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """One poll reads each record once. A record after a failed one still counts."""
    rig = await build(tmp_path)
    first = await rig.start()
    second = await rig.start(OTHER_SESSION)
    rig.open_gate(CHAT_SESSION, first)
    rig.open_gate(OTHER_SESSION, second)
    save_turn = rig.service.store.save_turn
    failed: list[str] = []

    def failing_once(record: Any) -> None:
        if not failed:
            failed.append(record.turn)
            raise OSError(errno.ENOSPC, "no space left")

        save_turn(record)

    monkeypatch.setattr(rig.service.store, "save_turn", failing_once)

    with caplog.at_level(logging.ERROR, logger="attendance"):
        rig.service.read_gates()

    assert failed == [first]
    assert rig.state(OTHER_SESSION) is SessionState.WAITING_APPROVAL
    assert LineKind.APPROVAL_REQUESTED in [
        line.kind for line in rig.service.store.journal.replay(FAMILY, OTHER_SESSION)
    ]
    assert "gate" in caplog.text
    await rig.stop()


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 0000 directory anyway")
async def test_an_audit_fault_that_was_not_written_is_written_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contract 05 §3.3.1. A fault that reached no file is not a published fault."""
    rig = await build(tmp_path)
    await rig.start()
    rig.audit.parent.mkdir(parents=True, exist_ok=True)
    rig.audit.parent.chmod(0o000)
    raise_fault = rig.service.faults.raise_fault
    failed: list[FaultCode] = []

    def failing_once(family: str, code: FaultCode, *rest: Any) -> None:
        if not failed:
            failed.append(code)
            raise OSError(errno.ENOSPC, "no space left")

        raise_fault(family, code, *rest)

    monkeypatch.setattr(rig.service.faults, "raise_fault", failing_once)

    try:
        with pytest.raises(OSError, match="no space left"):
            rig.service.read_gates()

        rig.service.read_gates()

        assert rig.fault_codes() == [FaultCode.AUDIT_UNREADABLE.value]
    finally:
        rig.audit.parent.chmod(0o700)

    await rig.stop()


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 0000 directory anyway")
async def test_an_audit_fault_that_was_not_cleared_is_cleared_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other direction. A clear that reached no file is not a published clear."""
    rig = await build(tmp_path)
    await rig.start()
    rig.audit.parent.mkdir(parents=True, exist_ok=True)
    rig.audit.parent.chmod(0o000)

    try:
        rig.service.read_gates()
    finally:
        rig.audit.parent.chmod(0o700)

    assert rig.fault_codes() == [FaultCode.AUDIT_UNREADABLE.value]

    failed: list[Path] = []

    def failing_once(path: Path, *rest: Any) -> None:
        if not failed:
            failed.append(path)
            raise OSError(errno.ENOSPC, "no space left")

        write_json(path, *rest)

    monkeypatch.setattr("attendance.faults.write_json", failing_once)

    with pytest.raises(OSError, match="no space left"):
        rig.service.read_gates()

    rig.service.read_gates()

    assert rig.fault_codes() == []
    await rig.stop()


def test_one_journal_that_cannot_sync_does_not_stop_the_next(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contract 02 §8.3 holds for each session. The failure is still raised."""
    journal = Journal(tmp_path, fsync_interval_s=0.0)
    journal.append(FAMILY, CHAT_SESSION, LineKind.NOTE, None, {"note": "first"})
    journal.append(FAMILY, OTHER_SESSION, LineKind.NOTE, None, {"note": "second"})
    synced: list[int] = []

    def sync(fd: int) -> None:
        if not synced:
            synced.append(fd)
            raise _sync_fails()

        synced.append(fd)

    monkeypatch.setattr(os, "fsync", sync)

    with pytest.raises(OSError, match="the sync failed"):
        journal.flush_due()

    assert len(synced) == 2
    monkeypatch.undo()
    journal.close_all()


async def _fails() -> None:
    raise RuntimeError("the work failed")


async def test_a_task_that_fails_says_so(caplog: pytest.LogCaptureFixture) -> None:
    """The event loop keeps the error of a task that nothing awaits."""
    task = asyncio.create_task(_fails(), name="the work")
    task.add_done_callback(report_failure)

    with caplog.at_level(logging.ERROR, logger="attendance"):
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

    assert "task the work ended with an error" in caplog.text
    assert "the work failed" in caplog.text


async def test_a_cancelled_task_is_not_a_failure(caplog: pytest.LogCaptureFixture) -> None:
    """`close()` cancels each task that the service owns."""
    task = asyncio.create_task(asyncio.sleep(60), name="the work")
    task.add_done_callback(report_failure)
    task.cancel()

    with caplog.at_level(logging.ERROR, logger="attendance"):
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

    assert caplog.text == ""


async def test_a_pre_start_that_raises_says_so(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Contract 03 §4.7 rule 11. A pre-start costs the caller nothing, and its
    fault still reaches the log."""
    config = make_config(tmp_path)
    write_status(config.state_root)

    def broken(dial: SandboxDial) -> Channel:
        raise RuntimeError("the factory failed")

    service = SessionService(config, factory=broken, cold_start_wait_s=1.0)
    service.start()

    with caplog.at_level(logging.ERROR, logger="attendance"):
        service.create_or_find(OWUI, CreateRequest(family=FAMILY, session=CHAT_SESSION))
        await wait_until(lambda: "the factory failed" in caplog.text)

    assert f"task pre-start {FAMILY}/{CHAT_SESSION} ended with an error" in caplog.text
    await service.close()
