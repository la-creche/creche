"""A turn whose start raises (contract 02 §4.3, §14).

A turn is in the book before the channel hears of it. An error between the
two must end the turn. A turn left `running` with no deadline watcher holds
its session until the process restarts.
"""

from __future__ import annotations

import asyncio
import errno
import logging
from pathlib import Path

import pytest
from attendance.auth import Principal
from attendance.channel import Channel, SandboxDial
from attendance.errors import ApiError, TurnReason
from attendance.models import LineKind, Turn
from attendance.requests import CreateRequest, RunTurnRequest, Wait
from attendance.service import SessionService
from attendance.states import TurnState
from attendance_harness import (
    CHAT_SESSION,
    FAMILY,
    SANDBOX,
    FakeFleet,
    PlaypenPlan,
    make_config,
    settle_now,
    wait_until,
    write_status,
)

OWUI = Principal.DOOR_OWUI
DOOR = "owui-1"
PROMPT = "Which sensor dropped out last night?"
START_ERROR = "attendance could not start this turn"


class Flaky:
    """A channel factory that raises until a test mends it.

    The error is of a type that no handler of the start names. It stands for
    each fault that the start does not expect.
    """

    def __init__(self) -> None:
        self.fleet = FakeFleet()
        self.broken = True

    def factory(self, dial: SandboxDial) -> Channel:
        if self.broken:
            raise RuntimeError("the factory failed")

        return self.fleet.factory(dial)


def build(tmp_path: Path, flaky: Flaky) -> SessionService:
    config = make_config(tmp_path)
    write_status(config.state_root)
    service = SessionService(config, factory=flaky.factory, cold_start_wait_s=1.0)
    service.start()
    service.create_or_find(OWUI, CreateRequest(family=FAMILY, session=CHAT_SESSION))
    return service


async def test_a_start_that_raises_fails_the_turn(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Contract 02 §14: `internal` is the reason of a turn that a defect ended."""
    service = build(tmp_path, Flaky())

    with caplog.at_level(logging.ERROR, logger="attendance"):
        live = await service.run_turn(
            OWUI, FAMILY, CHAT_SESSION, RunTurnRequest(prompt=PROMPT), DOOR
        )

    last = list(service.store.journal.replay(FAMILY, CHAT_SESSION))[-1]

    assert live.record.state is TurnState.FAILED
    assert live.record.reason is TurnReason.INTERNAL
    assert live.done.is_set()
    assert live.deadline_task is None
    assert last.kind is LineKind.TURN_FAILED
    assert last.body == {"reason": "internal", "message": START_ERROR}
    assert "the factory failed" in caplog.text
    await service.close()


async def test_an_accepted_start_that_raises_fails_the_turn(tmp_path: Path) -> None:
    """Contract 02 §5.4. Nothing awaits the start of an `accepted` turn."""
    service = build(tmp_path, Flaky())

    live = await service.run_turn(
        OWUI, FAMILY, CHAT_SESSION, RunTurnRequest(prompt=PROMPT, wait=Wait.ACCEPTED), DOOR
    )
    await settle_now(live.done)

    assert live.record.state is TurnState.FAILED
    assert live.record.reason is TurnReason.INTERNAL
    await service.close()


async def test_the_session_takes_a_turn_after_a_start_that_raised(tmp_path: Path) -> None:
    flaky = Flaky()
    service = build(tmp_path, flaky)
    await service.run_turn(OWUI, FAMILY, CHAT_SESSION, RunTurnRequest(prompt=PROMPT), DOOR)
    flaky.broken = False

    live = await service.run_turn(OWUI, FAMILY, CHAT_SESSION, RunTurnRequest(prompt=PROMPT), DOOR)

    assert live.record.state is TurnState.RUNNING
    assert live.deadline_task is not None
    await service.close()
    await flaky.fleet.stop()


async def test_a_request_that_ends_in_the_dial_does_not_end_the_start(tmp_path: Path) -> None:
    """A client that disconnects changes nothing. The server cancels the
    request while the service dials. The start is work of the service, so
    the turn still gets its `start_turn` and its deadline watcher."""
    flaky = Flaky()
    flaky.broken = False
    service = build(tmp_path, flaky)
    cold = asyncio.Event()
    flaky.fleet.plan(SANDBOX, PlaypenPlan(ready_gate=cold))
    request = asyncio.create_task(
        service.run_turn(OWUI, FAMILY, CHAT_SESSION, RunTurnRequest(prompt=PROMPT), DOOR)
    )
    await wait_until(lambda: SANDBOX in flaky.fleet.playpens)
    request.cancel()

    with pytest.raises(asyncio.CancelledError):
        await request

    cold.set()
    sent = await flaky.fleet.playpen().next_start()
    live = service.live_turn(FAMILY, CHAT_SESSION, sent["turn"])

    assert live is not None
    await wait_until(lambda: live.deadline_task is not None)
    assert live.record.state is TurnState.RUNNING
    await service.close()
    await flaky.fleet.stop()


async def test_a_turn_that_cannot_be_written_is_not_left_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The caller gets the error. The turn must not hold the session."""
    flaky = Flaky()
    flaky.broken = False
    service = build(tmp_path, flaky)
    save_turn = service.store.save_turn
    failed: list[str] = []

    def failing_once(record: Turn) -> None:
        if not failed:
            failed.append(record.turn)
            raise OSError(errno.ENOSPC, "no space left")

        save_turn(record)

    monkeypatch.setattr(service.store, "save_turn", failing_once)

    with pytest.raises(OSError, match="no space left"):
        await service.run_turn(OWUI, FAMILY, CHAT_SESSION, RunTurnRequest(prompt=PROMPT), DOOR)

    live = service.live_turn(FAMILY, CHAT_SESSION, failed[0])

    assert live is not None
    assert live.record.state is TurnState.FAILED
    assert live.record.reason is TurnReason.INTERNAL

    try:
        again = await service.run_turn(
            OWUI, FAMILY, CHAT_SESSION, RunTurnRequest(prompt=PROMPT), DOOR
        )
    except ApiError as refused:
        pytest.fail(f"the session stayed busy: {refused.code.value}")

    assert again.record.state is TurnState.RUNNING
    await service.close()
    await flaky.fleet.stop()
