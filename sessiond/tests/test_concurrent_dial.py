"""Concurrent turns of one family share one channel (contract 03 §1 rule 1).

Contract 03 opens ONE long-lived channel per family. Five chats at once all
reach `SupervisorLink.ensure_open` in the same moment. A second dial is not
just a wasted `sbx exec`: the new supervisor finds
the first one's lock and exits (§11.4 rule 4), and the turns written to the
losing channel never hear an answer.

`FakeChannel` is alive the instant `start()` returns, so it cannot show the
race. The real channel suspends while it spawns the child, and that suspension
is the whole window. `_SpawningChannel` adds it back and nothing else.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from agent_sessiond.auth import Principal
from agent_sessiond.channel import Channel, FakeChannel, SandboxDial
from agent_sessiond.requests import CreateRequest, RunTurnRequest
from agent_sessiond.service import SessionService
from agent_sessiond.turns import LiveTurn
from sessiond_harness import FAMILY, FakeSupervisor, SupervisorPlan, make_config, write_status

OWUI = Principal.DOOR_OWUI
DOOR = "owui-1"
HOW_MANY = 5


class _SpawningChannel(FakeChannel):
    """A fake channel that suspends while it opens, as a real one does."""

    async def start(self) -> None:
        await asyncio.sleep(0)
        await super().start()


class _SpawningFleet:
    """`FakeFleet`, but every channel takes a turn of the loop to open."""

    def __init__(self) -> None:
        self.dials: list[str] = []
        self.supervisors: list[FakeSupervisor] = []

    def factory(self, dial: SandboxDial) -> Channel:
        self.dials.append(dial.sandbox)
        channel = _SpawningChannel(dial.sandbox)
        supervisor = FakeSupervisor(channel, SupervisorPlan())
        self.supervisors.append(supervisor)
        supervisor.serve()

        return channel

    async def stop(self) -> None:
        for supervisor in self.supervisors:
            await supervisor.stop()


async def test_concurrent_turns_dial_once(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    write_status(config.state_root)
    fleet = _SpawningFleet()
    service = SessionService(config, factory=fleet.factory, cold_start_wait_s=1.0)
    service.start()

    sessions = [f"owui-chat-{index}" for index in range(HOW_MANY)]
    for session in sessions:
        service.create_or_find(OWUI, CreateRequest(family=FAMILY, session=session))

    live: list[LiveTurn] = await asyncio.gather(*(_turn(service, session) for session in sessions))

    assert fleet.dials == fleet.dials[:1], fleet.dials
    assert len({turn.record.turn for turn in live}) == HOW_MANY

    started = fleet.supervisors[0].started
    assert {message["session"] for message in started} == set(sessions)

    await service.close()
    await fleet.stop()


async def _turn(service: SessionService, session: str) -> LiveTurn:
    return await service.run_turn(
        OWUI,
        FAMILY,
        session,
        RunTurnRequest(prompt=f"prompt for {session}"),
        DOOR,
    )
