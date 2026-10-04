"""`caregiver`'s status documents, as the noticeboard reads them (contract 05 §8).

Invariant 20: one view shows every family, sandbox and session, and that
view is the truth. Truth here means two things this module enforces.

1. **A stale document is never shown as current** (contract 05 §2 rule 5).
   Past 90 seconds the family reads `unknown` and the row says why, because
   a `written_at` that old means `caregiver` is not running, not that the
   state below it still holds.
2. **Spend is one number per family.** The budget belongs
   to the family key, never to a sandbox (contract 05 §7 rule 1), so no
   sandbox row carries one and `SandboxRow` has no field for it.

Nothing here raises. A file that will not parse becomes a `problem` string
on the row, which the page renders as a report.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Final

from . import jsonfiles
from .jsonfiles import Json

#: Contract 05 §2 rule 5, and §3.3.1 rule 7 for a fault file.
STALE_AFTER_S: Final = 90

STATUS_FILE: Final = "status.json"
VALIDATION_FILE: Final = "validation.json"

#: Caps on lists that come from another process (invariant 14). A family
#: runs one or two sandboxes; twenty is far past a real document and stops
#: one malformed file from growing a page without bound.
MAX_SANDBOXES: Final = 20
MAX_FAULTS: Final = 20
MAX_ISSUES: Final = 200

#: How many characters of an image digest a row shows. Enough to tell two
#: releases apart, short enough to sit in a table cell.
DIGEST_CHARS: Final = 19

#: What a page shows for a field of an issue that holds no text.
NOT_TEXT: Final = "(not text)"


class Health(StrEnum):
    """Contract 05 §3's four states, plus the two a READER can be in.

    `unknown` is the contract's own word for a stale document (§2 rule 5).
    `unreadable` is this noticeboard's word for a file it could not parse at all,
    which the contract does not name because `caregiver` never writes one.
    """

    IN_SYNC = "in_sync"
    RECONCILING = "reconciling"
    INVALID = "invalid"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"
    UNREADABLE = "unreadable"

    @property
    def label(self) -> str:
        """The display name. `in_sync` is the JSON spelling and "in sync" is
        what a person reads (contract 05 §2.1)."""
        return "in sync" if self is Health.IN_SYNC else self.value

    @property
    def wants_attention(self) -> bool:
        return self is not Health.IN_SYNC


@dataclass(frozen=True)
class FaultRow:
    """Contract 05 §3.3."""

    code: str
    blocks_turns: bool
    since: str
    source: str
    stale: bool
    message: str
    sandbox: str

    @property
    def summary(self) -> str:
        detail = f": {self.message}" if self.message else ""
        return f"{self.code}{detail}"


@dataclass(frozen=True)
class SandboxRow:
    """Contract 05 §4.1. It carries no spend and no budget, on purpose."""

    id: str
    state: str
    power: str
    image: str
    cpus: int
    memory: str
    created_at: str
    ready_at: str
    channel: str
    has_playpen_env: bool

    @property
    def serves_new_turns(self) -> bool:
        """Contract 05 §4.2: only `ready` serves. `draining` finishes the
        turns it already holds."""
        return self.state == "ready"


@dataclass(frozen=True)
class SpendRow:
    """Contract 05 §7. One per family, and only ever one."""

    spend_usd: float | None
    budget_usd: float | None
    window: str
    source: str
    as_of: str
    #: Contract 05 §7 rule 4: a failed LiteLLM read leaves the previous
    #: numbers with their old `as_of`, and a reader must not present a stale
    #: number as current.
    stale: bool

    @property
    def known(self) -> bool:
        return self.spend_usd is not None

    @property
    def share(self) -> float | None:
        """Spend as a fraction of the budget, or None when either is
        missing or the budget is zero."""
        if self.spend_usd is None or not self.budget_usd:
            return None

        return self.spend_usd / self.budget_usd


@dataclass(frozen=True)
class ValidationRow:
    """Contract 05 §3.2's block, which points at the full report."""

    rev: str
    checked_at: str
    ok: bool
    never_valid: bool
    error_count: int
    warning_count: int
    report_path: str
    first_error: str


@dataclass(frozen=True)
class IssueRow:
    """One issue of contract 01 §7, as a page shows it."""

    loc: str
    msg: str


@dataclass(frozen=True)
class ReconcileRow:
    """Contract 05 §3.4. Null unless the family is `reconciling`."""

    since: str
    from_rev: str
    to_rev: str
    step: str
    attempts: int
    needs_switch: bool


@dataclass(frozen=True)
class LimitsRow:
    max_running_turns: int | None
    max_queued_turns: int | None
    job_timeout_s: int | None


