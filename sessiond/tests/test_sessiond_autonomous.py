"""Autonomous semantics: the trigger, the turn limit and the queue.

An autonomous family is the only kind with any of the three (contract 02
§13). Everything here runs against a fake playpen, so no test needs
the host, `sbx` or a live `sessiond`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from agent_sessiond.auth import Principal
from agent_sessiond.branching import Branch, Decision
from agent_sessiond.config import Config
from agent_sessiond.errors import ApiError, ErrorCode, TurnReason
from agent_sessiond.models import JournalLine, LineKind, Trigger, TriggerKind, Turn
from agent_sessiond.outcomes import ERROR_MAX_BYTES, cut_error
from agent_sessiond.paths import outcome_file
from agent_sessiond.persona import prepare
from agent_sessiond.queueing import MAX_QUEUED_TURNS, TurnQueue, Waiting
from agent_sessiond.requests import CreateRequest, RunTurnRequest, read_run_turn
from agent_sessiond.service import SessionService
from agent_sessiond.states import TurnState
from agent_sessiond.turns import LiveTurn
from sessiond_harness import FakeFleet, make_config, settle_now, wait_until, write_status

TRIGGER_DOOR = Principal.DOOR_TRIGGER
OWUI_DOOR = Principal.DOOR_OWUI
AUTO_FAMILY = "scrum-lead"
AUTO_SANDBOX = "scrum-lead-s1"
AUTO_SESSION = "auto-01JBQ7ZZ9D6M0Q4RXT2J8HYVBK"
SECOND_SESSION = "auto-01JBQ80M4F7S2YQ1VZK6W3TDEN"
CHAT_FAMILY = "chat"
CHAT_SESSION = "owui-3f2a9c41-77b0-4a1e-9a4c-1d0e5f8b2c33"
DOOR = "trigger-1"
FIRED_AT = "2026-09-19T06:00:00Z"
PROMPT = "Triage yesterday's tickets."
MAX_QUEUED = MAX_QUEUED_TURNS


class AutoHarness:
    """One service serving one autonomous family and one attended family."""

    def __init__(self, config: Config, service: SessionService, fleet: FakeFleet) -> None:
        self.config = config
        self.service = service
        self.fleet = fleet

    def create(self, family: str = AUTO_FAMILY, session: str = AUTO_SESSION) -> None:
        door = TRIGGER_DOOR if family == AUTO_FAMILY else OWUI_DOOR
        self.service.create_or_find(door, CreateRequest(family=family, session=session))

    async def run(
        self,
        trigger: Trigger | None,
        family: str = AUTO_FAMILY,
        session: str = AUTO_SESSION,
    ) -> LiveTurn:
        door = TRIGGER_DOOR if family == AUTO_FAMILY else OWUI_DOOR
        live = await self.service.run_turn(
            door,
            family,
            session,
            RunTurnRequest(prompt=PROMPT, trigger=trigger),
            DOOR,
        )
        await self.fleet.playpen(_sandbox_of(family)).next_start()
        return live

    async def queue(self, session: str = SECOND_SESSION) -> LiveTurn:
        """A turn that finds no free slot. Nothing dials a sandbox for it."""
        return await self.queue_with(None, session)

    async def queue_with(self, trigger: Trigger | None, session: str = SECOND_SESSION) -> LiveTurn:
        return await self.service.run_turn(
            TRIGGER_DOOR,
            AUTO_FAMILY,
            session,
            RunTurnRequest(prompt=PROMPT, trigger=trigger),
            DOOR,
        )

    def lines(self, session: str) -> list[JournalLine]:
        return list(self.service.store.journal.replay(AUTO_FAMILY, session))

    def kinds(self, session: str) -> list[LineKind]:
        return [line.kind for line in self.lines(session)]

    async def settle(self, live: LiveTurn) -> None:
        playpen = self.fleet.playpen(_sandbox_of(live.record.family))
        await playpen.settle(live.record.session, live.record.turn)
        await settle_now(live.done)

    async def outcome(self, family: str = AUTO_FAMILY) -> dict[str, Any]:
        """The one record a finished job left (contract 02 §13.1)."""
        folder = outcome_file(self.config.state_root, family, "x").parent
        await wait_until(lambda: any(folder.glob("*.json")))
        return outcomes_in(folder)[0]

    def stored(self, family: str = AUTO_FAMILY, session: str = AUTO_SESSION) -> Trigger | None:
        record = self.service.store.load(family, session)

        return record.trigger if record is not None else None

    async def stop(self) -> None:
        await self.service.close()
        await self.fleet.stop()


async def build(tmp_path: Path, **limits: object) -> AutoHarness:
    config = make_config(tmp_path)
    write_status(config.state_root)
    write_status(
        config.state_root,
        family=AUTO_FAMILY,
        kind="autonomous",
        sandboxes=((AUTO_SANDBOX, "ready"),),
        **limits,  # pyright: ignore[reportArgumentType]
    )
    fleet = FakeFleet()
    service = SessionService(config, factory=fleet.factory, cold_start_wait_s=1.0)
    service.start()
    return AutoHarness(config, service, fleet)


def outcomes_in(folder: Path) -> list[dict[str, Any]]:
    """Every outcome record one family left, oldest first (its id is a ULID)."""
    found: list[dict[str, Any]] = []

    for path in sorted(folder.glob("*.json")):
        parsed: object = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(parsed, dict)
        found.append(parsed)  # pyright: ignore[reportUnknownArgumentType]

    return found


def outcomes_of(root: Path, session: str) -> dict[str, Any]:
    """The one record a named session left."""
    folder = outcome_file(root, AUTO_FAMILY, "x").parent
    found = [one for one in outcomes_in(folder) if one["session"] == session]
    assert len(found) == 1, f"{session} left {len(found)} records"
    return found[0]


def _sandbox_of(family: str) -> str:
    return AUTO_SANDBOX if family == AUTO_FAMILY else "chat-s1"


def _read(body: dict[str, object]) -> Trigger | None:
    return read_run_turn(body, AUTO_FAMILY, AUTO_SESSION).trigger


def _waiting() -> Waiting:
    """One queue entry, with no service behind it."""
    request = RunTurnRequest(prompt=PROMPT)
    turn = Turn(
        turn="01JBQ7WZ0X4T9V6K2H8M3N5PQR",
        session=AUTO_SESSION,
        family=AUTO_FAMILY,
        state=TurnState.QUEUED,
        started_at=datetime(2026, 9, 19, 6, 0, tzinfo=UTC),
        sandbox="",
    )
    return Waiting(
        live=LiveTurn(record=turn, prompt=PROMPT),
        request=request,
        persona=prepare(""),
        branch=Decision(Branch.PROMPT),
    )


# --------------------------------------------------------------- the request


def test_the_trigger_object_is_read() -> None:
    """Contract 02 §13.2's field table."""
    found = _read(
        {
            "prompt": PROMPT,
            "trigger": {"kind": "webhook", "name": "boiler-alert", "fired_at": FIRED_AT},
        }
    )

    assert found is not None
    assert found.kind is TriggerKind.WEBHOOK
    assert found.name == "boiler-alert"
    assert found.fired_at == datetime(2026, 9, 19, 6, 0, tzinfo=UTC)


