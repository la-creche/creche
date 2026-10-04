"""The service end to end, with a fake playpen on the far side."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest
from attendance.auth import Principal
from attendance.config import Config
from attendance.errors import ApiError, ErrorCode, TurnReason
from attendance.family_status import StatusReader
from attendance.faults import FaultCode
from attendance.models import Holder, JournalLine, LineKind, OwuiRefs, Usage
from attendance.paths import fault_file, journal_file
from attendance.persona import BODY_BUDGET_BYTES
from attendance.requests import (
    CreateRequest,
    ListQuery,
    RunTurnRequest,
    WriterRequest,
)
from attendance.service import SessionService
from attendance.states import SessionState, TurnState
from attendance.streams import Follow, StreamEnd
from attendance.turns import LiveTurn
from attendance.wire import MAX_EVENT_DEPTH, MAX_LINE_BYTES, MAX_PERSONA_BYTES, SettledLine
from attendance_harness import (
    CHAT_SESSION,
    CONFIG_REV,
    DEEP_BODY,
    EPOCH,
    FAMILY,
    PLAYPEN_ENV,
    SANDBOX,
    FakeFleet,
    PlaypenPlan,
    journal_line,
    make_config,
    settle_now,
    wait_until,
    write_status,
)

OWUI = Principal.DOOR_OWUI
TUI = Principal.DOOR_TUI
VIEW = Principal.VIEW_RO
DOOR = "owui-1"
PROMPT = "Which sensor dropped out last night?"
ANSWER = "Sensor kitchen_temp stopped reporting at 02:14."
REFUSAL_BUDGET = 10

# A second registry revision, so a test can prove the value is read per turn.
NEXT_CONFIG_REV = "reg-0b74e2"


class Harness:
    """One service, one fleet and the config all three share."""

    def __init__(self, config: Config, service: SessionService, fleet: FakeFleet) -> None:
        self.config = config
        self.service = service
        self.fleet = fleet

    def create(self, session: str = CHAT_SESSION) -> dict[str, object]:
        body, _ = self.service.create_or_find(OWUI, CreateRequest(family=FAMILY, session=session))
        return body

    async def start_turn(
        self,
        session: str = CHAT_SESSION,
        prompt: str = PROMPT,
        key: str | None = None,
        persona: str = "",
        owui: OwuiRefs | None = None,
    ) -> LiveTurn:
        live = await self.service.run_turn(
            OWUI,
            FAMILY,
            session,
            RunTurnRequest(prompt=prompt, idempotency_key=key, persona_text=persona, owui=owui),
            DOOR,
        )
        await self.fleet.playpen().next_start()
        return live

    async def full_turn(self, session: str = CHAT_SESSION) -> LiveTurn:
        live = await self.start_turn(session)
        playpen = self.fleet.playpen()
        await playpen.play_turn(session, live.record.turn, ANSWER)
        await playpen.settle(session, live.record.turn)
        await settle_now(live.done)
        return live

    def kinds(self, session: str = CHAT_SESSION) -> list[LineKind]:
        return [line.kind for line in self.service.store.journal.replay(FAMILY, session)]

    def faults(self) -> list[str]:
        path = fault_file(self.config.state_root, FAMILY)

        if not path.is_file():
            return []

        return [entry["code"] for entry in json.loads(path.read_text())["faults"]]

    async def stop(self) -> None:
        await self.service.close()
        await self.fleet.stop()


async def build(tmp_path: Path, **status: object) -> Harness:
    """A service and a fleet, both rooted in one temporary directory."""
    config = make_config(tmp_path)
    write_status(config.state_root, **status)  # pyright: ignore[reportArgumentType]
    fleet = FakeFleet()
    service = SessionService(config, factory=fleet.factory, cold_start_wait_s=1.0)
    service.start()
    return Harness(config, service, fleet)


async def collect(stream: AsyncGenerator[JournalLine, None]) -> list[JournalLine]:
    return [line async for line in stream]


async def test_a_full_turn_reaches_settled(tmp_path: Path) -> None:
    harness = await build(tmp_path)
    harness.create()
    live = await harness.full_turn()

    assert live.record.state is TurnState.SETTLED
    assert live.answer() == ANSWER
    assert live.record.usage.cost_usd == pytest.approx(0.014)

    body = harness.service.get_session(OWUI, FAMILY, CHAT_SESSION, 10)
    assert body["state"] == SessionState.IDLE.value
    assert body["turns_total"] == 1
    assert body["sandbox"] == SANDBOX
    await harness.stop()


#: Each door that writes, with a session id of its own prefix (contract 02 §3.1).
DOOR_SESSIONS = [
    (OWUI, CHAT_SESSION),
    (TUI, "tui-01J9ZQ5V7Y8X4W3T2S1R0QPNMK"),
    (Principal.DOOR_DELEGATE, "job-01JBQ7ZZ9D6M0Q4RXT2J8HYVBK"),
    (Principal.DOOR_TRIGGER, "auto-01JBQ7ZZ9D6M0Q4RXT2J8HYVBK"),
]


@pytest.mark.parametrize(("door", "session"), DOOR_SESSIONS)
async def test_a_family_of_no_known_kind_takes_no_session(
    tmp_path: Path, door: Principal, session: str
) -> None:
    """Contract 02 §3.1. A status document that states no kind opens no door.

    The attended doors are in the list on purpose: a reader that takes such
    a document as `attended` gives them a family of another kind.
    """
    harness = await build(tmp_path, kind="robot")

    with pytest.raises(ApiError) as caught:
        harness.service.create_or_find(door, CreateRequest(family=FAMILY, session=session))

    assert caught.value.detail == {"kind": None}

    assert caught.value.code is ErrorCode.FORBIDDEN
    assert harness.fleet.dials == []
    await harness.stop()


@pytest.mark.parametrize("door", [OWUI, Principal.DOOR_DELEGATE])
async def test_a_family_that_never_validated_is_invalid_for_each_door(
    tmp_path: Path, door: Principal
) -> None:
    """Contract 05 §3.1 and contract 02 §14. `caregiver` writes an empty kind
    for this family, so no door can be the wrong one."""
    harness = await build(tmp_path, kind="", state="invalid", never_valid=True, sandboxes=())

    with pytest.raises(ApiError) as caught:
        harness.service.create_or_find(door, CreateRequest(family=FAMILY, session=CHAT_SESSION))

    assert caught.value.code is ErrorCode.FAMILY_INVALID
    await harness.stop()


async def test_a_session_takes_no_turn_once_the_kind_is_gone(tmp_path: Path) -> None:
    """The kind is read for each turn. A session made earlier does not keep it."""
    harness = await build(tmp_path)
    harness.create()
    write_status(harness.config.state_root, kind="")

    with pytest.raises(ApiError) as caught:
        await harness.service.run_turn(
            OWUI, FAMILY, CHAT_SESSION, RunTurnRequest(prompt=PROMPT), DOOR
        )

    assert caught.value.code is ErrorCode.FORBIDDEN
    assert harness.kinds().count(LineKind.TURN_STARTED) == 0
    await harness.stop()


async def test_no_channel_opens_for_a_family_of_no_known_kind(tmp_path: Path) -> None:
    """A channel takes the kind of its family. Each caller checks the kind
    first, and the place that makes the channel checks it again."""
    harness = await build(tmp_path, kind="robot")
    status = StatusReader(harness.config.state_root).require(FAMILY)

    with pytest.raises(ApiError) as caught:
        await harness.service._link_for(FAMILY, status, SANDBOX)

    assert caught.value.code is ErrorCode.FORBIDDEN
    assert caught.value.detail == {"kind": None}
    assert harness.service.link(SANDBOX) is None
    assert harness.fleet.dials == []
    await harness.stop()


async def test_creating_a_session_pre_starts_its_pi_process(tmp_path: Path) -> None:
    """Contract 03 §4.7 rules 8 and 9. The cold start is paid before the prompt."""
    harness = await build(tmp_path)
    harness.create()
    await wait_until(lambda: bool(harness.fleet.playpens))
    await wait_until(lambda: bool(harness.fleet.playpen().opens))

    opened = harness.fleet.playpen().opens[0]

    assert opened["session"] == CHAT_SESSION
    assert opened["config_rev"] == CONFIG_REV
    assert opened["env_epoch"] == EPOCH
    assert "prompt" not in opened
    assert "turn" not in opened
    await harness.stop()


async def test_a_pre_start_that_cannot_dial_still_answers_the_door(tmp_path: Path) -> None:
    """§4.7 rules 9 and 11. It is an optimisation, never a precondition."""
    harness = await build(tmp_path, sandboxes=((SANDBOX, "failed"),))
    body = harness.create()

    assert body["session"] == CHAT_SESSION
    # The dial never happened, and creating the session was not affected.
    await asyncio.sleep(0.05)
    assert harness.fleet.dials == []
    await harness.stop()


async def test_a_turn_works_when_the_pre_start_was_refused(tmp_path: Path) -> None:
    """§4.7 rule 10. `start_turn` is always correct on its own."""
    harness = await build(tmp_path)
    harness.create()
    await wait_until(lambda: bool(harness.fleet.playpens))
    await harness.fleet.playpen().open_session(CHAT_SESSION, resident=False)
    live = await harness.full_turn()

    assert live.record.state is TurnState.SETTLED
    assert harness.faults() == []
    await harness.stop()


async def test_the_prompt_reaches_the_journal_before_the_channel(tmp_path: Path) -> None:
    """Contract 02 §8.2. A first turn that dies leaves no pi file at all."""
    harness = await build(tmp_path)
    harness.create()
    live = await harness.start_turn()

    lines = list(harness.service.store.journal.replay(FAMILY, CHAT_SESSION))
    started = [line for line in lines if line.kind is LineKind.TURN_STARTED]

    assert len(started) == 1
    assert started[0].body["prompt"] == PROMPT
    assert started[0].body["sandbox"] == SANDBOX
    assert live.first_seq == started[0].journal_seq
    await harness.stop()


async def test_start_turn_carries_the_contract_fields(tmp_path: Path) -> None:
    harness = await build(tmp_path)
    harness.create()
    live = await harness.start_turn()
    sent = harness.fleet.playpen().started[-1]

    sessions = harness.config.sessions_root / FAMILY

    assert sent["turn"] == live.record.turn
    assert sent["session"] == CHAT_SESSION
    # Contract 03 §7.1: the path in the sandbox IS the host path, because
    # that is the only thing `sbx create` can do with a mount.
    assert sent["cwd"] == f"{sessions}/{CHAT_SESSION}"
    assert sent["session_dir"] == f"{sessions}/{CHAT_SESSION}/pi"
    assert sent["env_epoch"] == 7
    assert sent["config_rev"] == "reg-9f21c4"
    assert "persona" not in sent
    await harness.stop()


async def test_a_new_revision_reaches_the_next_turn(tmp_path: Path) -> None:
    """The CURRENT `config_rev`, read per turn, not the one the session began on.

    Contract 03 §4.1 makes the field the revision of the family config mount,
    and §6 rule 5 lets the playpen replace a held-open process built under
    an older one. A value cached at create would hide every later revision,
    so the playpen could never tell a stale process from a current one.
    """
    harness = await build(tmp_path)
    harness.create()
    await harness.full_turn()

    # `caregiver` publishes a new revision under the running service.
    write_status(harness.config.state_root, config_rev=NEXT_CONFIG_REV)
    await harness.start_turn()

    assert harness.fleet.playpen().started[-1]["config_rev"] == NEXT_CONFIG_REV
    await harness.stop()


async def test_the_hello_answers_the_ready(tmp_path: Path) -> None:
    harness = await build(tmp_path)
    harness.create()
    await harness.start_turn()
    hello = harness.fleet.playpen().hello

    assert hello is not None
    assert hello["protocol"] == "1.0"
    assert hello["family"] == FAMILY
    assert hello["sandbox"] == SANDBOX
    assert hello["env_epoch"] == 7
    assert hello["pi_idle_ttl_s"] == 900
    assert hello["channel_idle_ttl_s"] == 0
    assert hello["max_line_bytes"] == MAX_LINE_BYTES
    await harness.stop()


async def test_five_sessions_run_at_once_over_one_channel(tmp_path: Path) -> None:
    harness = await build(tmp_path)
    sessions = [f"owui-chat-{index}" for index in range(5)]

    for session in sessions:
        harness.create(session)

    live = [await harness.start_turn(session) for session in sessions]
    playpen = harness.fleet.playpen()

    for index, turn in enumerate(live):
        await playpen.emit_text(sessions[index], turn.record.turn, f"answer {index}")

    for index, turn in enumerate(live):
        await playpen.settle(sessions[index], turn.record.turn)
        await settle_now(turn.done)

        assert turn.record.state is TurnState.SETTLED
        assert turn.answer() == f"answer {index}"

    assert harness.fleet.dials == [SANDBOX]
    await harness.stop()


async def test_replay_from_a_sequence_number(tmp_path: Path) -> None:
    harness = await build(tmp_path)
    harness.create()
    await harness.full_turn()

    whole = await collect(
        harness.service.stream(VIEW, FAMILY, CHAT_SESSION, follow=Follow.REPLAY_ONLY)
    )
    tail = await collect(
        harness.service.stream(VIEW, FAMILY, CHAT_SESSION, from_seq=3, follow=Follow.REPLAY_ONLY)
    )

    assert [line.journal_seq for line in whole] == list(range(1, len(whole) + 1))
    assert [line.journal_seq for line in tail] == list(range(4, len(whole) + 1))
    await harness.stop()


async def test_a_reader_that_leaves_changes_nothing(tmp_path: Path) -> None:
    """Contract 02 §5.4. The turn keeps running (invariant 4)."""
    harness = await build(tmp_path)
    harness.create()
    live = await harness.start_turn()
    playpen = harness.fleet.playpen()

    stream = harness.service.stream(OWUI, FAMILY, CHAT_SESSION, turn=live.record.turn)
    await asyncio.wait_for(anext(stream), 2.0)
    await stream.aclose()

    await playpen.play_turn(CHAT_SESSION, live.record.turn, ANSWER)
    await playpen.settle(CHAT_SESSION, live.record.turn)
    await settle_now(live.done)

    assert live.record.state is TurnState.SETTLED
    assert LineKind.TURN_SETTLED in harness.kinds()
    await harness.stop()


async def test_a_turn_stream_ends_after_the_turn_settles(tmp_path: Path) -> None:
    harness = await build(tmp_path)
    harness.create()
    live = await harness.start_turn()
    playpen = harness.fleet.playpen()

    stream = harness.service.stream(
        OWUI,
        FAMILY,
        CHAT_SESSION,
        from_seq=live.first_seq - 1,
        turn=live.record.turn,
        end=StreamEnd.AFTER_TERMINAL,
    )
    reading = asyncio.create_task(collect(stream))
    await playpen.play_turn(CHAT_SESSION, live.record.turn, ANSWER)
    await playpen.settle(CHAT_SESSION, live.record.turn)
    lines = await asyncio.wait_for(reading, 2.0)

    assert lines[0].kind is LineKind.TURN_STARTED
    assert lines[-1].kind is LineKind.TURN_SETTLED
    await harness.stop()


async def test_an_idempotent_repeat_returns_the_same_turn(tmp_path: Path) -> None:
    harness = await build(tmp_path)
    harness.create()
    first = await harness.start_turn(key="msg-1")
    again = await harness.service.run_turn(
        OWUI,
        FAMILY,
        CHAT_SESSION,
        RunTurnRequest(prompt=PROMPT, idempotency_key="msg-1"),
        DOOR,
    )

    assert again is first
    assert len(harness.fleet.playpen().started) == 1
    await harness.stop()


async def test_the_same_key_with_another_prompt_refuses(tmp_path: Path) -> None:
    harness = await build(tmp_path)
    harness.create()
    await harness.start_turn(key="msg-1")

    with pytest.raises(ApiError) as caught:
        await harness.service.run_turn(
            OWUI,
            FAMILY,
            CHAT_SESSION,
            RunTurnRequest(prompt="a different question", idempotency_key="msg-1"),
            DOOR,
        )

    assert caught.value.code is ErrorCode.IDEMPOTENCY_MISMATCH
    await harness.stop()


async def test_a_second_writer_is_refused_and_may_still_read(tmp_path: Path) -> None:
    """Contract 02 §7.2 and §7.3 rule 4. Only writing is blocked.

    The turn is left running on purpose: an IDLE lease passes to the other
    door (§7.3 rule 5), so a settled turn would not be refused.
    """
    harness = await build(tmp_path)
    harness.create()
    await harness.start_turn()

    with pytest.raises(ApiError) as caught:
        await harness.service.run_turn(
            TUI, FAMILY, CHAT_SESSION, RunTurnRequest(prompt="mine now"), "tui-1"
        )

    assert caught.value.code is ErrorCode.SESSION_BUSY
    assert caught.value.detail is not None
    assert caught.value.detail["holder"] == "owui"

    lines = await collect(
        harness.service.stream(TUI, FAMILY, CHAT_SESSION, follow=Follow.REPLAY_ONLY)
    )
    assert lines
    await harness.stop()


async def test_a_settled_chat_turn_frees_the_session_now(tmp_path: Path) -> None:
    """Contract 02 §7.3 rule 5. Open WebUI turn, then a terminal, with no wait."""
    harness = await build(tmp_path)
    harness.create()
    await harness.full_turn()

    body = harness.service.take_writer(
        TUI, FAMILY, CHAT_SESSION, WriterRequest(holder=Holder.TUI), "tui.4021"
    )
    reasons = [
        line.body["reason"]
        for line in harness.service.store.journal.replay(FAMILY, CHAT_SESSION)
        if line.kind is LineKind.WRITER_CHANGED
    ]

    assert body["holder"] == "tui"
    assert reasons[-1] == "taken_over"
    await harness.stop()


async def test_releasing_the_lease_lets_the_next_door_in(tmp_path: Path) -> None:
    """Contract 02 §5.10. The release is journalled for both UIs."""
    harness = await build(tmp_path)
    harness.create()
    await harness.full_turn()

    released = harness.service.release_writer(OWUI, FAMILY, CHAT_SESSION, DOOR)
    reasons = [
        line.body["reason"]
        for line in harness.service.store.journal.replay(FAMILY, CHAT_SESSION)
        if line.kind is LineKind.WRITER_CHANGED
    ]

    assert released == {"released": True}
    assert harness.service.lease(FAMILY, CHAT_SESSION) is None
    assert reasons[-1] == "released"
    await harness.stop()


async def test_a_release_waits_for_a_turn_in_flight(tmp_path: Path) -> None:
    """Contract 02 §5.10 rule 2 and §5.11 rule 2."""
    harness = await build(tmp_path)
    harness.create()
    live = await harness.start_turn()

    with pytest.raises(ApiError) as lease_refused:
        harness.service.release_writer(OWUI, FAMILY, CHAT_SESSION, DOOR)

    with pytest.raises(ApiError) as process_refused:
        await harness.service.release_process(OWUI, FAMILY, CHAT_SESSION, DOOR)

    assert lease_refused.value.code is ErrorCode.SESSION_BUSY
    assert process_refused.value.code is ErrorCode.SESSION_BUSY
    assert lease_refused.value.detail == {"turn": live.record.turn}
    await harness.stop()


async def test_releasing_the_process_asks_the_sandbox(tmp_path: Path) -> None:
    """Contract 02 §5.11. The session survives, the pi process does not."""
    harness = await build(tmp_path)
    harness.create()
    await harness.full_turn()

    answer = await harness.service.release_process(OWUI, FAMILY, CHAT_SESSION, DOOR)
    playpen = harness.fleet.playpen()
    await wait_until(lambda: len(playpen.stops) == 1)

    assert answer == {"released": True}
    assert playpen.stops[0]["session"] == CHAT_SESSION
    assert harness.service.store.load(FAMILY, CHAT_SESSION) is not None
    await harness.stop()


async def test_a_second_turn_on_a_busy_session_is_refused(tmp_path: Path) -> None:
    harness = await build(tmp_path)
    harness.create()
    await harness.start_turn()

    with pytest.raises(ApiError) as caught:
        await harness.service.run_turn(
            OWUI, FAMILY, CHAT_SESSION, RunTurnRequest(prompt="and again"), DOOR
        )

    assert caught.value.code is ErrorCode.SESSION_BUSY
    await harness.stop()


async def test_channel_loss_fails_the_turn_and_keeps_the_session(tmp_path: Path) -> None:
    harness = await build(tmp_path)
    harness.create()
    live = await harness.start_turn()
    await harness.fleet.playpen().drop()
    await settle_now(live.done)

    assert live.record.state is TurnState.FAILED
    assert live.record.reason is TurnReason.CHANNEL_LOST
    assert harness.service.store.exists(FAMILY, CHAT_SESSION)
    assert harness.service.get_session(OWUI, FAMILY, CHAT_SESSION, 10)["state"] == "failed"
    await harness.stop()


async def test_a_dead_pi_process_fails_only_its_turn(tmp_path: Path) -> None:
    harness = await build(tmp_path)
    harness.create("owui-a")
    harness.create("owui-b")
    first = await harness.start_turn("owui-a")
    second = await harness.start_turn("owui-b")
    playpen = harness.fleet.playpen()

    await playpen.kill_process("owui-a", first.record.turn)
    await settle_now(first.done)
    await playpen.settle("owui-b", second.record.turn)
    await settle_now(second.done)

    assert first.record.reason is TurnReason.SANDBOX_LOST
    assert second.record.state is TurnState.SETTLED
    await harness.stop()


async def test_a_sequence_gap_fails_that_turn(tmp_path: Path) -> None:
    """Contract 03 §13 rule 4."""
    harness = await build(tmp_path)
    harness.create()
    live = await harness.start_turn()
    await harness.fleet.playpen().skip_sequence(CHAT_SESSION, live.record.turn)
    await settle_now(live.done)

    assert live.record.reason is TurnReason.PROTOCOL_VIOLATION
    await harness.stop()


async def test_a_run_of_bad_lines_degrades_the_family(tmp_path: Path) -> None:
    """Contract 03 §13 rule 2: ten refusals in the window close the channel."""
    harness = await build(tmp_path)
    harness.create()
    live = await harness.start_turn()

    for _ in range(REFUSAL_BUDGET):
        await harness.fleet.playpen().send_malformed()

    await settle_now(live.done)

    assert FaultCode.PROTOCOL_VIOLATION.value in harness.faults()
    assert live.record.reason is TurnReason.CHANNEL_LOST
    await harness.stop()


async def test_a_healthy_handshake_clears_the_violation(tmp_path: Path) -> None:
    """The file holds open faults, not a history (contract 05 §3.3.1)."""
    harness = await build(tmp_path)
    harness.create()
    live = await harness.start_turn()

    for _ in range(REFUSAL_BUDGET):
        await harness.fleet.playpen().send_malformed()

    await settle_now(live.done)
    assert FaultCode.PROTOCOL_VIOLATION.value in harness.faults()

    await wait_until(lambda: harness.service.link(SANDBOX) is None)
    await harness.start_turn()

    assert harness.faults() == []
    await harness.stop()


async def test_a_restart_clears_this_writers_faults(tmp_path: Path) -> None:
    """A fresh process has observed nothing, so it reports nothing."""
    harness = await build(tmp_path)
    harness.fleet.plan(SANDBOX, PlaypenPlan(protocol="2.0"))
    harness.create()
    live = await harness.service.run_turn(
        OWUI, FAMILY, CHAT_SESSION, RunTurnRequest(prompt=PROMPT), DOOR
    )
    await settle_now(live.done)

    assert FaultCode.PROTOCOL_MISMATCH.value in harness.faults()
    await harness.stop()

    revived = await build(tmp_path)
    assert revived.faults() == []
    await revived.stop()


async def test_one_oversized_line_leaves_the_channel_usable(tmp_path: Path) -> None:
    """Probe 0a, A8: one byte over was refused and the channel kept working."""
    harness = await build(tmp_path)
    harness.create()
    live = await harness.start_turn()
    playpen = harness.fleet.playpen()

    await playpen.send_oversized(MAX_LINE_BYTES + 1)
    await playpen.play_turn(CHAT_SESSION, live.record.turn, ANSWER)
    await playpen.settle(CHAT_SESSION, live.record.turn)
    await settle_now(live.done)

    assert live.record.state is TurnState.SETTLED
    assert harness.faults() == []
    await harness.stop()


async def test_a_protocol_mismatch_writes_its_fault(tmp_path: Path) -> None:
    harness = await build(tmp_path)
    harness.fleet.plan(SANDBOX, PlaypenPlan(protocol="2.0"))
    harness.create()
    live = await harness.service.run_turn(
        OWUI, FAMILY, CHAT_SESSION, RunTurnRequest(prompt=PROMPT), DOOR
    )
    await settle_now(live.done)

    assert FaultCode.PROTOCOL_MISMATCH.value in harness.faults()
    assert live.record.reason is TurnReason.CHANNEL_LOST
    await harness.stop()


async def test_orphan_processes_are_reported_then_cleared(tmp_path: Path) -> None:
    harness = await build(tmp_path)
    harness.fleet.plan(SANDBOX, PlaypenPlan(foreign_pi_processes=2))
    harness.create()
    live = await harness.start_turn()

    assert FaultCode.ORPHAN_PROCESSES.value in harness.faults()

    await harness.fleet.playpen().settle(CHAT_SESSION, live.record.turn)
    await settle_now(live.done)

    # A clean sandbox on the next dial clears it: the file holds the open
    # faults, not a history (contract 05 §3.3.1).
    await harness.fleet.playpen().drop()
    await wait_until(lambda: harness.service.link(SANDBOX) is None)
    harness.fleet.plan(SANDBOX, PlaypenPlan())
    await harness.start_turn()

    assert harness.faults() == []
    await harness.stop()


async def test_restart_recovery_finds_every_session(tmp_path: Path) -> None:
    harness = await build(tmp_path)
    harness.create("owui-a")
    harness.create("owui-b")
    await harness.full_turn("owui-a")
    unfinished = await harness.start_turn("owui-b")
    await harness.stop()

    revived = await build(tmp_path)
    listing = revived.service.list_sessions(VIEW, ListQuery())

    assert {row["session"] for row in listing["sessions"]} == {"owui-a", "owui-b"}

    # A restart is a channel drop, so a turn that was running is interrupted.
    found = revived.service.live_turn(FAMILY, "owui-b", unfinished.record.turn)
    assert found is not None
    assert found.record.state is TurnState.FAILED
    assert found.record.reason is TurnReason.CHANNEL_LOST
    await revived.stop()


async def test_a_restart_keeps_the_journal_gapless(tmp_path: Path) -> None:
    harness = await build(tmp_path)
    harness.create()
    await harness.full_turn()
    before = [
        line.journal_seq for line in harness.service.store.journal.replay(FAMILY, CHAT_SESSION)
    ]
    await harness.stop()

    revived = await build(tmp_path)
    await revived.full_turn()
    after = [
        line.journal_seq for line in revived.service.store.journal.replay(FAMILY, CHAT_SESSION)
    ]
    await revived.stop()

    assert after[: len(before)] == before
    assert after == list(range(1, len(after) + 1))


async def test_a_restart_reads_past_an_unreadable_journal_line(tmp_path: Path) -> None:
    """The journal of one session never stops the service from starting."""
    harness = await build(tmp_path)
    harness.create()
    await harness.full_turn()
    await harness.stop()

    with journal_file(harness.service.store.root, FAMILY, CHAT_SESSION).open("ab") as handle:
        handle.write(journal_line(99, DEEP_BODY))

    revived = await build(tmp_path)
    live = await revived.full_turn()

    assert live.record.state is TurnState.SETTLED
    assert revived.kinds().count(LineKind.TURN_SETTLED) == 2
    await revived.stop()


async def test_an_event_nested_too_deep_reaches_the_journal_capped(tmp_path: Path) -> None:
    """The journal never holds a line that its own reader cannot follow."""
    harness = await build(tmp_path)
    harness.create()
    live = await harness.start_turn()
    playpen = harness.fleet.playpen()
    await playpen.emit_nested(CHAT_SESSION, live.record.turn, MAX_EVENT_DEPTH + 1)
    await playpen.settle(CHAT_SESSION, live.record.turn)
    await settle_now(live.done)

    events = [
        line.body
        for line in harness.service.store.journal.replay(FAMILY, CHAT_SESSION)
        if line.kind is LineKind.PI_EVENT
    ]

    assert live.record.state is TurnState.SETTLED
    assert [sorted(event) for event in events] == [["original_bytes", "truncated", "type"]]
    await harness.stop()


async def test_steer_reaches_the_playpen(tmp_path: Path) -> None:
    harness = await build(tmp_path)
    harness.create()
    live = await harness.start_turn()
    await harness.service.steer(
        OWUI, FAMILY, CHAT_SESSION, live.record.turn, "Check the logbook.", DOOR
    )
    await wait_until(lambda: bool(harness.fleet.playpen().steers))

    assert harness.fleet.playpen().steers[-1]["message"] == "Check the logbook."
    await harness.stop()


async def test_only_the_lease_holder_steers(tmp_path: Path) -> None:
    harness = await build(tmp_path)
    harness.create()
    live = await harness.start_turn()

    with pytest.raises(ApiError) as caught:
        await harness.service.steer(TUI, FAMILY, CHAT_SESSION, live.record.turn, "mine", "tui-1")

    assert caught.value.code is ErrorCode.SESSION_BUSY
    await harness.stop()


async def test_a_late_ender_of_a_settled_turn_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Two enders can race. The turn settles while a stop waits for the channel.

    The second ender changes nothing and is no defect, so it writes no line
    and no log line.
    """
    harness = await build(tmp_path)
    harness.create()
    live = await harness.start_turn()
    link = harness.service.link(SANDBOX)
    assert link is not None
    send = link.send
    settled = SettledLine(
        session=CHAT_SESSION, turn=live.record.turn, turn_seq=1, resident=True, usage=Usage()
    )

    async def settle_first(message: dict[str, Any]) -> None:
        if message["type"] == "abort":
            await harness.service.handle_settled(FAMILY, settled)

        await send(message)

    monkeypatch.setattr(link, "send", settle_first)

    with caplog.at_level(logging.ERROR, logger="attendance"):
        body = await harness.service.stop_turn(
            OWUI, FAMILY, CHAT_SESSION, live.record.turn, "stop", DOOR
        )

    kinds = harness.kinds()

    assert body["state"] == TurnState.SETTLED.value
    assert kinds.count(LineKind.TURN_SETTLED) == 1
    assert LineKind.TURN_ABORTED not in kinds
    assert LineKind.NOTE not in kinds
    assert caplog.text == ""
    await harness.stop()