@dataclass(frozen=True)
class FamilyRow:
    """One row of the home page, and the header of the family page."""

    name: str
    kind: str
    health: Health
    reason: str
    written_at: str
    age_s: float | None
    registry_rev: str
    applied_rev: str
    config_rev: str
    epoch: int
    sandboxes: tuple[SandboxRow, ...]
    faults: tuple[FaultRow, ...]
    spend: SpendRow | None
    validation: ValidationRow | None
    reconcile: ReconcileRow | None
    limits: LimitsRow
    #: Why this row could not be read, when it could not be. Empty means the
    #: document parsed.
    problem: str

    @property
    def stale(self) -> bool:
        return self.health is Health.UNKNOWN

    @property
    def serving(self) -> SandboxRow | None:
        """Which sandbox takes a new turn (contract 05 §4.2 rule 1).

        A `ready` sandbox always wins. Otherwise the newest `creating` one,
        because running the handshake is what promotes it. Nothing else is
        dialled.
        """
        ready = [one for one in self.sandboxes if one.state == "ready"]

        if ready:
            return ready[0]

        creating = [one for one in self.sandboxes if one.state == "creating"]

        return creating[-1] if creating else None

    # There is no `turns_running` here: `caregiver` may not call
    # `GET /v1/sessions` (contract 02 §3.1), so it cannot count a running
    # turn, and a number it cannot count does not belong on the noticeboard
    # invariant 20 calls the truth. The live count belongs to `attendance`,
    # and `sessions.py` reads it per session there.

    @property
    def blocking_faults(self) -> tuple[FaultRow, ...]:
        return tuple(one for one in self.faults if one.blocks_turns)

    @property
    def live_sandboxes(self) -> tuple[SandboxRow, ...]:
        """Everything the document still names as existing."""
        return tuple(one for one in self.sandboxes if one.state != "gone")


@dataclass(frozen=True)
class OutcomeRow:
    """Contract 02 §13.1. Autonomous families only."""

    id: str
    family: str
    session: str
    trigger: str
    started_at: str
    ended_at: str
    status: str
    error: str
    turns: int
    spend_usd: float | None
    sandbox: str
    problem: str


def family_names(families_dir: Path) -> tuple[str, ...]:
    """Every family, found by listing the directory (contract 05 §2 rule 6).

    No index file exists, so no index can go stale. A directory that cannot
    be listed answers empty: the caller reports that separately, because
    "no families" and "cannot look" are different sentences.
    """
    try:
        return tuple(sorted(one.name for one in families_dir.iterdir() if one.is_dir()))
    except OSError:
        return ()


def listing_problem(families_dir: Path) -> str:
    """Why the families directory cannot be listed, or an empty string.

    `family_names` answers empty for such a directory. A page asks here, so
    that it does not say "no families" for a directory it cannot read.
    """
    try:
        next(families_dir.iterdir(), None)
    except OSError as error:
        return f"cannot list the families directory: {error.strerror or error}"

    return ""


def read_family(families_dir: Path, name: str, now: datetime) -> FamilyRow:
    """One family's status document, read as untrusted input."""
    path = families_dir / name / STATUS_FILE
    body, problem = jsonfiles.read_object(path)

    if body is None:
        return _unreadable(name, problem or jsonfiles.missing(STATUS_FILE))

    return _row(name, body, now)


def read_families(families_dir: Path, now: datetime) -> tuple[FamilyRow, ...]:
    """Every family, in name order. One bad document never hides the rest."""
    return tuple(read_family(families_dir, name, now) for name in family_names(families_dir))


def read_report(path: Path) -> tuple[tuple[IssueRow, ...], str]:
    """Contract 01 §7's issue list from the file §3.2 points at.

    Answers the issues and a problem string. A report is only read when the
    status document names one, so a missing file here IS worth reporting.
    """
    body, problem = jsonfiles.read_object(path)

    if body is None:
        return (), problem or jsonfiles.missing(path.name)

    issues = jsonfiles.children(body, "issues", MAX_ISSUES)

    return tuple(IssueRow(_issue_text(one, "loc"), _issue_text(one, "msg")) for one in issues), ""


def _issue_text(issue: Json, key: str) -> str:
    """One field of an issue, for a page. A field that is not there reads
    empty. A value that is no text reads as a marker: its own form has no
    bound, and a page shows only what a helper of `jsonfiles` bounds."""
    if key not in issue:
        return ""

    if not isinstance(issue[key], str):
        return NOT_TEXT

    return jsonfiles.text(issue, key)


