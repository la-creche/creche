"""The records the jobs and the remaining modules share (contract 02 §7, §13).

`to_api()` is the shape a reader sees. It holds exactly the fields the
contract's table names.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from .clock import rfc3339
from .states import SessionKind

LEASE_TTL_S = 60
DEFAULT_DEADLINE_S = 3600


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
