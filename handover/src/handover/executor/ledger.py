"""The ledger entry (`stage7-releases.md` §2.6).

One entry per request, written under the lock at step 10, at
`done/<ULID>.json`, with the whole log beside it at `done/<ULID>.log`.

**The ledger informs and never decides.** `done/` is readable by the group,
so a rollback target is re-verified P1 to P5 like any other request, and the
rule that a component never goes backwards rests on what is INSTALLED. Every
reader of this module should keep that in mind before adding a function that
reads an entry back.

Only validated fields and root's own words reach an entry. A request root
would not parse contributes its id — which came from the file name — and
nothing else (§3.2 rule 4).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

#: §2.6: the entry keeps the last 200 lines. The whole log is the `.log`
#: file beside it.
LOG_TAIL_LINES: Final = 200

#: What one run may say before it stops adding lines. A release that logs
#: more than this has a loop, and the ledger is not where that is debugged.
LOG_MAX_LINES: Final = 5000


class StepName(StrEnum):
    """§2.4's ten steps, in order. Row 10 is `record` on success and
    `restore` when a verify failed."""

    INTAKE = "intake"
    RESOLVE = "resolve"
    PROVENANCE = "provenance"
    CONTRACTS = "contracts"
    APPROVE = "approve"
    LOCK = "lock"
    QUIESCE = "quiesce"
    STAGE = "stage"
    SWITCH = "switch"
    RESTORE = "restore"
    RECORD = "record"


class StepStatus(StrEnum):
    OK = "ok"
    FAILED = "failed"
    REFUSED = "refused"
    SKIPPED = "skipped"


class Outcome(StrEnum):
    """§2.6's `status`. `restored` means the release deployed, failed
    verify, and went back cleanly."""

    SUCCEEDED = "succeeded"
    RESTORED = "restored"
    FAILED = "failed"
    REFUSED = "refused"


@dataclass(frozen=True)
class Step:
    name: str
    status: str
    seconds: float
    detail: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": self.status,
            "seconds": self.seconds,
            "detail": self.detail,
        }


@dataclass
class Entry:
    """§2.6's field table, built up as the steps run."""

    id: str
    kind: str
    requested_by: str
    status: Outcome = Outcome.FAILED
    manifest: dict[str, object] | None = None
    approved_at: float | None = None
    steps: list[Step] = field(default_factory=list[Step])
    refused_check: str | None = None
    reason: str | None = None
    verify: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    previous: dict[str, str | None] = field(default_factory=dict[str, "str | None"])
    manual: list[str] = field(default_factory=list[str])
    log: list[str] = field(default_factory=list[str])

    def add(self, step: Step) -> None:
        self.steps.append(step)

    def say(self, line: str) -> None:
        if len(self.log) < LOG_MAX_LINES:
            self.log.append(line)

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "status": str(self.status),
            "kind": self.kind,
            "requested_by": self.requested_by,
            "manifest": self.manifest,
            "approved_at": self.approved_at,
            "steps": [one.as_dict() for one in self.steps],
            "refused_check": self.refused_check,
            "reason": self.reason,
            "verify": self.verify,
            "previous": self.previous,
            "manual": self.manual,
            "log_tail": self.log[-LOG_TAIL_LINES:],
        }
