"""The validation report (contract 01 §7).

Invariant 19: a bad definition yields a report. Nothing here raises on bad
input, and nothing here half-applies: a report is data, and the caller decides
what to do with it."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

#: `first_error` in the status document is capped here (contract 05 §3.2).
FIRST_ERROR_CHARS = 200


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class FamilyState(StrEnum):
    """Contract 05 §2.1. `in_sync` is the JSON spelling; "in sync" is display."""

    IN_SYNC = "in_sync"
    RECONCILING = "reconciling"
    INVALID = "invalid"
    DEGRADED = "degraded"


@dataclass(frozen=True)
class Issue:
    """One violation. `loc` is a field path, for example `tools.kagi[1]`."""

    severity: Severity
    loc: str
    msg: str
    downgraded: bool = False

    def as_json(self) -> dict[str, Any]:
        return {
            "severity": str(self.severity),
            "loc": self.loc,
            "msg": self.msg,
            "downgraded": self.downgraded,
        }


@dataclass(frozen=True)
class Report:
    """One object per family (contract 01 §7).

    `state` and `applied` are what the validator can honestly say on its own:
    `invalid` when an error was found, else `reconciling`, and `applied: false`
    because validating changes no live state. `managerd` stamps the real pair
    with `stamped()` after it applies."""

    family: str
    file: str
    issues: tuple[Issue, ...] = ()
    state: FamilyState = FamilyState.RECONCILING
    applied: bool = False

    @property
    def errors(self) -> int:
        return sum(1 for issue in self.issues if issue.severity is Severity.ERROR)

    @property
    def warnings(self) -> int:
        return sum(1 for issue in self.issues if issue.severity is Severity.WARNING)

    @property
    def ok(self) -> bool:
        return self.errors == 0

    @property
    def first_error(self) -> str | None:
        for issue in self.issues:
            if issue.severity is Severity.ERROR:
                return f"{issue.loc}: {issue.msg}"[:FIRST_ERROR_CHARS]
        return None

    def stamped(self, state: FamilyState, *, applied: bool) -> Report:
        """The state a reconciler knows and a validator does not."""
        return Report(self.family, self.file, self.issues, state, applied)

    def as_json(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "file": self.file,
            "status": str(self.state),
            "applied": self.applied,
            "issues": [issue.as_json() for issue in self.issues],
            "errors": self.errors,
            "warnings": self.warnings,
        }


@dataclass
class Issues:
    """Accumulator. One parse reports every violation it can see, not the
    first (contract 01 §7 rule 2), so every check appends and none returns
    early on a failure it has already recorded."""

    entries: list[Issue] = field(default_factory=list[Issue])

    def error(self, loc: str, msg: str) -> None:
        self.entries.append(Issue(Severity.ERROR, loc, msg))

    def warn(self, loc: str, msg: str, *, downgraded: bool = False) -> None:
        self.entries.append(Issue(Severity.WARNING, loc, msg, downgraded))

    def frozen(self) -> tuple[Issue, ...]:
        return tuple(self.entries)