async def test_stop_aborts_the_turn(tmp_path: Path) -> None:
    harness = await build(tmp_path)
    harness.create()
    live = await harness.start_turn()
    body = await harness.service.stop_turn(
        OWUI, FAMILY, CHAT_SESSION, live.record.turn, "user_stopped", DOOR
    )
    await wait_until(lambda: bool(harness.fleet.playpen().aborts))

    assert body["state"] == TurnState.ABORTED.value
    assert harness.fleet.playpen().aborts[-1]["turn"] == live.record.turn
    assert LineKind.TURN_ABORTED in harness.kinds()
    await harness.stop()


async def test_delete_removes_the_session(tmp_path: Path) -> None:
    harness = await build(tmp_path)
    harness.create()
    await harness.full_turn()
    await harness.service.delete_session(OWUI, FAMILY, CHAT_SESSION)

    assert not harness.service.store.exists(FAMILY, CHAT_SESSION)

    with pytest.raises(ApiError) as caught:
        harness.service.get_session(OWUI, FAMILY, CHAT_SESSION, 10)

    assert caught.value.code is ErrorCode.NOT_FOUND
    await harness.stop()


async def test_delete_stops_a_pre_started_process(tmp_path: Path) -> None:
    """Contract 02 §5.8. A session with no turn still holds a pi process.

    `create_or_find` pre-starts one (contract 03 §4.7 rule 8), so a delete
    before the first prompt has a process to close. Nothing else would ever
    close it: the session is gone, so no reap rule can name it again.
    """
    harness = await build(tmp_path)
    harness.create()
    await wait_until(lambda: bool(harness.fleet.playpens))
    await wait_until(lambda: bool(harness.fleet.playpen().opens))
    await harness.service.delete_session(OWUI, FAMILY, CHAT_SESSION)
    await wait_until(lambda: bool(harness.fleet.playpen().stops))

    stopped = [str(stop["session"]) for stop in harness.fleet.playpen().stops]

    assert stopped == [CHAT_SESSION]
    await harness.stop()


