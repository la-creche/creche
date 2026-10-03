"""§2.6's outcome push: after every release, the operator is told.

`stage7-releases.md` §2.6 says two things happen when the ledger entry
appears, and nothing else.

1. The requester is woken with the entry.
2. The phone gets one push, **not actionable**: `pep 2.1.0 restored: verify
   failed` or `pep 2.1.0 succeeded (9 verify hooks ok)`.

This module is the second one. It carries what was asked, what happened,
and where the ledger entry is.

**The wake-up is deliberately NOT here**: root holds no session credential
and `attendance` runs as the operator, so root cannot start a turn without a new
root-to-operator channel.
`done/` is group readable by design (§2.6), so the woken-from-the-ledger
half belongs to an operator-side reader. Root's job is the record and the push.

Nothing here decides anything. A push that fails is logged and the release
keeps its outcome: an unreachable phone must not turn a succeeded release
into a failed one.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, cast

from ..catalog import Action
from .ledger import Entry, Outcome
from .spool import DONE_DIR, REQUEST_SUFFIX, SPOOL_ROOT

#: §2.5's cap, applied here too: one field, one line, one screen.
NOTICE_MAX_CHARS: Final = 120

CUT_MARK: Final = "…"

#: What a push says when a release ended with no reason recorded.
NO_REASON: Final = "no reason recorded"


@dataclass(frozen=True)
class Notice:
    """One outcome push. Four fields, all built by root from root's own
    record — never from a byte a requester wrote."""

    #: The request id, which is also the ledger entry's file name.
    id: str
    #: What was asked: `pep 2.0.3 → 2.1.0`, or the component alone when the
    #: run never got far enough to resolve a version.
    asked: str
    #: §2.6's `status`: succeeded, restored, failed or refused.
    outcome: str
    #: Why, in root's own words. A closed-list reason or a step name.
    detail: str
    #: Where the entry is, so the reader never has to guess the path.
    ledger: str

    def line(self) -> str:
        """The one line a notification shows: §2.6's own two examples."""
        return _cut(f"{self.asked} {self.outcome}: {self.detail}")

    def as_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "asked": _cut(self.asked),
            "outcome": _cut(self.outcome),
            "detail": _cut(self.detail),
            "ledger": _cut(self.ledger),
        }


#: The seam. One function: tell the operator, answer whether it was delivered.
Notifier = Callable[[Notice], bool]


def say_nothing(notice: Notice) -> bool:
    """The notifier a host with no phone path configured gets.

    Unlike `approval.deny_all`, silence here is SAFE: a push that never
    goes out cannot approve anything. It costs the reader the push and
    nothing else, and the ledger entry is still written.
    """
    del notice

    return False


def _cut(value: str) -> str:
    if len(value) <= NOTICE_MAX_CHARS:
        return value

    return value[: NOTICE_MAX_CHARS - len(CUT_MARK)] + CUT_MARK


def ledger_path(request_id: str, root: str = SPOOL_ROOT) -> str:
    return f"{root}/{DONE_DIR}/{request_id}{REQUEST_SUFFIX}"


def notice_of(entry: Entry, component: str | None = None) -> Notice:
    """§2.6's push, built from the entry root just wrote.

    `component` names what the caller knows and the entry may not: a repair
    has no resolved manifest, so its component comes from the switch note.
    """
    return Notice(
        id=entry.id,
        asked=_asked(entry, component),
        outcome=str(entry.status),
        detail=_detail(entry),
        ledger=ledger_path(entry.id),
    )


def _asked(entry: Entry, component: str | None) -> str:
    """`pep 2.0.3 → 2.1.0`, from the ledger's own `previous` map and the
    resolved manifest. A run that refused before step 2 has neither, so it
    falls back to the component the caller named, then to the id."""
    moves = _moves(entry)
    if moves:
        return ", ".join(moves)

    return component or entry.id


def _moves(entry: Entry) -> list[str]:
    rows = _deploying_rows(entry)
    if not rows:
        return [f"{name} {was or 'absent'}" for name, was in sorted(entry.previous.items())]

    return [f"{name} {was} → {to}" for name, was, to in rows]


def _deploying_rows(entry: Entry) -> list[tuple[str, str, str]]:
    """Every `action: deploy` row of the resolved manifest, in name order."""
    document = entry.manifest
    if not isinstance(document, dict):
        return []

    listed = document.get("components")
    if not isinstance(listed, list):
        return []

    found: list[tuple[str, str, str]] = []
    for row in listed:  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(row, dict) or row.get("action") != str(Action.DEPLOY):  # pyright: ignore[reportUnknownMemberType]
            continue

        fields = cast("dict[str, object]", row)
        was = fields.get("from_version")
        found.append(
            (
                str(fields.get("name")),
                str(was) if was else "absent",
                str(fields.get("to_version")),
            )
        )

    return sorted(found)


def _detail(entry: Entry) -> str:
    """Why, in one phrase. A refusal names its check, a success counts the
    hooks that passed, and everything else quotes the closed-list reason."""
    if entry.status is Outcome.SUCCEEDED:
        return f"{len(entry.verify)} verify hook(s) ok"

    if entry.refused_check:
        return f"{entry.reason or NO_REASON} ({entry.refused_check})"

    return entry.reason or NO_REASON
