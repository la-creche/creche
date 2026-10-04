"""What survives an autonomous job (contract 02 §13.1).

An autonomous session is deleted when its job ends, because nobody will ever
open it again and a timer that fires every morning would otherwise grow a
session directory a day. What stays is one small record per job, and the one
view reads it (invariant 20).

```
  last turn terminal --> build the record --> outcomes/<family>/<ulid>.json
                                          `-> delete the session
```

The record is written BEFORE the session is deleted. A crash between the two
leaves a session that ends again, and writes a second record, which is
visible and harmless. The other order loses the job entirely.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from .atomic import MODE_PUBLIC_READ, write_json
from .clock import rfc3339
from .errors import TurnReason
from .ids import new_ulid
from .models import GateTally, Session, Trigger, Turn
from .paths import outcome_file
from .states import TurnState

# Contract 02 §13.1's `error` field. A model wrote some of this text, so it
# is capped like every other value that reaches a durable record.
ERROR_MAX_BYTES = 4_096

# CONTRACT-QUESTION: contract 02 §13.1 sums `spend_usd` "from turn usage",
# and that usage is contract 03 §5.2's advisory fold of pi's own
# `cost.total`, which is unverified against what pi actually sends.
# Contract 05 §7 names LiteLLM the one authority for spend. Real tokens
# with a summed cost of exactly zero is that unreliable fold showing, not a
# free job. `outcomes` cannot read LiteLLM or the family status document's
# `spend` block itself (`attendance/AGENTS.md` structure rule 4: layers talk
# downward only, and `family_status` is a peer, not something below this
# module). So a summed zero counts only when no turn reported any tokens
# either; otherwise the field is `null` with `spend_reason` naming why,
# which §13.1 allows.
SPEND_UNKNOWN_REASON: Final = (
    "tokens were used but no cost was reported; the advisory number is unreliable "
    "and this layer does not read LiteLLM, spend's one authority (contract 05 §7)"
)


# CONTRACT-QUESTION: contract 02 §13.1 gives `spend_usd` as a number or
# null, and names one case for null. It does not name a sum that is not
# finite. Each cost of a turn is finite, and the sum of two of them can be
# infinity. No JSON number is infinity, so the record holds null with this
# reason. The other reading writes the largest float, which states a spend
# that no turn reported.
SPEND_NOT_FINITE_REASON: Final = (
    "the sum of the reported costs is not a finite number; the advisory number is unreliable"
)


class Outcome(StrEnum):
    """Contract 02 §13.1's `status` field."""

    OK = "ok"
    FAILED = "failed"
    DENIED = "denied"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


# The reasons §13.1's narrower statuses name. Everything else that failed is
# `failed`, which is the honest answer when nothing more precise is known.
_BY_REASON: dict[TurnReason, Outcome] = {
    TurnReason.APPROVAL_DENIED: Outcome.DENIED,
    TurnReason.APPROVAL_TIMEOUT: Outcome.TIMEOUT,
    TurnReason.TURN_TIMEOUT: Outcome.TIMEOUT,
}


@dataclass(slots=True)
class Approvals:
    """Contract 02 §13.1's `approvals` block.

    Every one of the four numbers is counted from the PEP's own audit
    records (§13.1.1): `requested` when a gate opens, the other three from
    the record that resolves it. None is read off the turn's failure
    reason: a refused gate leaves the turn running (contract 02 §4.3), so
    one turn can meet an approved gate AND a denied one.

    `approved + denied + timed_out` can be lower than `requested`. A gate
    that ended on `approval_revoked` or `approval_abandoned` decided nothing
    about the call, and the job's `status` carries what happened instead.
    """

    requested: int = 0
    approved: int = 0
    denied: int = 0
    timed_out: int = 0

    def to_file(self) -> dict[str, Any]:
        return {
            "requested": self.requested,
            "approved": self.approved,
            "denied": self.denied,
            "timed_out": self.timed_out,
        }