async def test_a_persona_shapes_the_turn_and_grants_nothing(tmp_path: Path) -> None:
    harness = await build(tmp_path)
    harness.create()
    await harness.start_turn(persona="You answer as the house assistant.")
    sent = harness.fleet.playpen().started[-1]

    assert "You answer as the house assistant." in sent["persona"]
    assert "no authority" in sent["persona"]
    assert harness.service.get_session(OWUI, FAMILY, CHAT_SESSION, 0)["persona_hash"]
    await harness.stop()


async def test_an_oversized_persona_is_truncated_not_refused(tmp_path: Path) -> None:
    """Contract 02 §11 rule 6. A folder may shape, never break (invariant 7)."""
    harness = await build(tmp_path)
    harness.create()
    live = await harness.start_turn(persona="p" * (BODY_BUDGET_BYTES + 500))
    sent = harness.fleet.playpen().started[-1]

    assert len(sent["persona"].encode("utf-8")) <= MAX_PERSONA_BYTES
    assert live.record.persona_truncated is True
    assert LineKind.NOTE in harness.kinds()
    await harness.stop()


async def test_no_ready_sandbox_answers_sandbox_unavailable(tmp_path: Path) -> None:
    harness = await build(tmp_path, sandboxes=((SANDBOX, "failed"),))
    harness.create()

    with pytest.raises(ApiError) as caught:
        await harness.service.run_turn(
            OWUI, FAMILY, CHAT_SESSION, RunTurnRequest(prompt=PROMPT), DOOR
        )

    assert caught.value.code is ErrorCode.SANDBOX_UNAVAILABLE
    await harness.stop()


