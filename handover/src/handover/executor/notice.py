"""§2.6's outcome push: after every release, the operator is told.

`stage7-releases.md` §2.6 says two things happen when the ledger entry
appears, and nothing else.

1. The requester is woken with the entry.
2. The phone gets one push, **not actionable**.

This module is the second one. The message of the push has the words that
the operator decided:

```
All succeeded

Release failed at vmm release.
Rolled back components: vmm, caregiver

Release failed at approve step.
Rolled back components: none
```

The place is `<component> release` when one component caused the end, and
`<step> step` when none did. The rolled back components are the ones that
the restore put back, in the order of the restore. The executor records
both in the entry (`Entry.failed_component`, `Entry.rolled_back`). Nothing
here reads the text of an error. The journal line keeps what was asked and
the closed-list reason.

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
from .ledger import Entry, Outcome, StepName, StepStatus

#: §2.5's cap, applied here too: one field, one line, one screen.
NOTICE_MAX_CHARS: Final = 120

CUT_MARK: Final = "…"

#: What a journal line says when a release ended with no reason recorded.
NO_REASON: Final = "no reason recorded"

#: The message of the push, as the operator decided it.
ALL_SUCCEEDED: Final = "All succeeded"
FAILED_AT: Final = "Release failed at {where}.\nRolled back components: {back}"
COMPONENT_PLACE: Final = "{name} release"
STEP_PLACE: Final = "{name} step"
NOTHING_BACK: Final = "none"

#: The step statuses that end a release.
STOPPED: Final = frozenset({str(StepStatus.FAILED), str(StepStatus.REFUSED)})


@dataclass(frozen=True)
class Notice:
    """One outcome push, and its journal line. Root builds each field from
    its own record, and never from a byte that a requester wrote."""

    #: The request id, which is also the ledger entry's file name.
    id: str
    #: What was asked: `chaperone 2.0.3 → 2.1.0`, or the component alone when the
    #: run never got far enough to resolve a version.
    asked: str
    #: §2.6's `status`: succeeded, restored, failed or refused.
    outcome: str
    #: Why, in root's own words. A closed-list reason or a step name.
    detail: str
    #: The message of the push (the module docstring).
    message: str

    def line(self) -> str:
        """The journal line: `chaperone 2.1.0 restored: verify failed`."""
        return _cut(f"{self.asked} {self.outcome}: {self.detail}")


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
        message=_message(entry),
    )


def _message(entry: Entry) -> str:
    """The message of the push: `All succeeded`, or the place of the end
    and each component that the restore put back."""
    if entry.status is Outcome.SUCCEEDED:
        return ALL_SUCCEEDED

    if entry.failed_component is not None:
        where = COMPONENT_PLACE.format(name=entry.failed_component)
    else:
        where = STEP_PLACE.format(name=_stopped_step(entry))

    back = ", ".join(entry.rolled_back) or NOTHING_BACK

    return FAILED_AT.format(where=where, back=back)


def _stopped_step(entry: Entry) -> str:
    """The step that ended the release: the first step that failed or
    refused. A restore that failed comes after the step that ended the
    release, so it is not the place. An entry with no such step names its
    last step, and an entry with no step names the intake."""
    for step in entry.steps:
        if step.status in STOPPED:
            return step.name

    if entry.steps:
        return entry.steps[-1].name

    return str(StepName.INTAKE)


def _asked(entry: Entry, component: str | None) -> str:
    """`chaperone 2.0.3 → 2.1.0`, from the ledger's own `previous` map and the
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
