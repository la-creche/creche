"""The switch-sandbox handshake (contract 05 §5).

`managerd` makes this one call. Every session survives it, the outgoing
sandbox is drained or interrupted, and the old channel is shut down cleanly
before this service reports the sandbox free.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from attendance.auth import Principal
from attendance.errors import ApiError, ErrorCode, TurnReason
from attendance.models import LineKind
from attendance.requests import CreateRequest, RunTurnRequest, SwitchMode, SwitchRequest
from attendance.service import SessionService
from attendance.states import TurnState
from attendance.switching import SwitchOutcome
from attendance.turns import LiveTurn
from attendance_harness import (
    FAMILY,
    SANDBOX,
    FakeFleet,
    PlaypenPlan,
    make_config,
    settle_now,
    wait_until,
    write_status,
)

MANAGERD = Principal.MANAGERD
OWUI = Principal.DOOR_OWUI
DOOR = "owui-1"

NEXT_SANDBOX = "chat-s2"
FIRST = "owui-chat-one"
SECOND = "owui-chat-two"
REASON = "mounts changed: added /srv/agents/work/code-sandbox rw"
ANSWER = "Sensor kitchen_temp stopped reporting at 02:14."
NOT_YET_S = 0.05


class SwitchHarness:
    """One family, two sandboxes, and a fake playpen in each."""

    def __init__(self, tmp_path: Path) -> None:
        self.config = make_config(tmp_path)
        self.fleet = FakeFleet()
        self.service = SessionService(
            self.config, factory=self.fleet.factory, cold_start_wait_s=1.0
        )
        self.publish()
        self.service.start()

    def publish(self, *boxes: tuple[str, str]) -> None:
        """Write the status document `managerd` would publish right now.

        One sandbox to begin with. `managerd` creates the replacement and
        then makes the call, so the second row appears with the switch.
        """
        rows = boxes if boxes else ((SANDBOX, "ready"),)
        write_status(self.config.state_root, sandboxes=rows)

    def create(self, session: str) -> None:
        self.service.create_or_find(OWUI, CreateRequest(family=FAMILY, session=session))

    async def start_turn(self, session: str) -> LiveTurn:
        live = await self.service.run_turn(
            OWUI, FAMILY, session, RunTurnRequest(prompt="ping"), DOOR
        )
        await self.fleet.playpen(live.record.sandbox).next_start()
        return live

    async def settle(self, live: LiveTurn) -> None:
        playpen = self.fleet.playpen(live.record.sandbox)
        await playpen.play_turn(live.record.session, live.record.turn, ANSWER)
        await playpen.settle(live.record.session, live.record.turn)
        await settle_now(live.done)

    def request(
        self,
        mode: SwitchMode = SwitchMode.DRAIN,
        outgoing: str | None = SANDBOX,
        to: str = NEXT_SANDBOX,
        deadline_s: int = 300,
        family: str = FAMILY,
    ) -> SwitchRequest:
        return SwitchRequest(
            family=family,
            to=to,
            mode=mode,
            reason=REASON,
            outgoing=outgoing,
            deadline_s=deadline_s,
        )

    def switch(self, **over: Any) -> asyncio.Task[dict[str, Any]]:
        """`managerd` publishes the replacement, then calls."""
        self.publish((SANDBOX, "ready"), (NEXT_SANDBOX, "ready"))
        return asyncio.create_task(self.service.switch_sandbox(MANAGERD, self.request(**over)))

    def notes(self, session: str) -> list[dict[str, Any]]:
        return [
            line.body
            for line in self.service.store.journal.replay(FAMILY, session)
            if line.kind is LineKind.NOTE
        ]

    async def stop(self) -> None:
        await self.service.close()
        await self.fleet.stop()


async def test_a_drain_waits_for_two_running_turns(tmp_path: Path) -> None:
    """Contract 05 §5.3 rule 2. Running turns finish on the old sandbox."""
    harness = SwitchHarness(tmp_path)
    harness.create(FIRST)
    harness.create(SECOND)
    first = await harness.start_turn(FIRST)
    second = await harness.start_turn(SECOND)
    switch = harness.switch()

    # The answer waits for the last turn, not for the first.
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(switch), NOT_YET_S)

    await harness.settle(first)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(switch), NOT_YET_S)

    await harness.settle(second)
    body = await asyncio.wait_for(switch, 2.0)

    assert body == {
        "switched": True,
        "outcome": SwitchOutcome.DRAINED.value,
        "turns_running_at_start": 2,
        "turns_finished": 2,
        "turns_aborted": 0,
        "sessions": 2,
    }
    assert first.record.state is TurnState.SETTLED
    assert second.record.state is TurnState.SETTLED
    await harness.stop()


async def test_a_new_turn_goes_to_the_incoming_sandbox(tmp_path: Path) -> None:
    """§5.3 rule 1. `switched` is true from the instant the call is accepted."""
    harness = SwitchHarness(tmp_path)
    harness.create(FIRST)
    running = await harness.start_turn(FIRST)
    switch = harness.switch()
    await asyncio.sleep(NOT_YET_S)

    harness.create(SECOND)
    moved = await harness.start_turn(SECOND)

    assert moved.record.sandbox == NEXT_SANDBOX
    assert harness.fleet.dials == [SANDBOX, NEXT_SANDBOX]

    await harness.settle(running)
    await asyncio.wait_for(switch, 2.0)
    await harness.stop()


async def test_an_interrupt_ends_running_turns_at_once(tmp_path: Path) -> None:
    """§5.3 rule 3. A removal applies at once (invariant 9)."""
    harness = SwitchHarness(tmp_path)
    harness.create(FIRST)
    harness.create(SECOND)
    first = await harness.start_turn(FIRST)
    second = await harness.start_turn(SECOND)
    body = await asyncio.wait_for(harness.switch(mode=SwitchMode.INTERRUPT), 2.0)

    assert body["outcome"] == SwitchOutcome.INTERRUPTED.value
    assert body["turns_aborted"] == 2
    assert body["turns_finished"] == 0

    for live in (first, second):
        assert live.record.state is TurnState.ABORTED
        assert live.record.reason is TurnReason.PERMISSION_REMOVED

    await harness.stop()


async def test_a_drain_past_its_deadline_aborts_the_rest(tmp_path: Path) -> None:
    """§5.3 rule 2's last sentence. `sandbox_lost` is the turn's reason."""
    harness = SwitchHarness(tmp_path)
    harness.create(FIRST)
    stuck = await harness.start_turn(FIRST)
    body = await asyncio.wait_for(harness.switch(deadline_s=1), 5.0)

    assert body["outcome"] == SwitchOutcome.DEADLINE_HIT.value
    assert body["turns_aborted"] == 1
    assert body["turns_finished"] == 0
    assert stuck.record.state is TurnState.ABORTED
    assert stuck.record.reason is TurnReason.SANDBOX_LOST
    await harness.stop()


async def test_held_open_processes_close_before_the_channel(tmp_path: Path) -> None:
    """Contract 05 §4.4 step 2, and `attendance/AGENTS.md` switch rule 2."""
    harness = SwitchHarness(tmp_path)

    for session in (FIRST, SECOND):
        harness.create(session)
        await harness.settle(await harness.start_turn(session))

    old = harness.fleet.playpen(SANDBOX)
    await asyncio.wait_for(harness.switch(), 2.0)

    assert sorted(str(stop["session"]) for stop in old.stops) == [FIRST, SECOND]
    assert harness.fleet.channels[SANDBOX].alive is False
    await harness.stop()


async def test_an_idle_family_switches_with_nothing_to_drain(tmp_path: Path) -> None:
    harness = SwitchHarness(tmp_path)
    harness.create(FIRST)
    body = await asyncio.wait_for(harness.switch(), 2.0)

    assert body["outcome"] == SwitchOutcome.DRAINED.value
    assert body["turns_running_at_start"] == 0
    assert body["sessions"] == 1
    await harness.stop()


async def test_a_first_create_has_no_outgoing_sandbox(tmp_path: Path) -> None:
    """§5.1: `from` is null on a first create."""
    harness = SwitchHarness(tmp_path)
    body = await asyncio.wait_for(harness.switch(outgoing=None), 2.0)

    assert body["switched"] is True
    assert body["turns_running_at_start"] == 0
    assert harness.fleet.dials == [NEXT_SANDBOX]
    await harness.stop()


async def test_a_repeat_answers_the_same_and_does_nothing(tmp_path: Path) -> None:
    """§5.3 rule 7. `managerd` may retry after a timeout."""
    harness = SwitchHarness(tmp_path)
    harness.create(FIRST)
    live = await harness.start_turn(FIRST)
    old = harness.fleet.playpen(SANDBOX)
    first = await asyncio.wait_for(harness.switch(mode=SwitchMode.INTERRUPT), 2.0)
    aborts = len(old.aborts)
    second = await asyncio.wait_for(harness.switch(mode=SwitchMode.INTERRUPT), 2.0)

    assert second == first
    assert len(old.aborts) == aborts
    assert live.record.state is TurnState.ABORTED
    assert len(harness.notes(FIRST)) == 1
    await harness.stop()


async def test_every_session_learns_why_its_turn_ended(tmp_path: Path) -> None:
    """§5.3 rule 6. Both UIs can show the reason."""
    harness = SwitchHarness(tmp_path)
    harness.create(FIRST)
    harness.create(SECOND)
    await asyncio.wait_for(harness.switch(), 2.0)

    for session in (FIRST, SECOND):
        notes = harness.notes(session)
        assert len(notes) == 1
        assert notes[0]["from"] == SANDBOX
        assert notes[0]["to"] == NEXT_SANDBOX
        assert notes[0]["mode"] == SwitchMode.DRAIN.value
        assert notes[0]["reason"] == REASON

    await harness.stop()


async def test_sessions_survive_a_switch(tmp_path: Path) -> None:
    """§5.3 rule 4. A switch never deletes a session or changes an id."""
    harness = SwitchHarness(tmp_path)
    harness.create(FIRST)
    await harness.settle(await harness.start_turn(FIRST))
    await asyncio.wait_for(harness.switch(mode=SwitchMode.INTERRUPT), 2.0)

    body = harness.service.get_session(OWUI, FAMILY, FIRST, 10)

    assert body["session"] == FIRST
    assert body["turns_total"] == 1
    assert harness.service.store.session_ids(FAMILY) == [FIRST]
    await harness.stop()


@pytest.mark.parametrize("state", ["planned", "draining", "stopping", "gone", "failed"])
async def test_an_incoming_sandbox_that_cannot_serve_is_refused(tmp_path: Path, state: str) -> None:
    """§5.3 rule 8, read with §4.2's rule that this service runs the handshake."""
    harness = SwitchHarness(tmp_path)
    harness.publish((SANDBOX, "ready"), (NEXT_SANDBOX, state))

    with pytest.raises(ApiError) as refused:
        await harness.service.switch_sandbox(MANAGERD, harness.request())

    assert refused.value.code is ErrorCode.BAD_REQUEST
    await harness.stop()