async def test_a_document_with_no_env_file_is_a_fault_not_a_guess(tmp_path: Path) -> None:
    """Contract 03 §7.1 and contract 05 §4.1. `sbx exec` forwards no host
    environment, so a command without `--env-file` starts a playpen that
    finds none of its mounts. Dialling anyway would bury that in a log
    file; the fault names the cause where an operator reads it."""
    harness = await build(tmp_path, playpen_env="")
    harness.create()

    with pytest.raises(ApiError) as caught:
        await harness.service.run_turn(
            OWUI, FAMILY, CHAT_SESSION, RunTurnRequest(prompt=PROMPT), DOOR
        )

    assert caught.value.code is ErrorCode.SANDBOX_UNAVAILABLE
    assert harness.faults() == ["sandbox_start_failed"]
    assert harness.fleet.dials == []
    await harness.stop()


async def test_the_env_file_path_reaches_the_channel(tmp_path: Path) -> None:
    """The path `caregiver` published is what `attendance` dials with."""
    harness = await build(tmp_path)
    harness.create()
    await harness.start_turn()

    assert harness.fleet.env_files == [PLAYPEN_ENV]
    await harness.stop()


async def test_a_cold_start_is_waited_out(tmp_path: Path) -> None:
    """Contract 02 §5.1. A cold start is not `sandbox_unavailable`."""
    harness = await build(tmp_path, sandboxes=((SANDBOX, "creating"),))
    harness.create()

    async def become_ready() -> None:
        await asyncio.sleep(0.05)
        write_status(harness.config.state_root, sandboxes=((SANDBOX, "ready"),))

    warming = asyncio.create_task(become_ready())
    live = await harness.service.run_turn(
        OWUI, FAMILY, CHAT_SESSION, RunTurnRequest(prompt=PROMPT), DOOR
    )
    await warming

    assert live.record.sandbox == SANDBOX
    await harness.stop()