def test_the_three_labels_still_work() -> None:
    """Contract 02 §13.2 rule 2. The trigger door wrote these before §13.2."""
    found = _read(
        {
            "prompt": PROMPT,
            "labels": {
                "trigger_kind": "timer",
                "trigger_name": "morning-triage",
                "trigger_fired_at": FIRED_AT,
            },
        }
    )

    assert found is not None
    assert found.kind is TriggerKind.TIMER
    assert found.name == "morning-triage"


def test_the_object_wins_over_the_labels() -> None:
    """Contract 02 §13.2 rule 2's last sentence."""
    found = _read(
        {
            "prompt": PROMPT,
            "trigger": {"kind": "webhook", "name": "boiler-alert"},
            "labels": {"trigger_kind": "timer", "trigger_name": "morning-triage"},
        }
    )

    assert found is not None
    assert found.kind is TriggerKind.WEBHOOK
    assert found.name == "boiler-alert"


def test_a_turn_with_no_trigger_carries_none() -> None:
    assert _read({"prompt": PROMPT}) is None


@pytest.mark.parametrize(
    "trigger",
    [
        {"name": "boiler-alert"},
        {"kind": "cron"},
        {"kind": "timer", "fired_at": "yesterday morning"},
    ],
)
def test_a_malformed_trigger_is_refused(trigger: dict[str, str]) -> None:
    """Contract 02 §13.2 rule 4. It reaches a durable record (invariant 14)."""
    with pytest.raises(ApiError) as caught:
        _read({"prompt": PROMPT, "trigger": trigger})

    assert caught.value.code is ErrorCode.BAD_REQUEST