async def test_an_incoming_sandbox_the_document_omits_is_refused(tmp_path: Path) -> None:
    harness = SwitchHarness(tmp_path)
    harness.publish((SANDBOX, "ready"))

    with pytest.raises(ApiError) as refused:
        await harness.service.switch_sandbox(MANAGERD, harness.request())

    assert refused.value.code is ErrorCode.BAD_REQUEST
    await harness.stop()


async def test_a_creating_incoming_sandbox_is_accepted(tmp_path: Path) -> None:
    """§4.2: a `creating` sandbox is dialled, because the handshake promotes it."""
    harness = SwitchHarness(tmp_path)
    harness.publish((SANDBOX, "ready"), (NEXT_SANDBOX, "creating"))
    body = await asyncio.wait_for(harness.service.switch_sandbox(MANAGERD, harness.request()), 2.0)

    assert body["switched"] is True

    harness.create(FIRST)
    live = await harness.start_turn(FIRST)

    assert live.record.sandbox == NEXT_SANDBOX
    await harness.stop()


@pytest.mark.parametrize(
    "over",
    [
        {"to": "other-s9"},
        {"outgoing": "other-s1"},
        {"to": SANDBOX},
        {"to": "not a sandbox"},
    ],
)
async def test_a_malformed_pair_is_refused(tmp_path: Path, over: dict[str, Any]) -> None:
    """§5.3 rule 8: another family's sandbox, and `from` equal to `to`."""
    harness = SwitchHarness(tmp_path)

    with pytest.raises(ApiError) as refused:
        await harness.service.switch_sandbox(MANAGERD, harness.request(**over))

    assert refused.value.code is ErrorCode.BAD_REQUEST
    await harness.stop()


