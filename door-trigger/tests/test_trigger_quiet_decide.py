"""`quiet/decide.py`: every reason a cron firing wakes its family (contract
01 §3.15), one rule per test. Pure, so no fake is needed."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agent_door_trigger.quiet.decide import (
    Daily,
    Jobs,
    Reason,
    Rule,
    Snapshot,
    Wake,
    Watch,
    reasons,
    wake_of,
)

FIRED = datetime(2026, 9, 25, 10, 0, tzinfo=UTC)
AN_HOUR_ON = FIRED + timedelta(hours=1)
FLOOR = timedelta(hours=24)
BOARD = "a" * 64
DONE = frozenset({"auto-OLD"})

RULE = Rule(floor=FLOOR, board=Watch.ON, jobs=Watch.ON)
GOOD = Wake(session="auto-GOOD", at=FIRED, board=BOARD, ended=DONE)
SETTLED = Jobs(live=frozenset(), ended=DONE)


def _seen(
    at: datetime = AN_HOUR_ON,
    board: str | None = BOARD,
    jobs: Jobs | None = SETTLED,
    daily: Daily = Daily.CLEAR,
) -> Snapshot:
    return Snapshot(at=at, board=board, jobs=jobs, daily=daily)


def test_nothing_changed_is_quiet() -> None:
    assert reasons(GOOD, _seen(), RULE) == ()


def test_no_good_wake_on_record_wakes() -> None:
    assert reasons(None, _seen(), RULE) == (Reason.NO_GOOD_WAKE,)


def test_the_floor_wakes_a_frozen_board() -> None:
    assert reasons(GOOD, _seen(at=FIRED + FLOOR), RULE) == (Reason.FLOOR,)


def test_an_hour_short_of_the_floor_is_quiet() -> None:
    assert reasons(GOOD, _seen(at=FIRED + FLOOR - timedelta(hours=1)), RULE) == ()


def test_an_owed_daily_call_wakes() -> None:
    assert reasons(GOOD, _seen(daily=Daily.OWED), RULE) == (Reason.OWED,)


def test_a_live_job_wakes() -> None:
    jobs = Jobs(live=frozenset({"auto-RUN"}), ended=DONE)
    assert reasons(GOOD, _seen(jobs=jobs), RULE) == (Reason.JOB_LIVE,)


def test_a_job_that_ended_since_the_good_wake_wakes() -> None:
    """It was live, or not yet started, when the gate last looked: the
    family never saw it end."""
    jobs = Jobs(live=frozenset(), ended=DONE | {"auto-NEW"})
    assert reasons(GOOD, _seen(jobs=jobs), RULE) == (Reason.JOB_ENDED,)


def test_a_job_that_fell_off_the_page_is_quiet() -> None:
    assert reasons(GOOD, _seen(jobs=Jobs(live=frozenset(), ended=frozenset())), RULE) == ()


def test_jobs_that_cannot_be_read_wake() -> None:
    assert reasons(GOOD, _seen(jobs=None), RULE) == (Reason.JOBS_UNREAD,)


def test_a_good_wake_with_no_job_list_wakes() -> None:
    unread = Wake(session="auto-GOOD", at=FIRED, board=BOARD, ended=None)
    assert reasons(unread, _seen(), RULE) == (Reason.JOBS_UNREAD,)


def test_a_family_that_starts_no_jobs_ignores_them() -> None:
    rule = Rule(floor=FLOOR, board=Watch.ON, jobs=Watch.OFF)
    assert reasons(GOOD, _seen(jobs=None), rule) == ()


def test_a_moved_board_wakes() -> None:
    assert reasons(GOOD, _seen(board="b" * 64), RULE) == (Reason.BOARD_MOVED,)


def test_a_board_that_cannot_be_read_wakes() -> None:
    assert reasons(GOOD, _seen(board=None), RULE) == (Reason.BOARD_UNREAD,)


def test_a_family_with_no_board_ignores_it() -> None:
    rule = Rule(floor=FLOOR, board=Watch.OFF, jobs=Watch.ON)
    assert reasons(GOOD, _seen(board=None), rule) == ()


def test_every_reason_is_named() -> None:
    jobs = Jobs(live=frozenset({"auto-RUN"}), ended=DONE)
    seen = _seen(at=FIRED + FLOOR, board=None, jobs=jobs, daily=Daily.OWED)

    assert reasons(GOOD, seen, RULE) == (
        Reason.FLOOR,
        Reason.OWED,
        Reason.JOB_LIVE,
        Reason.BOARD_UNREAD,
    )


def test_a_wake_keeps_what_the_gate_saw() -> None:
    kept = wake_of("auto-NEW", _seen())

    assert kept == Wake(session="auto-NEW", at=AN_HOUR_ON, board=BOARD, ended=DONE)
