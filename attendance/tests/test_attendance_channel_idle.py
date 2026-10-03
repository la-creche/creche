"""Closing an idle channel, and the turn that dials it again (contract 03 §10).

A thin or autonomous family's VM must stop between jobs, and it stops only
once its one `sbx exec` is gone. Every test runs against a fake playpen,
with the 120-second window shrunk to a fraction of a second.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from attendance.auth import Principal
from attendance.config import Config
from attendance.playpen_link import PlaypenLink
from attendance.requests import CreateRequest, RunTurnRequest, Wait
from attendance.service import SessionService
from attendance.states import TurnState
from attendance.turns import LiveTurn
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

AUTO_FAMILY = "scrum-lead"
AUTO_SANDBOX = "scrum-lead-s1"
AUTO_SESSION = "auto-01JBQ7ZZ9D6M0Q4RXT2J8HYVBK"
SECOND_SESSION = "auto-01JBQ80M4F7S2YQ1VZK6W3TDEN"
TRIGGER_DOOR = Principal.DOOR_TRIGGER
OWUI_DOOR = Principal.DOOR_OWUI
DOOR = "trigger-1"
PROMPT = "Triage yesterday's tickets."

# Contract 03 §10 rule 3's 120 seconds, shrunk so a test watches it pass.
IDLE_TTL_S = 0.05
# Several idle windows. A channel still open after it never closed.
IDLE_WATCH_S = 0.3
# Contract 02 §5.4: `accepted` answers "at once". Far below any cold start.
AT_ONCE_S = 0.5


class IdleHarness:
    """One service serving one autonomous family and one attended family."""

    def __init__(self, config: Config, service: SessionService, fleet: FakeFleet) -> None:
        self.config = config
        self.service = service
        self.fleet = fleet

    def create(self, family: str = AUTO_FAMILY, session: str = AUTO_SESSION) -> None:
        self.service.create_or_find(_door(family), CreateRequest(family=family, session=session))

    async def run(
        self,
        family: str = AUTO_FAMILY,
        session: str = AUTO_SESSION,
        wait: Wait = Wait.STREAM,
    ) -> LiveTurn:
        return await self.service.run_turn(
            _door(family), family, session, RunTurnRequest(prompt=PROMPT, wait=wait), DOOR
        )

    async def full_turn(self, family: str = AUTO_FAMILY, session: str = AUTO_SESSION) -> None:
        live = await self.run(family, session)
        playpen = self.fleet.playpen(_sandbox_of(family))
        await playpen.next_start()
        await playpen.settle(session, live.record.turn)
        await settle_now(live.done)

    def link(self, family: str = AUTO_FAMILY) -> PlaypenLink:
        found = self.service.link(_sandbox_of(family))
        assert found is not None
        return found

    async def closed(self, family: str = AUTO_FAMILY) -> None:
        await wait_until(lambda: not self.link(family).is_open)

    async def stop(self) -> None:
        await self.service.close()
        await self.fleet.stop()


async def build(tmp_path: Path) -> IdleHarness:
    config = make_config(tmp_path)
    write_status(config.state_root)
    write_status(
        config.state_root,
        family=AUTO_FAMILY,
        kind="autonomous",
        sandboxes=((AUTO_SANDBOX, "ready"),),
    )
    fleet = FakeFleet()
    service = SessionService(
        config,
        factory=fleet.factory,
        cold_start_wait_s=1.0,
        channel_idle_ttl_s=IDLE_TTL_S,
    )
    service.start()
    return IdleHarness(config, service, fleet)


def _door(family: str) -> Principal:
    return TRIGGER_DOOR if family == AUTO_FAMILY else OWUI_DOOR


def _sandbox_of(family: str) -> str:
    return AUTO_SANDBOX if family == AUTO_FAMILY else SANDBOX


async def test_an_autonomous_channel_closes_after_its_job(tmp_path: Path) -> None:
    """Contract 03 §10 rule 3 and `spec.md` §3.4: the VM's memory returns."""
    harness = await build(tmp_path)
    harness.create()
    await harness.full_turn()
    await harness.closed()

    assert harness.fleet.playpen(AUTO_SANDBOX).shutdowns == 1
    await harness.stop()


async def test_the_next_firing_dials_again(tmp_path: Path) -> None:
    """Contract 03 §10 rule 5. A closed channel costs the next turn a cold
    start, never the turn itself."""
    harness = await build(tmp_path)
    harness.create()
    await harness.full_turn()
    await harness.closed()

    harness.create(session=SECOND_SESSION)
    await harness.full_turn(session=SECOND_SESSION)

    assert harness.fleet.dials == [AUTO_SANDBOX, AUTO_SANDBOX]
    await harness.stop()


async def test_an_attended_channel_stays_open(tmp_path: Path) -> None:
    """Contract 03 §10 rule 2. The sandbox stays warm for the next message."""
    harness = await build(tmp_path)
    harness.create(FAMILY, CHAT_SESSION)
    await harness.full_turn(FAMILY, CHAT_SESSION)
    await asyncio.sleep(IDLE_WATCH_S)

    assert harness.link(FAMILY).is_open is True
    assert harness.fleet.playpen(SANDBOX).shutdowns == 0
    await harness.stop()


async def test_an_accepted_turn_answers_before_its_dial(tmp_path: Path) -> None:
    """Contract 02 §5.4: `accepted` answers at once. After an idle close the
    next firing is a cold dial of 10 to 15 seconds (contract 03 §10 rule 5),
    and the trigger door stops reading after 15."""
    harness = await build(tmp_path)
    gate = asyncio.Event()
    harness.fleet.plan(AUTO_SANDBOX, PlaypenPlan(ready_gate=gate))
    harness.create()

    live = await asyncio.wait_for(harness.run(wait=Wait.ACCEPTED), AT_ONCE_S)

    assert live.record.state is TurnState.RUNNING

    gate.set()
    await wait_until(lambda: AUTO_SANDBOX in harness.fleet.playpens)
    started = await harness.fleet.playpen(AUTO_SANDBOX).next_start()

    assert started["turn"] == live.record.turn
    await harness.stop()


async def test_a_turn_stopped_during_its_dial_is_never_sent(tmp_path: Path) -> None:
    """The dial outlives the answer, so the turn can end first. Sending it
    anyway would run a stopped job and hold the channel open for ever."""
    harness = await build(tmp_path)
    gate = asyncio.Event()
    harness.fleet.plan(AUTO_SANDBOX, PlaypenPlan(ready_gate=gate))
    harness.create()
    live = await harness.run(wait=Wait.ACCEPTED)
    await harness.service.stop_turn(
        TRIGGER_DOOR, AUTO_FAMILY, AUTO_SESSION, live.record.turn, "operator", DOOR
    )

    gate.set()
    await wait_until(lambda: AUTO_SANDBOX in harness.fleet.playpens)
    await harness.closed()

    assert live.record.state is TurnState.ABORTED
    assert harness.fleet.playpen(AUTO_SANDBOX).started == []
    await harness.stop()