async def test_an_unknown_family_is_refused(tmp_path: Path) -> None:
    harness = SwitchHarness(tmp_path)

    with pytest.raises(ApiError) as refused:
        await harness.service.switch_sandbox(
            MANAGERD, harness.request(family="ghost", to="ghost-s1", outgoing=None)
        )

    assert refused.value.code is ErrorCode.FAMILY_UNKNOWN
    await harness.stop()


async def test_no_door_may_switch(tmp_path: Path) -> None:
    """Contract 02 §3.1. The `managerd` token reaches `/internal/*` alone."""
    harness = SwitchHarness(tmp_path)

    with pytest.raises(ApiError) as refused:
        await harness.service.switch_sandbox(OWUI, harness.request())

    assert refused.value.code is ErrorCode.FORBIDDEN
    await harness.stop()


async def test_a_crash_mid_switch_converges_on_the_document(tmp_path: Path) -> None:
    """The status document is the truth about which sandbox serves.

    The new channel is open and the old one has not closed when the service
    dies. Nothing about the switch is on disk, so the restart reads what
    `managerd` published and moves every new turn onto the new sandbox.
    """
    harness = SwitchHarness(tmp_path)
    harness.create(FIRST)
    await harness.start_turn(FIRST)
    switch = harness.switch()
    await asyncio.sleep(NOT_YET_S)

    # Mid-switch: the new sandbox already serves and the old one still runs.
    harness.create(SECOND)
    await harness.start_turn(SECOND)
    switch.cancel()
    await harness.service.close()

    # `managerd` has since retired the old sandbox.
    harness.publish((SANDBOX, "stopping"), (NEXT_SANDBOX, "ready"))
    revived = SessionService(harness.config, factory=harness.fleet.factory, cold_start_wait_s=1.0)
    revived.start()

    recovered = revived.store.load_turns(FAMILY, FIRST)

    assert [turn.state for turn in recovered] == [TurnState.FAILED]
    assert recovered[0].reason is TurnReason.CHANNEL_LOST

    live = await revived.run_turn(OWUI, FAMILY, FIRST, RunTurnRequest(prompt="again"), DOOR)

    assert live.record.sandbox == NEXT_SANDBOX
    assert sorted(revived.store.session_ids(FAMILY)) == [FIRST, SECOND]

    await revived.close()
    await harness.stop()


