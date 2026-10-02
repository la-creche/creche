"""Session, turn, lease and journal-line records (contract 02 §4, §7, §8).

Two shapes exist for each record. `to_api()` is what a door sees, and it holds
exactly the fields the contract's table names. `to_file()` is what lands on
disk, and it may hold more, because the service has to rebuild its indexes
after a restart (invariant 1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from .clock import rfc3339, rfc3339_ms
from .errors import TurnReason
from .states import SessionKind, SessionState, TurnState

FIRST_JOURNAL_SEQ = 1
LEASE_TTL_S = 60
DEFAULT_DEADLINE_S = 3600


class LineKind(StrEnum):
    """Journal line kinds (contract 02 §8.1)."""

    SESSION_CREATED = "session_created"
    SESSION_TITLED = "session_titled"
    WRITER_CHANGED = "writer_changed"
    TURN_QUEUED = "turn_queued"
    TURN_STARTED = "turn_started"
    PI_EVENT = "pi_event"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_RESOLVED = "approval_resolved"
    TURN_SETTLED = "turn_settled"
    TURN_FAILED = "turn_failed"
    TURN_ABORTED = "turn_aborted"
    BRANCH_FALLBACK = "branch_fallback"
    TERMINAL_EXCHANGE = "terminal_exchange"
    NOTE = "note"
    HEARTBEAT = "heartbeat"


# The three lines that end a turn. A caller may act on one, so it reaches the
# platter before the answer leaves (contract 02 §8.3), and a turn-scoped
# stream ends after it (§5.4).
TERMINAL_LINE_KINDS: frozenset[LineKind] = frozenset(
    {LineKind.TURN_SETTLED, LineKind.TURN_FAILED, LineKind.TURN_ABORTED}
)


class Holder(StrEnum):
    """Which door holds the writer lease (contract 02 §7.1)."""

    OWUI = "owui"
    TUI = "tui"
    DELEGATE = "delegate"
    DISPATCH = "dispatch"
    TRIGGER = "trigger"


class TriggerKind(StrEnum):
    """What fired an autonomous job (contract 02 §13.2)."""

    TIMER = "timer"
    WEBHOOK = "webhook"
    #: Another family's `enqueue` verb started this session (contract 01
    #: §3.13's dispatch form, contract 02 §13.4). `name` then carries the
    #: CALLING family, which is the fact a reader of an outcome record wants.
    DISPATCH = "dispatch"


#: Contract 02 §13.2 rule 6. A chain is bounded because it reaches a durable
#: record and it arrives from another process (invariants 12 and 14).
CHAIN_MAX_FAMILIES = 8


@dataclass(slots=True)
class Trigger:
    """The firing that caused one autonomous job (contract 02 §13.2).

    It is kept on the session because one session is one job is one firing,
    and because §13.1's outcome record has to name it after the session is
    deleted.
    """

    kind: TriggerKind
    name: str | None = None
    fired_at: datetime | None = None
    #: Contract 02 §13.2 rule 6, `dispatch` only. The delegation chain the
    #: PEP built from its own mints (contract 04 §6.3), caller first. Empty
    #: on the other two kinds, and the file then carries no `chain` key.
    chain: tuple[str, ...] = ()

    def to_api(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "kind": self.kind.value,
            "name": self.name,
            "fired_at": rfc3339(self.fired_at) if self.fired_at is not None else None,
        }

        # Absent, not empty, on a timer or a webhook: a reader that meets the
        # key knows a family enqueued this job.
        if self.chain:
            body["chain"] = list(self.chain)

        return body


@dataclass(slots=True)
class Usage:
    """Token and cost counts. Advisory, per contract 03 §13 rule 7."""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost_usd: float = 0.0

    def to_api(self) -> dict[str, Any]:
        return {
            "input": self.input,
            "output": self.output,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
            "cost_usd": self.cost_usd,
        }

    def add(self, other: Usage) -> None:
        """Fold another reading in. Turn usage sums across a turn's events."""
        self.input += other.input
        self.output += other.output
        self.cache_read += other.cache_read
        self.cache_write += other.cache_write
        self.cost_usd += other.cost_usd