def test_a_malformed_label_is_refused_too() -> None:
    with pytest.raises(ApiError) as caught:
        _read({"prompt": PROMPT, "labels": {"trigger_kind": "cron"}})

    assert caught.value.code is ErrorCode.BAD_REQUEST


# --------------------------------------------------------------- the session


async def test_the_first_turn_fixes_the_trigger(tmp_path: Path) -> None:
    """Contract 02 §13.2 rule 1. One session is one job is one firing."""
    harness = await build(tmp_path)
    harness.create()

    await harness.run(Trigger(kind=TriggerKind.TIMER, name="morning-triage"))
    first = harness.stored()

    assert first is not None
    assert first.name == "morning-triage"
    await harness.stop()


async def test_a_later_turns_trigger_is_ignored(tmp_path: Path) -> None:
    """Contract 02 §13.2 rule 1.

    Two queued turns on one session is the only way to reach a second turn
    of an autonomous job: a running one refuses a second (§5.6), and a
    settled one deletes the session (§13 rule 7).
    """
    harness = await build(tmp_path, max_running_turns=1)
    harness.create()
    harness.create(session=SECOND_SESSION)
    await harness.run(None)

    await harness.queue_with(Trigger(kind=TriggerKind.TIMER, name="morning-triage"))
    await harness.queue_with(Trigger(kind=TriggerKind.WEBHOOK, name="boiler-alert"))
    found = harness.stored(session=SECOND_SESSION)

    assert found is not None
    assert found.name == "morning-triage"
    await harness.stop()


async def test_an_attended_turn_carries_no_trigger(tmp_path: Path) -> None:
    """Contract 02 §13.2 rule 3."""
    harness = await build(tmp_path)
    harness.create(CHAT_FAMILY, CHAT_SESSION)

    with pytest.raises(ApiError) as caught:
        await harness.run(Trigger(kind=TriggerKind.TIMER), family=CHAT_FAMILY, session=CHAT_SESSION)

    assert caught.value.code is ErrorCode.BAD_REQUEST
    await harness.stop()


async def test_the_trigger_survives_a_restart(tmp_path: Path) -> None:
    """The outcome record names it after the session is gone (§13.1).

    A restart fails the turn that was running, which ends the job, so the
    trigger has to have reached disk before this service went down.
    """
    config = make_config(tmp_path)
    harness = await build(tmp_path)
    harness.create()
    await harness.run(Trigger(kind=TriggerKind.WEBHOOK, name="boiler-alert"))
    await harness.stop()

    reread = SessionService(config)
    reread.start()
    found = outcomes_of(config.state_root, AUTO_SESSION)

    assert found["trigger"]["kind"] == "webhook"
    assert found["trigger"]["name"] == "boiler-alert"
    await reread.close()


# ----------------------------------------------------------------- the queue


async def test_a_second_turn_queues_at_the_limit(tmp_path: Path) -> None:
    """Contract 02 §13 rules 2 and 3. The limit counts the whole family."""
    harness = await build(tmp_path, max_running_turns=1)
    harness.create()
    harness.create(session=SECOND_SESSION)

    await harness.run(None)
    queued = await harness.queue(SECOND_SESSION)

    assert queued.record.state is TurnState.QUEUED
    assert queued.record.sandbox == ""
    assert harness.kinds(SECOND_SESSION)[-1] is LineKind.TURN_QUEUED
    assert LineKind.TURN_STARTED not in harness.kinds(SECOND_SESSION)
    await harness.stop()