async def test_a_switch_starts_no_process_of_its_own(tmp_path: Path) -> None:
    """§5.3 rule 8 asks for the handshake, rule 5 moves no process.

    So the switch dials the incoming sandbox once and starts nothing in it.
    The first turn there still pays pi's cold start.
    """
    harness = SwitchHarness(tmp_path)
    harness.create(FIRST)
    await harness.settle(await harness.start_turn(FIRST))
    await asyncio.wait_for(harness.switch(), 2.0)

    assert harness.fleet.dials == [SANDBOX, NEXT_SANDBOX]
    incoming = harness.fleet.playpen(NEXT_SANDBOX)

    assert incoming.started == []
    assert incoming.opens == []

    await wait_until(lambda: True)
    await harness.stop()


async def test_a_failed_handshake_keeps_the_family_serving(tmp_path: Path) -> None:
    """Contract 05 §5.3 rule 8: nothing moves until the handshake passes.

    The incoming sandbox answers `fatal` instead of `ready`, so the call is
    refused. No turn moves, the outgoing channel stays open, and `managerd`
    still owns both sandboxes.
    """
    harness = SwitchHarness(tmp_path)
    harness.fleet.plan(NEXT_SANDBOX, PlaypenPlan(fatal="control_mount_unwritable"))
    harness.create(FIRST)
    await harness.settle(await harness.start_turn(FIRST))

    with pytest.raises(ApiError) as refused:
        await asyncio.wait_for(harness.switch(), 2.0)

    assert refused.value.code is ErrorCode.SANDBOX_UNAVAILABLE
    assert harness.notes(FIRST) == []

    # The next turn still runs on the sandbox the family was serving on.
    live = await harness.start_turn(FIRST)

    assert live.record.sandbox == SANDBOX
    await harness.settle(live)
    await harness.stop()