class GateOutcome(StrEnum):
    """Which of §13.1.1's counters one resolving audit record raises."""

    APPROVED = "approved"
    DENIED = "denied"
    TIMED_OUT = "timed_out"
    #: A gate that decided nothing about the call: the grant went away, or
    #: the caller did. It stays in `Turn.approvals` and nowhere else.
    UNCOUNTED = "uncounted"


@dataclass(slots=True)
class GateTally:
    """How one turn's gates resolved (contract 02 §13.1.1).

    Counted from the PEP's own resolving audit records (contract 04 §6.4),
    never from the turn's failure reason. A refused gate leaves the turn
    running (contract 02 §4.3), so ONE turn can meet an approved gate and a
    denied one, and no single reason can carry both.

    `approved + denied + timed_out` can be lower than `Turn.approvals`. A
    gate that ended on `approval_revoked` or `approval_abandoned` decided
    nothing about the call, so it is counted in `requested` alone.
    """

    approved: int = 0
    denied: int = 0
    timed_out: int = 0

    def to_file(self) -> dict[str, Any]:
        return {"approved": self.approved, "denied": self.denied, "timed_out": self.timed_out}

    def count(self, outcome: GateOutcome) -> None:
        """Raise the one counter this resolution belongs to, if any."""
        if outcome is GateOutcome.APPROVED:
            self.approved += 1
            return

        if outcome is GateOutcome.DENIED:
            self.denied += 1
            return

        if outcome is GateOutcome.TIMED_OUT:
            self.timed_out += 1

    def add(self, other: GateTally) -> None:
        """Fold another turn's gates in. §13.1's record sums a whole job."""
        self.approved += other.approved
        self.denied += other.denied
        self.timed_out += other.timed_out


@dataclass(slots=True)
class WriterLease:
    """One writer per session (contract 02 §7.1)."""

    holder: Holder
    door_instance: str
    since: datetime
    expires_at: datetime
    turn: str | None = None

    def to_api(self) -> dict[str, Any]:
        return {
            "holder": self.holder.value,
            "door_instance": self.door_instance,
            "since": rfc3339(self.since),
            "expires_at": rfc3339(self.expires_at),
            "turn": self.turn,
        }

    def is_expired(self, moment: datetime) -> bool:
        return moment >= self.expires_at


@dataclass(slots=True)
class OwuiRefs:
    """The four Open WebUI ids a turn may carry (contract 02 §10)."""

    chat_id: str
    message_id: str
    user_message_id: str | None = None
    parent_id: str | None = None

    def to_api(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "message_id": self.message_id,
            "user_message_id": self.user_message_id,
            "parent_id": self.parent_id,
        }


@dataclass(slots=True)
class Turn:
    """One prompt run to settled (contract 02 §4.3, §4.4)."""

    turn: str
    session: str
    family: str
    state: TurnState
    started_at: datetime
    sandbox: str
    deadline_s: int = DEFAULT_DEADLINE_S
    reason: TurnReason | None = None
    ended_at: datetime | None = None
    idempotency_key: str | None = None
    usage: Usage = field(default_factory=Usage)
    approvals: int = 0
    owui: OwuiRefs | None = None
    persona_truncated: bool = False
    # File only, so §13.1's record can be built after a restart. Contract 02
    # §4.4's field table carries `approvals` and no second counter, and the
    # one view reads the four numbers off the outcome record instead.
    gates: GateTally = field(default_factory=GateTally)
    # File only. A repeat of a known key with a different prompt must refuse
    # (contract 02 §6 rule 5), and that check has to survive a restart. The
    # prompt itself stays in the journal, so only its digest is kept here.
    prompt_sha256: str = ""

    def to_api(self) -> dict[str, Any]:
        return {
            "turn": self.turn,
            "state": self.state.value,
            "reason": self.reason.value if self.reason is not None else None,
            "started_at": rfc3339(self.started_at),
            "ended_at": rfc3339(self.ended_at) if self.ended_at is not None else None,
            "deadline_s": self.deadline_s,
            "idempotency_key": self.idempotency_key,
            "sandbox": self.sandbox,
            "usage": self.usage.to_api(),
            "approvals": self.approvals,
            "owui": self.owui.to_api() if self.owui is not None else None,
            "persona_truncated": self.persona_truncated,
        }

    def to_file(self) -> dict[str, Any]:
        stored = self.to_api()
        stored["session"] = self.session
        stored["family"] = self.family
        stored["prompt_sha256"] = self.prompt_sha256
        stored["gates"] = self.gates.to_file()
        return stored


