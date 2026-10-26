"""The records of a job: its trigger and its usage (contract 02 §13).

`to_api()` is the shape a reader sees. It holds exactly the fields the
contract's table names.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from .clock import rfc3339

DEFAULT_DEADLINE_S = 3600


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