async def test_the_queued_line_carries_the_prompt(tmp_path: Path) -> None:
    """Contract 02 §8.1 and §8.2. The prompt is on disk before the FIFO."""
    harness = await build(tmp_path, max_running_turns=1)
    harness.create()
    harness.create(session=SECOND_SESSION)
    await harness.run(None)

    await harness.queue(SECOND_SESSION)
    body = harness.lines(SECOND_SESSION)[-1].body

    assert body["prompt"] == PROMPT
    assert body["queue_depth"] == 1
    await harness.stop()


async def test_a_queued_turn_starts_when_a_slot_frees(tmp_path: Path) -> None:
    """Contract 02 §13 rule 3."""
    harness = await build(tmp_path, max_running_turns=1)
    harness.create()
    harness.create(session=SECOND_SESSION)
    first = await harness.run(None)
    queued = await harness.queue(SECOND_SESSION)

    await harness.settle(first)
    await harness.fleet.playpen(AUTO_SANDBOX).next_start()

    assert queued.record.state is TurnState.RUNNING
    assert queued.record.sandbox == AUTO_SANDBOX
    await harness.stop()


async def test_an_attended_family_never_queues(tmp_path: Path) -> None:
    """Contract 02 §13 rule 5. A human at a keyboard waits for nothing."""
    harness = await build(tmp_path)
    harness.create(CHAT_FAMILY, CHAT_SESSION)

    live = await harness.run(None, family=CHAT_FAMILY, session=CHAT_SESSION)

    assert live.record.state is TurnState.RUNNING
    await harness.stop()


async def test_the_queue_refuses_past_its_cap(tmp_path: Path) -> None:
    """Contract 02 §13 rule 4. A runaway timer grows no unbounded queue."""
    harness = await build(tmp_path, max_running_turns=1)
    harness.create()
    await harness.run(None)

    for index in range(MAX_QUEUED):
        session = f"auto-01JBQ7ZZ9D6M0Q4RXT2J8HYV{index:02d}"
        harness.create(session=session)
        await harness.queue(session)

    harness.create(session=SECOND_SESSION)

    with pytest.raises(ApiError) as caught:
        await harness.service.run_turn(
            TRIGGER_DOOR,
            AUTO_FAMILY,
            SECOND_SESSION,
            RunTurnRequest(prompt=PROMPT),
            DOOR,
        )

    assert caught.value.code is ErrorCode.QUEUE_FULL
    assert caught.value.body()["retry_after_s"]
    await harness.stop()


async def test_a_refused_turn_leaves_no_record(tmp_path: Path) -> None:
    """`check_room` runs before the turn is minted."""
    queue = TurnQueue(max_queued=1)
    queue.add(AUTO_FAMILY, _waiting())

    with pytest.raises(ApiError):
        queue.check_room(AUTO_FAMILY)

    assert queue.depth(AUTO_FAMILY) == 1


async def test_a_restart_aborts_the_queue(tmp_path: Path) -> None:
    """Contract 02 §13.3 and §4.3. `queue_lost`, never `internal`.

    That abort ends the job (§13 rule 7), so what the reason reaches is
    the outcome record rather than a turn file nobody will open again.
    """
    config = make_config(tmp_path)
    harness = await build(tmp_path, max_running_turns=1)
    harness.create()
    harness.create(session=SECOND_SESSION)
    await harness.run(None)
    await harness.queue(SECOND_SESSION)
    await harness.stop()

    reread = SessionService(config)
    reread.start()
    found = outcomes_of(config.state_root, SECOND_SESSION)

    assert found["status"] == "cancelled"
    assert found["error"] == TurnReason.QUEUE_LOST.value
    await reread.close()


