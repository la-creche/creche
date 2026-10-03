"""One cron firing's quiet check (contract 01 §3.15), before any session
exists. `cli.py` asks `check()`, fires only when the answer has a reason,
and hands the new session to `record()`."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol
from zoneinfo import ZoneInfo

from agent_family import FamilyFile, QuietBlock, Verb, load_registry

from .decide import Daily, Reason, Rule, Snapshot, Watch, reasons, wake_of
from .pep import FamilyReads
from .records import Called, Ending, Records
from .state import GateState, StateStore

_LOG = logging.getLogger(__name__)

#: `datetime.weekday()` numbers Monday 0, so these two are the weekend.
SATURDAY: Final = 5


class Busy(StrEnum):
    """Whether a wake of the family was still live when the gate looked."""

    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"


class LiveSessions(Protocol):
    """What the gate asks `attendance`."""

    def any_live(self, family: str) -> bool | None:
        """Whether any session of the family is live. None: unknown."""
        ...


@dataclass(frozen=True)
class Check:
    """One check's answer. No reason is quiet."""

    reasons: tuple[Reason, ...]
    seen: Snapshot | None
    state: GateState
    busy: Busy = Busy.NO

    @property
    def quiet(self) -> bool:
        return not self.reasons

    def summary(self) -> str:
        """The journal's one line about this check."""
        if self.reasons:
            return "; ".join(self.reasons)

        if self.busy is Busy.YES:
            return "a wake is still live"

        good = self.state.good
        since = good.at.isoformat(timespec="seconds") if good is not None else "never"
        return f"nothing changed since {since}"


class QuietGate:
    """The check for one family whose file carries `quiet:`."""

    def __init__(
        self,
        family: FamilyFile,
        quiet: QuietBlock,
        *,
        reads: FamilyReads,
        records: Records,
        store: StateStore,
        sessions: LiveSessions,
        clock: Callable[[], datetime],
    ) -> None:
        self._name = family.name
        self._quiet = quiet
        self._rule = _rule_of(family, quiet)
        self._reads = reads
        self._records = records
        self._store = store
        self._sessions = sessions
        self._clock = clock

    def check(self) -> Check:
        now = self._clock()

        # Live first, then the record: `attendance` writes the record BEFORE
        # it deletes the session, so this order sees one or the other.
        busy = _busy_of(self._sessions.any_live(self._name))
        state = self._settle(self._store.read(self._name), busy)

        # A second wake would only queue behind the live one (§3.15 rule 3).
        # The floor still bounds the skip, from the latest firing let through.
        latest = state.fired or state.good
        if busy is Busy.YES and latest is not None and now - latest.at < self._rule.floor:
            self._keep(state)
            return Check(reasons=(), seen=None, state=state, busy=Busy.YES)

        seen = self._look(now)
        self._keep(state)
        return Check(reasons=reasons(state.good, seen, self._rule), seen=seen, state=state)

    def record(self, session: str, check: Check) -> None:
        """Keep what the gate saw before `session` fired. It becomes the last
        good wake once that job ends `ok`."""
        if check.seen is None:
            return

        self._keep(GateState(fired=wake_of(session, check.seen), good=check.state.good))

    def _settle(self, state: GateState, busy: Busy) -> GateState:
        """Move the last firing to `good` once its job ended `ok`. Drop it
        when it failed or vanished: the older good wake stands, so whatever
        woke it wakes the next firing too."""
        fired = state.fired
        if fired is None:
            return state

        ending = self._records.ending(self._name, fired.session, fired.at)
        if ending is Ending.OK:
            return GateState(fired=None, good=fired)

        if ending is Ending.FAILED or busy is Busy.NO:
            return GateState(fired=None, good=state.good)

        return state

    def _look(self, now: datetime) -> Snapshot:
        board = self._quiet.board
        return Snapshot(
            at=now,
            board=self._reads.board(board) if board is not None else None,
            jobs=self._reads.jobs() if self._rule.jobs is Watch.ON else None,
            daily=self._daily(now),
        )

    def _daily(self, now: datetime) -> Daily:
        """Owed from `hour` in `zone`, on a weekday if asked, until the audit
        holds the call since local midnight."""
        daily = self._quiet.daily
        if daily is None:
            return Daily.CLEAR

        try:
            local = now.astimezone(ZoneInfo(daily.zone))
        except (KeyError, ValueError, OSError):
            return Daily.OWED

        if daily.weekdays_only and local.weekday() >= SATURDAY:
            return Daily.CLEAR

        if local.hour < daily.hour:
            return Daily.CLEAR

        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        called = self._records.called(self._name, daily.call, midnight, now)
        return Daily.CLEAR if called is Called.YES else Daily.OWED

    def _keep(self, state: GateState) -> None:
        """A record that will not write costs a wake, never the firing."""
        try:
            self._store.write(self._name, state)
        except OSError as exc:
            _LOG.warning("quiet: %s: cannot write the gate record (%s)", self._name, exc.strerror)


def gated_family(registry_root: Path, name: str) -> tuple[FamilyFile, QuietBlock] | None:
    """The family and its `quiet:` block, when the registry's file for it is
    valid and carries one. Anything else fires as before: a check read from
    a file `managerd` would refuse is not one to trust."""
    try:
        registry = load_registry(registry_root)
    except OSError as exc:
        _LOG.warning("quiet: cannot read the registry at %s (%s)", registry_root, exc.strerror)
        return None

    family = registry.families.get(name)
    report = registry.reports.get(name)
    if family is None or report is None or not report.ok or family.quiet is None:
        return None

    return family, family.quiet


def _busy_of(live: bool | None) -> Busy:
    if live is None:
        return Busy.UNKNOWN

    return Busy.YES if live else Busy.NO


def _rule_of(family: FamilyFile, quiet: QuietBlock) -> Rule:
    """A family that starts jobs is never quiet while one runs (§3.15)."""
    starts_jobs = Verb.ENQUEUE in family.verbs.granted()
    return Rule(
        floor=timedelta(hours=quiet.floor_hours),
        board=Watch.ON if quiet.board is not None else Watch.OFF,
        jobs=Watch.ON if starts_jobs else Watch.OFF,
    )