async def test_a_never_valid_family_refuses(tmp_path: Path) -> None:
    harness = await build(tmp_path, state="invalid", never_valid=True)

    with pytest.raises(ApiError) as caught:
        harness.create()

    assert caught.value.code is ErrorCode.FAMILY_INVALID
    await harness.stop()


async def test_a_blocking_fault_refuses(tmp_path: Path) -> None:
    harness = await build(
        tmp_path,
        state="degraded",
        faults=({"code": "protocol_violation", "blocks_turns": True},),
    )

    with pytest.raises(ApiError) as caught:
        harness.create()

    assert caught.value.code is ErrorCode.FAMILY_DEGRADED
    await harness.stop()


async def test_an_unknown_family_refuses(tmp_path: Path) -> None:
    harness = await build(tmp_path)

    with pytest.raises(ApiError) as caught:
        harness.service.create_or_find(OWUI, CreateRequest(family="nosuch", session="owui-x"))

    assert caught.value.code is ErrorCode.FAMILY_UNKNOWN
    await harness.stop()


async def test_a_door_creates_only_its_own_prefix(tmp_path: Path) -> None:
    harness = await build(tmp_path)

    with pytest.raises(ApiError) as caught:
        harness.service.create_or_find(TUI, CreateRequest(family=FAMILY, session="owui-not-mine"))

    assert caught.value.code is ErrorCode.FORBIDDEN
    await harness.stop()