async def test_a_stopped_turn_leaves_the_queue(tmp_path: Path) -> None:
    """A stopped turn never reaches a slot, and never holds one either."""
    harness = await build(tmp_path, max_running_turns=1)
    harness.create()
    harness.create(session=SECOND_SESSION)
    await harness.run(None)
    queued = await harness.queue(SECOND_SESSION)

    await harness.service.stop_turn(
        TRIGGER_DOOR, AUTO_FAMILY, SECOND_SESSION, queued.record.turn, "no longer wanted", DOOR
    )

    assert queued.record.state is TurnState.ABORTED
    assert harness.service.queue_depth(AUTO_FAMILY) == 0
    await harness.stop()


async def test_a_queued_turn_with_no_env_file_ends_the_job(tmp_path: Path) -> None:
    """Contract 05 §4.1 and sessiond/AGENTS.md mount rule 2, on the queue.

    The document loses its env file while the turn waits. Nothing awaits
    the start, so a refusal that escaped would leave the turn `running`
    for ever and the job without an outcome record.
    """
    harness = await build(tmp_path, max_running_turns=1)
    harness.create()
    harness.create(session=SECOND_SESSION)
    first = await harness.run(None)
    queued = await harness.queue(SECOND_SESSION)
    write_status(
        harness.config.state_root,
        family=AUTO_FAMILY,
        kind="autonomous",
        sandboxes=((AUTO_SANDBOX, "ready"),),
        max_running_turns=1,
        playpen_env="",
    )

    await harness.settle(first)
    await settle_now(queued.done)
    folder = outcome_file(harness.config.state_root, AUTO_FAMILY, "x").parent
    await wait_until(lambda: len(outcomes_in(folder)) == 2)

    assert queued.record.state is TurnState.FAILED
    assert queued.record.reason is TurnReason.SANDBOX_LOST
    assert outcomes_of(harness.config.state_root, SECOND_SESSION)["status"] == "failed"
    await harness.stop()


async def test_a_queued_turn_with_no_sandbox_ends_the_job(tmp_path: Path) -> None:
    """Contract 02 §4.3 and §13 rule 7. The family has nothing to dial.

    The sandbox that served the first turn is draining when the slot
    frees, so no sandbox can take the queued one. It must end `failed`,
    not stay `queued` out of the FIFO with its job never ending.
    """
    harness = await build(tmp_path, max_running_turns=1)
    harness.create()
    harness.create(session=SECOND_SESSION)
    first = await harness.run(None)
    queued = await harness.queue(SECOND_SESSION)
    write_status(
        harness.config.state_root,
        family=AUTO_FAMILY,
        kind="autonomous",
        sandboxes=((AUTO_SANDBOX, "draining"),),
        max_running_turns=1,
    )

    await harness.settle(first)
    await settle_now(queued.done)
    folder = outcome_file(harness.config.state_root, AUTO_FAMILY, "x").parent
    await wait_until(lambda: len(outcomes_in(folder)) == 2)
    record = outcomes_of(harness.config.state_root, SECOND_SESSION)

    assert queued.record.state is TurnState.FAILED
    assert queued.record.reason is TurnReason.SANDBOX_LOST
    assert queued.record.sandbox == ""
    assert record["status"] == "failed"
    assert "no ready sandbox" in record["error"]
    await harness.stop()


# --------------------------------------------------------- the outcome record


async def test_a_finished_job_leaves_a_record(tmp_path: Path) -> None:
    """Contract 02 §13 rule 7 and §13.1. The session goes, the record stays."""
    harness = await build(tmp_path)
    harness.create()
    live = await harness.run(Trigger(kind=TriggerKind.TIMER, name="morning-triage"))

    await harness.settle(live)
    record = await harness.outcome()

    assert record["session"] == AUTO_SESSION
    assert record["family"] == AUTO_FAMILY
    assert record["status"] == "ok"
    assert record["error"] is None
    assert record["turns"] == 1
    assert record["sandbox"] == AUTO_SANDBOX
    assert record["trigger"]["name"] == "morning-triage"
    assert record["spend_usd"] > 0

    # The delete runs after the turn that asked for it has settled.
    await wait_until(lambda: harness.service.store.load(AUTO_FAMILY, AUTO_SESSION) is None)
    await harness.stop()