def read_outcomes(outcomes_dir: Path, family: str, limit: int) -> tuple[OutcomeRow, ...]:
    """The newest outcome records (contract 02 §13.1).

    The file name is a ULID, and a ULID sorts by time, so newest-first is a
    reverse sort on the name and costs no `stat` call.
    """
    directory = outcomes_dir / family

    try:
        names = sorted((one.name for one in directory.glob("*.json")), reverse=True)
    except OSError:
        return ()

    return tuple(_outcome(directory / name) for name in names[:limit])


def _outcome(path: Path) -> OutcomeRow:
    body, problem = jsonfiles.read_object(path)
    name = path.stem

    if body is None:
        return OutcomeRow(
            id=name,
            family="",
            session="",
            trigger="",
            started_at="",
            ended_at="",
            status="",
            error="",
            turns=0,
            spend_usd=None,
            sandbox="",
            problem=problem or jsonfiles.missing(path.name),
        )

    trigger = jsonfiles.child(body, "trigger")

    return OutcomeRow(
        id=jsonfiles.whole(body, "id") or name,
        family=jsonfiles.whole(body, "family"),
        session=jsonfiles.whole(body, "session"),
        trigger=_trigger_text(trigger),
        started_at=jsonfiles.whole(body, "started_at"),
        ended_at=jsonfiles.whole(body, "ended_at"),
        status=jsonfiles.whole(body, "status"),
        error=jsonfiles.text(body, "error"),
        turns=jsonfiles.integer(body, "turns"),
        spend_usd=jsonfiles.number(body, "spend_usd"),
        sandbox=jsonfiles.whole(body, "sandbox"),
        problem="",
    )


def _trigger_text(trigger: Json) -> str:
    kind = jsonfiles.whole(trigger, "kind")
    named = jsonfiles.whole(trigger, "name")

    if kind and named:
        return f"{kind} {named}"

    return kind or named


def _unreadable(name: str, problem: str) -> FamilyRow:
    """A family whose document this noticeboard could not parse. Every field a page
    touches still exists, so a template never has to guard."""
    return FamilyRow(
        name=name,
        kind="",
        health=Health.UNREADABLE,
        reason=problem,
        written_at="",
        age_s=None,
        registry_rev="",
        applied_rev="",
        config_rev="",
        epoch=0,
        sandboxes=(),
        faults=(),
        spend=None,
        validation=None,
        reconcile=None,
        limits=LimitsRow(None, None, None),
        problem=problem,
    )


def _row(name: str, body: Json, now: datetime) -> FamilyRow:
    written = jsonfiles.moment(body, "written_at")
    age = jsonfiles.age_s(written, now)
    validation = _validation(body)
    faults = _faults(body)
    reconcile = _reconcile(body)
    health = _health(body, age)

    return FamilyRow(
        name=jsonfiles.whole(body, "family") or name,
        kind=jsonfiles.whole(body, "kind"),
        health=health,
        reason=_reason(health, age, validation, faults, reconcile),
        written_at=jsonfiles.whole(body, "written_at"),
        age_s=age,
        registry_rev=jsonfiles.whole(body, "registry_rev"),
        applied_rev=jsonfiles.whole(body, "applied_rev"),
        config_rev=jsonfiles.whole(body, "config_rev"),
        epoch=jsonfiles.integer(jsonfiles.child(body, "credentials"), "epoch"),
        sandboxes=_sandboxes(body),
        faults=faults,
        spend=_spend(body, written, now),
        validation=validation,
        reconcile=reconcile,
        limits=_limits(body),
        problem="",
    )


def _health(body: Json, age: float | None) -> Health:
    """A stale document answers `unknown` whatever its `state` says.

    Contract 05 §2 rule 5 is explicit: past 90 seconds `caregiver` is not
    running, and the reader must not show the stale state as current.
    """
    if age is None or age > STALE_AFTER_S:
        return Health.UNKNOWN

    stated = jsonfiles.whole(body, "state")

    try:
        return Health(stated)
    except ValueError:
        # A state outside contract 05 §3's table is a document this noticeboard
        # does not understand, not a family that is fine.
        return Health.UNREADABLE


def _reason(
    health: Health,
    age: float | None,
    validation: ValidationRow | None,
    faults: tuple[FaultRow, ...],
    reconcile: ReconcileRow | None,
) -> str:
    """One line that says why the row is not `in sync`."""
    if health is Health.UNKNOWN:
        if age is None:
            return "no written_at in the status document; caregiver may not be running"

        return f"caregiver last wrote {int(age)}s ago, past the {STALE_AFTER_S}s limit"

    if health is Health.INVALID and validation is not None:
        never = "no revision of this family ever validated; " if validation.never_valid else ""
        return f"{never}{validation.first_error or 'see the validation report'}"

    if health is Health.DEGRADED and faults:
        blocking = [one for one in faults if one.blocks_turns]
        return (blocking[0] if blocking else faults[0]).summary

    if health is Health.RECONCILING and reconcile is not None:
        return f"step {reconcile.step}, attempt {reconcile.attempts}"

    return ""