@dataclass(slots=True)
class Session:
    """One durable conversation or job in one family (contract 02 §4.2)."""

    family: str
    session: str
    kind: SessionKind
    created_at: datetime
    updated_at: datetime
    title: str = ""
    journal_seq: int = 0
    turns_total: int = 0
    sandbox: str | None = None
    persona_hash: str | None = None
    labels: dict[str, str] = field(default_factory=dict[str, str])
    # Open WebUI message id to pi entry id (contract 02 §10.1). File only.
    owui_map: dict[str, str] = field(default_factory=dict[str, str])
    # The session that asked for this job, thin families only (contract 02
    # §5.1, §12.1). File only: §4.2's field table does not carry it.
    owner_session: str | None = None
    # The firing that caused this job, autonomous families only (contract 02
    # §13.2). File only, and fixed by the session's first turn.
    trigger: Trigger | None = None
    # The Open WebUI chat this session is copied into, and that copy's newest
    # message (contract 02 §10.4). File only: a session not born in Open WebUI
    # gains a chat, and the leaf is what an append attaches to. Reading the
    # chat back instead is what §10.4 rule 1 forbids.
    owui_chat: str | None = None
    owui_leaf: str | None = None
    # How many of `turns_total` were terminal exchanges (contract 02 §4.2,
    # §10.5), and the newest pi entry this service has accounted for. The
    # cursor is `since` on the next contract 03 §4.8 read, so a restart must
    # not lose it: a lost cursor reads the whole history and copies every
    # exchange into Open WebUI a second time.
    terminal_total: int = 0
    pi_cursor: str | None = None

    def to_api(
        self,
        state: SessionState,
        turns_running: int,
        writer: WriterLease | None,
    ) -> dict[str, Any]:
        """State, running count and writer are live values, not stored ones."""
        return {
            "family": self.family,
            "session": self.session,
            "kind": self.kind.value,
            "title": self.title,
            "state": state.value,
            "created_at": rfc3339(self.created_at),
            "updated_at": rfc3339(self.updated_at),
            "journal_seq": self.journal_seq,
            "writer": writer.to_api() if writer is not None else None,
            "turns_total": self.turns_total,
            "terminal_total": self.terminal_total,
            "turns_running": turns_running,
            "sandbox": self.sandbox,
            "persona_hash": self.persona_hash,
            "labels": dict(self.labels),
        }

    def to_file(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "session": self.session,
            "kind": self.kind.value,
            "title": self.title,
            "created_at": rfc3339(self.created_at),
            "updated_at": rfc3339(self.updated_at),
            "journal_seq": self.journal_seq,
            "turns_total": self.turns_total,
            "sandbox": self.sandbox,
            "persona_hash": self.persona_hash,
            "labels": dict(self.labels),
            "owui_map": dict(self.owui_map),
            "owner_session": self.owner_session,
            "trigger": self.trigger.to_api() if self.trigger is not None else None,
            "owui_chat": self.owui_chat,
            "owui_leaf": self.owui_leaf,
            "terminal_total": self.terminal_total,
            "pi_cursor": self.pi_cursor,
        }


@dataclass(slots=True)
class JournalLine:
    """One line of the event stream (contract 02 §8)."""

    journal_seq: int | None
    ts: datetime
    kind: LineKind
    turn: str | None
    body: dict[str, Any]

    def to_api(self) -> dict[str, Any]:
        return {
            "journal_seq": self.journal_seq,
            "ts": rfc3339_ms(self.ts),
            "kind": self.kind.value,
            "turn": self.turn,
            "body": self.body,
        }
