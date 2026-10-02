"""Test doubles for the quiet gate's three readers and its store, plus a
family file that carries `quiet:`. Named `trigger_quiet_fakes.py` because a
test helper's basename must be unique repo-wide (`door-owui/AGENTS.md`)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from agent_door_trigger.quiet.decide import Jobs
from agent_door_trigger.quiet.gate import QuietGate
from agent_door_trigger.quiet.records import Called, Ending
from agent_door_trigger.quiet.state import GateState
from agent_family import FamilyFile, QuietBlock, parse_family

FAMILY = "scrum-lead"
BOARD = "a" * 64
DONE = frozenset({"auto-OLD"})

#: A Friday, 10:00 in Phoenix.
FRIDAY_MORNING = datetime(2026, 9, 25, 17, 0, tzinfo=UTC)

FAMILY_YAML = """\
name: scrum-lead
kind: autonomous
description: Test fixture family.

model: { router: agent-router, budget_usd_per_day: 10 }

tools:
  board-lead: [survey_board]
  mail: [send_standup_email]

verbs:
  enqueue: { targets: [finance-worker] }
  job_status: {}

triggers:
  - cron: "@hourly"

quiet:
  board: board-lead
  daily: { call: mail__send_standup_email, hour: 16, zone: America/Phoenix, weekdays_only: true }
  floor_hours: 24
"""


def family(text: str = FAMILY_YAML) -> tuple[FamilyFile, QuietBlock]:
    parsed, issues = parse_family(text)
    assert parsed is not None, issues
    assert parsed.quiet is not None
    return parsed, parsed.quiet


@dataclass
class FakeReads:
    """`FamilyReads`: set what the PEP answers, read what was asked."""

    board_value: str | None = BOARD
    jobs_value: Jobs | None = field(default_factory=lambda: Jobs(live=frozenset(), ended=DONE))
    asked: list[str] = field(default_factory=list[str])

    def board(self, server: str) -> str | None:
        self.asked.append(f"board {server}")
        return self.board_value

    def jobs(self) -> Jobs | None:
        self.asked.append("jobs")
        return self.jobs_value


@dataclass
class FakeRecords:
    """`Records`: an ending per session, and one answer for the audit."""

    endings: dict[str, Ending] = field(default_factory=dict[str, Ending])
    called_value: Called = Called.NO
    called_asks: list[tuple[str, str, datetime, datetime]] = field(
        default_factory=list[tuple[str, str, datetime, datetime]]
    )

    def ending(self, family: str, session: str, since: datetime) -> Ending:
        return self.endings.get(session, Ending.PENDING)

    def called(self, family: str, call: str, since: datetime, until: datetime) -> Called:
        self.called_asks.append((family, call, since, until))
        return self.called_value


@dataclass
class MemoryStore:
    """`StateStore` in memory. `broken` makes every write fail."""

    states: dict[str, GateState] = field(default_factory=dict[str, GateState])
    broken: bool = False

    def read(self, family: str) -> GateState:
        return self.states.get(family, GateState())

    def write(self, family: str, state: GateState) -> None:
        if self.broken:
            raise PermissionError(13, "Permission denied")

        self.states[family] = state


@dataclass
class FakeSessions:
    """`LiveSessions`: whether a wake of the family is live."""

    live: bool | None = False

    def any_live(self, family: str) -> bool | None:
        return self.live


@dataclass
class Rig:
    """One gate and everything behind it. `now` moves the clock."""

    reads: FakeReads = field(default_factory=FakeReads)
    records: FakeRecords = field(default_factory=FakeRecords)
    store: MemoryStore = field(default_factory=MemoryStore)
    sessions: FakeSessions = field(default_factory=FakeSessions)
    now: datetime = FRIDAY_MORNING

    def gate(self, text: str = FAMILY_YAML) -> QuietGate:
        found, quiet = family(text)
        return QuietGate(
            found,
            quiet,
            reads=self.reads,
            records=self.records,
            store=self.store,
            sessions=self.sessions,
            clock=lambda: self.now,
        )

    @property
    def state(self) -> GateState:
        return self.store.read(FAMILY)
