"""`quiet/gate.py`: one cron firing's check, end to end over fakes
(contract 01 §3.15). Each test walks the gate the way the hourly timer
does: check, fire or not, record, and the clock moves on."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_door_trigger.quiet.decide import Jobs, Reason, Wake
from agent_door_trigger.quiet.gate import Busy, gated_family
from agent_door_trigger.quiet.records import Called, Ending
from agent_door_trigger.quiet.state import GateState
from trigger_quiet_fakes import BOARD, DONE, FAMILY, FAMILY_YAML, FRIDAY_MORNING, Rig

AN_HOUR = timedelta(hours=1)
#: Friday 16:30 and Saturday 16:30 in Phoenix.
FRIDAY_AFTERNOON = datetime(2026, 9, 25, 23, 30, tzinfo=UTC)
SATURDAY_AFTERNOON = datetime(2026, 9, 26, 23, 30, tzinfo=UTC)


def _woken(rig: Rig, session: str = "auto-FIRST") -> None:
    """One firing the gate let through, as `cli.py` records it."""
    check = rig.gate().check()
    assert not check.quiet, check.summary()
    rig.gate().record(session, check)


def _good(rig: Rig) -> None:
    """A wake that ended `ok`, an hour on."""
    _woken(rig)
    rig.records.endings["auto-FIRST"] = Ending.OK
    rig.now += AN_HOUR


def test_the_first_firing_wakes_and_keeps_what_it_saw() -> None:
    rig = Rig()

    check = rig.gate().check()
    rig.gate().record("auto-FIRST", check)

    assert check.reasons == (Reason.NO_GOOD_WAKE,)
    assert rig.state == GateState(
        fired=Wake(session="auto-FIRST", at=FRIDAY_MORNING, board=BOARD, ended=DONE)
    )


def test_nothing_changed_after_a_good_wake_is_quiet() -> None:
    rig = Rig()
    _good(rig)

    check = rig.gate().check()

    assert check.quiet
    assert check.summary() == "nothing changed since 2026-09-25T17:00:00+00:00"
    assert rig.state.fired is None
    assert rig.state.good is not None
    assert rig.state.good.session == "auto-FIRST"


def test_a_moved_board_wakes() -> None:
    rig = Rig()
    _good(rig)
    rig.reads.board_value = "b" * 64

    assert rig.gate().check().reasons == (Reason.BOARD_MOVED,)


def test_a_job_that_ended_since_wakes() -> None:
    rig = Rig()
    _good(rig)
    rig.reads.jobs_value = Jobs(live=frozenset(), ended=DONE | {"auto-NEW"})

    assert rig.gate().check().reasons == (Reason.JOB_ENDED,)


def test_a_failed_wake_leaves_the_older_good_one() -> None:
    """Whatever woke the failed one wakes the next firing too."""
    rig = Rig()
    _good(rig)
    rig.reads.board_value = "b" * 64
    _woken(rig, "auto-SECOND")
    rig.records.endings["auto-SECOND"] = Ending.FAILED
    rig.now += AN_HOUR

    check = rig.gate().check()

    assert check.reasons == (Reason.BOARD_MOVED,)
    assert rig.state.good is not None
    assert rig.state.good.session == "auto-FIRST"
    assert rig.state.fired is None


def test_a_live_wake_skips_the_firing() -> None:
    rig = Rig()
    _woken(rig)
    rig.sessions.live = True
    rig.now += AN_HOUR

    check = rig.gate().check()

    assert check.quiet
    assert check.busy is Busy.YES
    assert check.summary() == "a wake is still live"
    assert rig.reads.asked == ["board board-lead", "jobs"]  # the first firing's two, no more
    assert rig.state.fired is not None


def test_a_live_wake_past_the_floor_fires_anyway() -> None:
    rig = Rig()
    _good(rig)
    rig.sessions.live = True
    rig.now = FRIDAY_MORNING + timedelta(hours=24)

    assert Reason.FLOOR in rig.gate().check().reasons


def test_a_wake_that_vanished_is_dropped() -> None:
    """No record and no live session: `attendance` writes the record before
    it deletes the session, so this job ended without one."""
    rig = Rig()
    _good(rig)
    rig.reads.board_value = "b" * 64
    _woken(rig, "auto-SECOND")
    rig.now += AN_HOUR

    rig.gate().check()

    assert rig.state.fired is None
    assert rig.state.good is not None
    assert rig.state.good.session == "auto-FIRST"


def test_an_unknown_attendance_keeps_the_last_firing() -> None:
    rig = Rig()
    _woken(rig)
    rig.sessions.live = None
    rig.now += AN_HOUR

    rig.gate().check()

    assert rig.state.fired is not None


def test_the_daily_call_is_owed_from_its_hour() -> None:
    rig = Rig()
    _good(rig)
    rig.now = FRIDAY_AFTERNOON

    assert rig.gate().check().reasons == (Reason.OWED,)
    family, call, since, until = rig.records.called_asks[-1]
    assert (family, call) == (FAMILY, "mail__send_standup_email")
    assert since == datetime(2026, 9, 25, 7, 0, tzinfo=UTC)  # Phoenix's midnight
    assert until == FRIDAY_AFTERNOON


def test_a_made_daily_call_is_not_owed() -> None:
    rig = Rig()
    _good(rig)
    rig.now = FRIDAY_AFTERNOON
    rig.records.called_value = Called.YES

    assert rig.gate().check().quiet


def test_an_audit_it_cannot_read_owes_the_call() -> None:
    rig = Rig()
    _good(rig)
    rig.now = FRIDAY_AFTERNOON
    rig.records.called_value = Called.UNKNOWN

    assert rig.gate().check().reasons == (Reason.OWED,)


def test_a_weekend_owes_nothing() -> None:
    rig = Rig()
    _good(rig)
    rig.now = SATURDAY_AFTERNOON
    rig.records.endings["auto-FIRST"] = Ending.OK

    check = rig.gate().check()

    assert Reason.OWED not in check.reasons
    assert rig.records.called_asks == []


def test_a_family_that_starts_no_jobs_never_reads_them() -> None:
    rig = Rig()
    text = FAMILY_YAML.replace(
        "verbs:\n  enqueue: { targets: [finance-worker] }\n  job_status: {}\n", ""
    )

    rig.gate(text).check()

    assert "jobs" not in rig.reads.asked


def test_a_busy_check_records_nothing() -> None:
    rig = Rig()
    _woken(rig)
    rig.sessions.live = True
    rig.now += AN_HOUR
    before = rig.state

    gate = rig.gate()
    gate.record("auto-NEVER", gate.check())

    assert rig.state == before


def test_a_record_that_will_not_write_never_stops_the_firing() -> None:
    rig = Rig()
    rig.store.broken = True

    gate = rig.gate()
    check = gate.check()
    gate.record("auto-FIRST", check)

    assert check.reasons == (Reason.NO_GOOD_WAKE,)


# --- gated_family: which files carry a check ---


def _write(registry: Path, text: str) -> None:
    directory = registry / "families" / FAMILY
    directory.mkdir(parents=True)
    (directory / "family.yaml").write_text(text, encoding="utf-8")


QUIET_ONLY = """\
name: scrum-lead
kind: autonomous
description: Test fixture family.

model: { router: agent-router, budget_usd_per_day: 10 }

triggers:
  - cron: "@hourly"

quiet: { floor_hours: 12 }
"""


def test_a_valid_file_with_quiet_is_gated(tmp_path: Path) -> None:
    _write(tmp_path, QUIET_ONLY)

    gated = gated_family(tmp_path, FAMILY)

    assert gated is not None
    assert gated[1].floor_hours == 12


def test_a_file_with_no_quiet_is_not_gated(tmp_path: Path) -> None:
    _write(tmp_path, QUIET_ONLY.replace("quiet: { floor_hours: 12 }\n", ""))

    assert gated_family(tmp_path, FAMILY) is None


def test_an_invalid_file_is_not_gated(tmp_path: Path) -> None:
    """`caregiver` serves the last good definition. A check read from the
    refused one is not one to trust, so the firing goes ahead."""
    _write(tmp_path, QUIET_ONLY.replace("floor_hours: 12", "floor_hours: 0"))

    assert gated_family(tmp_path, FAMILY) is None


def test_an_unknown_family_is_not_gated(tmp_path: Path) -> None:
    assert gated_family(tmp_path, FAMILY) is None
