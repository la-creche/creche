"""What survives a dispatched job (contract 02 §13.1).

The conversation of a job stays in the facade's store. What this service
keeps is one small record per job, and the one view reads it (invariant 20).

```
  prompt settles --> build the record --> outcomes/<family>/<ulid>.json
                                      `-> note its id in the ledger entry
```
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from .acp_jobs import EndKind, JobEnd
from .atomic import MODE_PUBLIC_READ, write_json
from .clock import now, rfc3339
from .ids import new_ulid
from .models import Trigger
from .paths import outcome_file

# Contract 02 §13.1's `error` field. A model wrote some of this text, so it
# is capped like every other value that reaches a durable record.
ERROR_MAX_BYTES = 4_096

# A job over ACP has no usage: the prompt answer carries none. LiteLLM is
# spend's one authority (contract 05 §7), and this layer does not read it.
SPEND_NO_USAGE_REASON: Final = (
    "the job ran over ACP, whose prompt answer reports no usage; "
    "LiteLLM is spend's one authority (contract 05 §7)"
)


class Outcome(StrEnum):
    """Contract 02 §13.1's `status` field."""

    OK = "ok"
    FAILED = "failed"
    DENIED = "denied"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


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
            # Phase 2 plan, row 19. Approvals are chaperone gates now, and
            # no gate holds a turn, so this service counts none.
            "approvals": None,
            "spend_usd": self.spend_usd,
            "spend_reason": self.spend_reason,
            "sandbox": self.sandbox,
        }


def from_job(
    family: str,
    job: str,
    trigger: Trigger,
    started_at: datetime,
    end: JobEnd,
    sandbox: str,
) -> OutcomeRecord:
    """One record for one job over ACP: one prompt, so one turn."""
    return OutcomeRecord(
        id=new_ulid(),
        family=family,
        session=job,
        trigger=trigger,
        started_at=started_at,
        ended_at=now(),
        status=_BY_END[end.kind],
        error=cut_error(end.error) if end.kind is not EndKind.OK else None,
        turns=1,
        spend_usd=None,
        spend_reason=SPEND_NO_USAGE_REASON,
        sandbox=sandbox,
    )


_BY_END: dict[EndKind, Outcome] = {
    EndKind.OK: Outcome.OK,
    EndKind.TIMEOUT: Outcome.TIMEOUT,
    EndKind.FAILED: Outcome.FAILED,
    EndKind.CANCELLED: Outcome.CANCELLED,
}


def write(state_root: Path, outcome: OutcomeRecord) -> Path:
    """Temp file and rename, world-readable for the noticeboard (§13.1)."""
    path = outcome_file(state_root, outcome.family, outcome.id)
    write_json(path, outcome.to_file(), MODE_PUBLIC_READ)
    return path


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