async def test_a_refused_switch_is_retried_not_replayed(tmp_path: Path) -> None:
    """§5.3 rule 7 makes a completed switch idempotent, not a refused one.

    `managerd` retries the same call on its next pass. Answering that retry
    from the remembered failure would leave the family on two sandboxes for
    ever.
    """
    harness = SwitchHarness(tmp_path)
    harness.fleet.plan(NEXT_SANDBOX, PlaypenPlan(fatal="control_mount_unwritable"))
    harness.create(FIRST)

    with pytest.raises(ApiError):
        await asyncio.wait_for(harness.switch(), 2.0)

    harness.fleet.plan(NEXT_SANDBOX, PlaypenPlan())
    body = await asyncio.wait_for(harness.switch(), 2.0)

    assert body["switched"] is True
    await harness.stop()


async def test_a_new_sandbox_alone_moves_no_turn(tmp_path: Path) -> None:
    """Contract 05 §4.2 rule 1a. The §5 call is what moves new turns.

    §4.3 step 5b publishes the replacement before the call, so between the
    publish and the answer both sandboxes are `creating`. Reading the
    document per turn would hand the family to the replacement with no
    switch at all, and that replacement has had no handshake.
    """
    harness = SwitchHarness(tmp_path)
    harness.publish((SANDBOX, "creating"))
    harness.create(FIRST)
    await harness.settle(await harness.start_turn(FIRST))

    harness.publish((SANDBOX, "creating"), (NEXT_SANDBOX, "creating"))
    live = await harness.start_turn(FIRST)

    assert live.record.sandbox == SANDBOX
    assert harness.fleet.dials == [SANDBOX]
    await harness.settle(live)
    await harness.stop()


async def test_a_shutdown_drops_a_running_switch(tmp_path: Path) -> None:
    """A drain waits for turns, and `close()` may not wait for a drain.

    Nothing about a switch is on disk. A restart reads the status document
    and converges on it, so the run is dropped exactly as a pre-start is. A
    task left pending outlives the service it holds and warns when collected.
    """
    harness = SwitchHarness(tmp_path)
    harness.create(FIRST)
    await harness.start_turn(FIRST)
    switch = harness.switch()
    await asyncio.sleep(NOT_YET_S)

    assert switch.done() is False

    await harness.service.close()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(switch, 2.0)

    await harness.fleet.stop()