def _sandboxes(body: Json) -> tuple[SandboxRow, ...]:
    return tuple(
        SandboxRow(
            id=jsonfiles.whole(one, "id"),
            state=jsonfiles.whole(one, "state"),
            power=jsonfiles.whole(one, "power"),
            image=jsonfiles.whole(one, "image")[:DIGEST_CHARS],
            cpus=jsonfiles.integer(one, "cpus"),
            memory=jsonfiles.whole(one, "memory"),
            created_at=jsonfiles.whole(one, "created_at"),
            ready_at=jsonfiles.whole(one, "ready_at"),
            channel=jsonfiles.whole(one, "channel"),
            # Contract 05 §4.1.1 rule 4: a sandbox row without this path is
            # a fault, because `attendance` cannot build a channel command.
            has_playpen_env=bool(jsonfiles.whole(one, "supervisor_env")),
        )
        for one in jsonfiles.children(body, "sandboxes", MAX_SANDBOXES)
    )


def _faults(body: Json) -> tuple[FaultRow, ...]:
    return tuple(
        FaultRow(
            code=jsonfiles.whole(one, "code"),
            blocks_turns=jsonfiles.flag(one, "blocks_turns"),
            since=jsonfiles.whole(one, "since"),
            source=jsonfiles.whole(one, "source"),
            stale=jsonfiles.flag(one, "stale"),
            message=jsonfiles.text(one, "message"),
            sandbox=jsonfiles.whole(one, "sandbox"),
        )
        for one in jsonfiles.children(body, "faults", MAX_FAULTS)
    )


def _spend(body: Json, written: datetime | None, now: datetime) -> SpendRow | None:
    """Contract 05 §7. `spend: null` is legal and means "nothing published"."""
    spend = jsonfiles.block(body, "spend")

    if spend is None:
        return None

    as_of = jsonfiles.moment(spend, "as_of")
    reference = written if written is not None else now

    return SpendRow(
        spend_usd=jsonfiles.number(spend, "spend_usd"),
        budget_usd=jsonfiles.number(spend, "budget_usd"),
        window=jsonfiles.whole(spend, "window"),
        source=jsonfiles.whole(spend, "source"),
        as_of=jsonfiles.whole(spend, "as_of"),
        # Rule 4 asks a reader to compare `as_of` with `written_at`. The
        # limit is §2's own 90 seconds: the document is rewritten at least
        # every 30, so a spend number three writes behind is not current.
        stale=_lags(as_of, reference),
    )


def _lags(as_of: datetime | None, reference: datetime) -> bool:
    if as_of is None:
        return True

    return (reference - as_of).total_seconds() > STALE_AFTER_S


def _validation(body: Json) -> ValidationRow | None:
    found = jsonfiles.block(body, "validation")

    if found is None:
        return None

    return ValidationRow(
        rev=jsonfiles.whole(found, "rev"),
        checked_at=jsonfiles.whole(found, "checked_at"),
        ok=jsonfiles.flag(found, "ok"),
        never_valid=jsonfiles.flag(found, "never_valid"),
        error_count=jsonfiles.integer(found, "error_count"),
        warning_count=jsonfiles.integer(found, "warning_count"),
        report_path=jsonfiles.whole(found, "report_path"),
        first_error=jsonfiles.text(found, "first_error"),
    )


def _reconcile(body: Json) -> ReconcileRow | None:
    found = jsonfiles.block(body, "reconcile")

    if found is None:
        return None

    return ReconcileRow(
        since=jsonfiles.whole(found, "since"),
        from_rev=jsonfiles.whole(found, "from_rev"),
        to_rev=jsonfiles.whole(found, "to_rev"),
        step=jsonfiles.whole(found, "step"),
        attempts=jsonfiles.integer(found, "attempts"),
        needs_switch=jsonfiles.flag(found, "needs_switch"),
    )


def _limits(body: Json) -> LimitsRow:
    found = jsonfiles.child(body, "limits")

    return LimitsRow(
        max_running_turns=_optional_int(found, "max_running_turns"),
        max_queued_turns=_optional_int(found, "max_queued_turns"),
        job_timeout_s=_optional_int(found, "job_timeout_s"),
    )


def _optional_int(found: Json, key: str) -> int | None:
    """`limits` is null throughout for an attended family (contract 05 §9),
    so absent and null must stay distinguishable from zero."""
    value = found.get(key)

    if isinstance(value, bool) or not isinstance(value, int):
        return None

    return value