@dataclass(slots=True)
class OutcomeRecord:
    """One finished autonomous job (contract 02 §13.1)."""

    id: str
    family: str
    session: str
    trigger: Trigger | None
    started_at: datetime
    ended_at: datetime
    status: Outcome
    error: str | None
    turns: int
    approvals: Approvals
    spend_usd: float | None
    #: Why `spend_usd` is null. Always None when it is a number.
    spend_reason: str | None
    sandbox: str

    def to_file(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "family": self.family,
            "session": self.session,
            "trigger": self.trigger.to_api() if self.trigger is not None else None,
            "started_at": rfc3339(self.started_at),
            "ended_at": rfc3339(self.ended_at),
            "status": self.status.value,
            "error": self.error,
            "turns": self.turns,
            "approvals": self.approvals.to_file(),
            "spend_usd": self.spend_usd,
            "spend_reason": self.spend_reason,
            "sandbox": self.sandbox,
        }


def build(record: Session, turns: list[Turn], error: str | None = None) -> OutcomeRecord:
    """One record from the session and its turns. Every field is already held."""
    last = turns[-1]
    spend_usd, spend_reason = _spend_of(turns)

    return OutcomeRecord(
        id=new_ulid(),
        family=record.family,
        session=record.session,
        trigger=record.trigger,
        started_at=turns[0].started_at,
        ended_at=last.ended_at if last.ended_at is not None else last.started_at,
        status=status_of(last),
        error=cut_error(error),
        turns=len(turns),
        approvals=_approvals(turns),
        spend_usd=spend_usd,
        spend_reason=spend_reason,
        sandbox=last.sandbox or record.sandbox or "",
    )


def _spend_of(turns: list[Turn]) -> tuple[float | None, str | None]:
    """Contract 02 §13.1's `spend_usd`, honest about what the sum can prove.

    A summed cost of exactly zero is ambiguous on its own: it is the right
    answer when no turn ever reached a model, and it is what
    the `CONTRACT-QUESTION` above already distrusts otherwise. The one extra
    signal this module already holds is token usage — a metered call that
    used real tokens essentially never costs exactly nothing, so THAT
    combination is reported unknown rather than a false `$0.00`.
    """
    cost = round(sum(turn.usage.cost_usd for turn in turns), 6)
    tokens = sum(
        turn.usage.input + turn.usage.output + turn.usage.cache_read + turn.usage.cache_write
        for turn in turns
    )

    if not math.isfinite(cost):
        return None, SPEND_NOT_FINITE_REASON

    if cost == 0.0 and tokens > 0:
        return None, SPEND_UNKNOWN_REASON

    return cost, None


def status_of(last: Turn) -> Outcome:
    """Contract 02 §13.1's `status`, from the turn that ended the job."""
    if last.state is TurnState.SETTLED:
        return Outcome.OK

    if last.state is TurnState.ABORTED:
        return Outcome.CANCELLED

    if last.reason is None:
        return Outcome.FAILED

    return _BY_REASON.get(last.reason, Outcome.FAILED)


def write(state_root: Path, outcome: OutcomeRecord) -> Path:
    """Temp file and rename, world-readable for the noticeboard (§13.1)."""
    path = outcome_file(state_root, outcome.family, outcome.id)
    write_json(path, outcome.to_file(), MODE_PUBLIC_READ)
    return path


def _approvals(turns: list[Turn]) -> Approvals:
    """Contract 02 §13.1.1. Every number is a count of audit records."""
    whole = GateTally()

    for turn in turns:
        whole.add(turn.gates)

    return Approvals(
        requested=sum(turn.approvals for turn in turns),
        approved=whole.approved,
        denied=whole.denied,
        timed_out=whole.timed_out,
    )


def cut_error(error: str | None) -> str | None:
    """§13.1's 4 KiB cap. A model wrote some of this text."""
    if error is None:
        return None

    # A message from a sandbox can hold one half of a surrogate pair. Such a
    # text has no UTF-8 form, so the cap in bytes cannot count it. The record
    # holds U+FFFD in the place of each half.
    whole = error.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    encoded = whole.encode("utf-8")

    if len(encoded) <= ERROR_MAX_BYTES:
        return whole

    return encoded[:ERROR_MAX_BYTES].decode("utf-8", "ignore")