async def test_the_record_names_a_failure(tmp_path: Path) -> None:
    """Contract 02 §13.1's `status` and `error`."""
    harness = await build(tmp_path)
    harness.create()
    live = await harness.run(None)

    await harness.fleet.playpen(AUTO_SANDBOX).fail(
        AUTO_SESSION, live.record.turn, "model_error", "the model refused"
    )
    await settle_now(live.done)
    record = await harness.outcome()

    assert record["status"] == "failed"
    assert record["error"] == "the model refused"
    await harness.stop()


async def test_a_stopped_job_reads_as_cancelled(tmp_path: Path) -> None:
    harness = await build(tmp_path)
    harness.create()
    live = await harness.run(None)

    await harness.service.stop_turn(
        TRIGGER_DOOR, AUTO_FAMILY, AUTO_SESSION, live.record.turn, "no longer wanted", DOOR
    )
    record = await harness.outcome()

    assert record["status"] == "cancelled"
    await harness.stop()


async def test_a_queued_turn_holds_the_job_open(tmp_path: Path) -> None:
    """§13 rule 7. The job ends when no turn of it is `queued` either."""
    harness = await build(tmp_path, max_running_turns=1)
    harness.create()
    harness.create(session=SECOND_SESSION)
    first = await harness.run(None)
    await harness.queue()

    await harness.settle(first)

    assert harness.service.store.load(AUTO_FAMILY, SECOND_SESSION) is not None
    await harness.stop()


async def test_an_attended_session_is_never_deleted(tmp_path: Path) -> None:
    """Contract 02 §13 rule 7 is autonomous only. Invariant 1 holds elsewhere."""
    harness = await build(tmp_path)
    harness.create(CHAT_FAMILY, CHAT_SESSION)
    live = await harness.run(None, family=CHAT_FAMILY, session=CHAT_SESSION)

    await harness.settle(live)

    assert harness.service.store.load(CHAT_FAMILY, CHAT_SESSION) is not None
    await harness.stop()


def test_a_long_error_is_cut() -> None:
    """Contract 02 §13.1. `error` is at most 4 KiB, and a model wrote it."""
    cut = cut_error("e" * (ERROR_MAX_BYTES * 2))

    assert cut is not None
    assert len(cut.encode("utf-8")) == ERROR_MAX_BYTES


async def test_a_restart_ends_a_job_it_missed(tmp_path: Path) -> None:
    """Contract 02 §13 rule 7 across a restart.

    The last turn was running when this service went down, so nothing ever
    settles it again. `_recover_turns` fails it and the sweep ends the job.
    """
    harness = await build(tmp_path)
    harness.create()
    await harness.run(Trigger(kind=TriggerKind.TIMER, name="morning-triage"))
    await harness.stop()

    reread = SessionService(make_config(tmp_path))
    reread.start()
    record = outcomes_of(make_config(tmp_path).state_root, AUTO_SESSION)

    assert record["status"] == "failed"
    assert record["error"] == TurnReason.CHANNEL_LOST.value
    assert record["trigger"]["name"] == "morning-triage"
    assert reread.store.load(AUTO_FAMILY, AUTO_SESSION) is None
    await reread.close()


async def test_a_restart_keeps_an_unfinished_job(tmp_path: Path) -> None:
    """A queued turn holds the job open across a restart too — until §13.3
    aborts it, which is itself the end of the job."""
    harness = await build(tmp_path, max_running_turns=1)
    harness.create()
    harness.create(session=SECOND_SESSION)
    await harness.run(None)
    await harness.queue()
    await harness.stop()

    reread = SessionService(make_config(tmp_path))
    reread.start()

    assert reread.store.load(AUTO_FAMILY, SECOND_SESSION) is None
    await reread.close()


# ----------------------------------------------------------- the PEP is away


def _pep_unreachable() -> dict[str, Any]:
    """Contract 05 §3.3's fault, as `managerd` writes it. It blocks nothing."""
    return {
        "code": "pep_unreachable",
        "blocks_turns": False,
        "since": FIRED_AT,
        "source": "managerd",
    }


