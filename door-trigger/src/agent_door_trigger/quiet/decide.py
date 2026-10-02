"""Every reason a cron firing should wake its family (contract 01 §3.15).

Pure: what the gate saw now, against what it saw just before the last good
wake. No clock, no disk, no network, so each rule is one test away."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum


class Watch(StrEnum):
    """Whether one signal counts for this family."""

    ON = "on"
    OFF = "off"


class Daily(StrEnum):
    """Whether the family's daily call is owed right now."""

    OWED = "owed"
    CLEAR = "clear"


class Reason(StrEnum):
    """One reason to wake. The value is what the journal line says."""

    NO_GOOD_WAKE = "no good wake on record"
    FLOOR = "floor_hours passed since the last good wake"
    OWED = "the daily call is owed"
    JOBS_UNREAD = "jobs unreadable"
    JOB_LIVE = "a job is live"
    JOB_ENDED = "a job ended since the last good wake"
    BOARD_UNREAD = "board unreadable"
    BOARD_MOVED = "the board moved"


@dataclass(frozen=True)
class Rule:
    """What the family file asks the gate to watch."""

    floor: timedelta
    board: Watch
    jobs: Watch


@dataclass(frozen=True)
class Jobs:
    """The jobs this family enqueued, as `job_status` answered."""

    live: frozenset[str]
    ended: frozenset[str]


@dataclass(frozen=True)
class Snapshot:
    """What the gate read just before a firing. None: that read failed."""

    at: datetime
    board: str | None
    jobs: Jobs | None
    daily: Daily


@dataclass(frozen=True)
class Wake:
    """A firing the gate let through, with what it saw first."""

    session: str
    at: datetime
    board: str | None
    ended: frozenset[str] | None


def reasons(good: Wake | None, seen: Snapshot, rule: Rule) -> tuple[Reason, ...]:
    """Every reason to wake. Empty means quiet."""
    if good is None:
        return (Reason.NO_GOOD_WAKE,)

    found: list[Reason] = []
    if seen.at - good.at >= rule.floor:
        found.append(Reason.FLOOR)

    if seen.daily is Daily.OWED:
        found.append(Reason.OWED)

    if rule.jobs is Watch.ON:
        found.extend(_jobs(good, seen))

    if rule.board is Watch.ON:
        found.extend(_board(good, seen))

    return tuple(found)


def wake_of(session: str, seen: Snapshot) -> Wake:
    """What the gate keeps about a firing it let through."""
    ended = seen.jobs.ended if seen.jobs is not None else None
    return Wake(session=session, at=seen.at, board=seen.board, ended=ended)


def _jobs(good: Wake, seen: Snapshot) -> tuple[Reason, ...]:
    """A job the family started is work until it has ended AND the family
    has woken since. One that ended after the last good wake fired was never
    seen ended."""
    if seen.jobs is None or good.ended is None:
        return (Reason.JOBS_UNREAD,)

    if seen.jobs.live:
        return (Reason.JOB_LIVE,)

    if seen.jobs.ended - good.ended:
        return (Reason.JOB_ENDED,)

    return ()


def _board(good: Wake, seen: Snapshot) -> tuple[Reason, ...]:
    if seen.board is None or good.board is None:
        return (Reason.BOARD_UNREAD,)

    if seen.board != good.board:
        return (Reason.BOARD_MOVED,)

    return ()