async def test_the_owui_map_is_filled_from_the_turn(tmp_path: Path) -> None:
    """Contract 02 §10.1. Two rows per settled turn, with no pi extension."""
    harness = await build(tmp_path)
    harness.create()
    refs = OwuiRefs(
        chat_id="3f2a9c41",
        message_id="assistant-1",
        user_message_id="user-1",
        parent_id=None,
    )
    live = await harness.start_turn(owui=refs)
    await harness.fleet.playpen().settle(CHAT_SESSION, live.record.turn)
    await settle_now(live.done)

    stored = harness.service.store.load(FAMILY, CHAT_SESSION)
    assert stored is not None
    assert stored.owui_map["user-1"] == "a1b2c3d4"
    assert stored.owui_map["assistant-1"] == "e5f6a7b8"
    await harness.stop()


async def test_a_turn_past_its_deadline_times_out(tmp_path: Path) -> None:
    harness = await build(tmp_path)
    harness.create()
    live = await harness.service.run_turn(
        OWUI,
        FAMILY,
        CHAT_SESSION,
        RunTurnRequest(prompt=PROMPT, deadline_s=1),
        DOOR,
    )
    await harness.fleet.playpen().next_start()
    await settle_now(live.done, timeout=3.0)

    assert live.record.state is TurnState.FAILED
    assert live.record.reason is TurnReason.TURN_TIMEOUT
    await harness.stop()


async def test_listing_pages_by_cursor(tmp_path: Path) -> None:
    harness = await build(tmp_path)

    for index in range(5):
        harness.create(f"owui-chat-{index}")

    first = harness.service.list_sessions(VIEW, ListQuery(limit=2))
    assert len(first["sessions"]) == 2
    assert first["next_cursor"] is not None

    second = harness.service.list_sessions(
        VIEW, ListQuery(limit=2, cursor=str(first["next_cursor"]))
    )
    assert len(second["sessions"]) == 2

    seen = {row["session"] for row in first["sessions"] + second["sessions"]}
    assert len(seen) == 4
    await harness.stop()


async def test_find_returns_the_existing_session(tmp_path: Path) -> None:
    harness = await build(tmp_path)
    _, created = harness.service.create_or_find(
        OWUI, CreateRequest(family=FAMILY, session=CHAT_SESSION, title="Kitchen debug")
    )
    body, again = harness.service.create_or_find(
        OWUI, CreateRequest(family=FAMILY, session=CHAT_SESSION, title="Ignored")
    )

    assert created is True
    assert again is False
    assert body["title"] == "Kitchen debug"
    await harness.stop()