def _set_faults(harness: AutoHarness, faults: tuple[dict[str, Any], ...]) -> None:
    write_status(
        harness.config.state_root,
        family=AUTO_FAMILY,
        kind="autonomous",
        sandboxes=((AUTO_SANDBOX, "ready"),),
        faults=faults,
    )


async def _fire(harness: AutoHarness, name: str | None, session: str = AUTO_SESSION) -> LiveTurn:
    """One trigger firing. Nothing waits for a sandbox: that is the point."""
    trigger = None if name is None else Trigger(kind=TriggerKind.TIMER, name=name)

    return await harness.service.run_turn(
        TRIGGER_DOOR,
        AUTO_FAMILY,
        session,
        RunTurnRequest(prompt=PROMPT, trigger=trigger),
        DOOR,
    )


async def test_a_firing_during_the_outage_dials_nothing(tmp_path: Path) -> None:
    """Contract 05 §3.3 rule 6. The cost it names is not paid here.

    A cron firing is a session whose whole purpose is tool calls. During the
    outage every one of them fails, so the turn would run to the end on text
    alone and LiteLLM would bill it.
    """
    harness = await build(tmp_path)
    _set_faults(harness, (_pep_unreachable(),))
    harness.create()

    live = await _fire(harness, "triage")

    assert live.record.state is TurnState.FAILED
    # Contract 02 §14. The word names the fact: nobody removed a
    # permission, the PEP stopped answering.
    assert live.record.reason is TurnReason.PEP_UNREACHABLE
    assert harness.fleet.playpens == {}
    await harness.stop()


async def test_the_refused_firing_leaves_a_reason(tmp_path: Path) -> None:
    """Contract 02 §13.1. The one view shows a record, never a silence."""
    harness = await build(tmp_path)
    _set_faults(harness, (_pep_unreachable(),))
    harness.create()

    await _fire(harness, "triage")
    record = await harness.outcome()

    assert record["status"] == "failed"
    assert "policy service" in record["error"]
    assert record["trigger"]["name"] == "triage"
    assert record["spend_usd"] == 0
    await harness.stop()


async def test_the_journal_names_the_reason_by_its_own_word(tmp_path: Path) -> None:
    """Contract 02 §14. One word, one meaning.

    A reader who meets `permission_removed` goes looking for a grant that
    somebody took away. Nobody took anything: the PEP stopped answering.
    """
    harness = await build(tmp_path)
    _set_faults(harness, (_pep_unreachable(),))
    harness.create()

    await _fire(harness, "triage")
    failures = [
        line.body for line in harness.lines(AUTO_SESSION) if line.kind is LineKind.TURN_FAILED
    ]

    assert [body["reason"] for body in failures] == [TurnReason.PEP_UNREACHABLE.value]
    assert TurnReason.PERMISSION_REMOVED.value not in str(failures)
    await harness.stop()


async def test_an_attended_turn_still_runs_during_the_outage(tmp_path: Path) -> None:
    """`blocks_turns: false` is the contract's own word. A person asked."""
    harness = await build(tmp_path)
    write_status(harness.config.state_root, family=CHAT_FAMILY, faults=(_pep_unreachable(),))
    harness.create(family=CHAT_FAMILY, session=CHAT_SESSION)

    live = await harness.run(None, family=CHAT_FAMILY, session=CHAT_SESSION)

    assert live.record.state is TurnState.RUNNING
    await harness.stop()


async def test_the_next_firing_runs_once_the_pep_answers(tmp_path: Path) -> None:
    """Contract 05 §3.3 rule 5. `managerd` clears the fault by itself."""
    harness = await build(tmp_path)
    _set_faults(harness, (_pep_unreachable(),))
    harness.create()
    await _fire(harness, "triage")

    _set_faults(harness, ())
    harness.create(session=SECOND_SESSION)
    live = await harness.run(None, session=SECOND_SESSION)

    assert live.record.state is TurnState.RUNNING
    await harness.stop()
