"""One reconcile pass over one family (contract 05).

The operator's invariant 9 is the point of the whole rework: "A permission change
needs one action and no manual apply. It never ends a session. A removal
applies at once." This module is that sentence as code.

    registry file  --classify()-->  the steps of contract 05 §3.4

A pass writes files and makes key calls. It starts and stops no VM: the
vmm starts one VM per session at its first prompt and stops it itself
(decisions CH1, CH21-1, contract 05 §3.4). A new shape reaches a VM at its
next start (contract 05 §3.5).

Three rules shape every branch below.

1. **The applied snapshot moves last.** An interrupted pass leaves the old
   snapshot, so the next pass computes the same diff and repeats the same
   steps. Every step is idempotent, so repeating is free.
2. **Narrowing before widening.** An interruption then leaves LESS reach
   than the target, never more (invariant 9). Egress removals run before
   egress allows, and a grant rewrite carries both directions in one
   atomic file.
3. **An invalid file changes nothing** (invariant 19). The last good
   definition keeps serving and the report says why."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from agent_family import (
    Diff,
    FamilyFile,
    FamilyState,
    Index,
    Issue,
    Registry,
    Report,
    Severity,
    Step,
    classify,
)
from agent_family.registry import FAMILY_FILE

from . import paths, reach_files, steps
from .applied import AppliedState, read_applied, write_applied
from .chaperone_watch import PepReport, unwatched
from .clock import now_rfc3339
from .credentials import Credentials
from .faults import FaultEntry
from .litellm_keys import SPEND_WINDOW, LiteLLMError, LiteLLMKeys, Spend
from .policy_file import PolicyShapeError
from .policy_fleet import FleetPolicy, LeftOut
from .status import (
    CredentialsBlock,
    LimitsBlock,
    ReconcileBlock,
    ReconcileStep,
    StatusDocument,
    TriggersBlock,
    ValidationBlock,
    accepts_dispatch,
    write_status,
    write_validation_report,
)
from .steps import WHOLE_FILE, KeyRefresh
from .timers import TimerOutcome, UnitWriter, apply_timers
from .webhook_tokens import WebhookToken, ensure_webhooks

log = logging.getLogger("caregiver.reconcile")

#: Contract 05 §3.2: `first_error` is a convenience for the noticeboard.
FIRST_ERROR_CHARS: Final = 200

#: Contract 05 §3.3. A generated timer systemd does not report as enabled.
#: It never fires, and a schedule that does not run leaves no other trace.
TIMER_ENABLE_FAILED: Final = "timer_enable_failed"


class SpendRead(StrEnum):
    """Contract 05 §7 rule 3: a one-shot apply publishes `spend: null` and
    raises no `spend_unknown`, because nothing was attempted that could
    fail. Spend over time is the watch loop's fact."""

    READ = "read"
    SKIP = "skip"


class FamilyNotFoundError(RuntimeError):
    """The registry holds no `family.toml` for the name: no
    `families/<name>/` directory, or a directory with no file. That is not
    the same as an invalid `family.toml`, which gets a report."""


def has_family_file(registry: Registry, name: str) -> bool:
    """Whether the registry holds a `family.toml` for `name`.

    Contract 01 §5.6 rule 3: a family directory with no `family.toml` is a
    warning, and `caregiver` ignores it. The registry gives that directory
    a report with no error and no parsed file. A file that does not parse
    has an error, and a file that parses is in `registry.families`."""
    report = registry.reports.get(name)
    if report is None:
        return False

    return name in registry.families or not report.ok


@dataclass(frozen=True)
class Actors:
    """Everything outside this process that a reconcile may touch. Each is
    a Protocol with a fake, so no test needs a host.

    No field has a default. A defaulted `units` would let a caller that
    forgot it write no timer and say nothing, and an autonomous family
    whose schedule silently never fires is the failure nobody goes
    looking for."""

    litellm: LiteLLMKeys
    units: UnitWriter
    policy: FleetPolicy


@dataclass(frozen=True)
class ReconcileResult:
    """What one pass did. `ran` is the step list for the pass's one log
    line: family, change class, action, result."""

    family: str
    status: StatusDocument
    ran: tuple[ReconcileStep, ...]
    note: str

    @property
    def ok(self) -> bool:
        return self.status.state is FamilyState.IN_SYNC

    def noted(self, note: str) -> ReconcileResult:
        return replace(self, note=note)

    def log_line(self) -> str:
        steps_text = ",".join(self.ran) if self.ran else "-"
        return f"{self.family}: {self.note} steps={steps_text} state={self.status.state}"


def reconcile_family(
    registry: Registry,
    family_name: str,
    *,
    state_root: Path,
    actors: Actors,
    spend: SpendRead = SpendRead.SKIP,
    chaperone: PepReport | None = None,
) -> ReconcileResult:
    """Validate, diff, act, publish. Never raises on content.

    `chaperone` is the fleet's one PEP reading, taken by the loop's watch and
    handed to every family's pass (contract 05 §3.3 `pep_unreachable`). A
    caller that passes none publishes `pep.watch: off`, because it has no
    interval to probe over and may not claim the PEP answered."""
    report = registry.reports.get(family_name)
    if report is None or not has_family_file(registry, family_name):
        raise FamilyNotFoundError(family_name)

    watch = chaperone if chaperone is not None else unwatched()
    write_validation_report(paths.validation_path(state_root, family_name), report)
    folded = (*steps.read_faults(state_root, family_name), *_pep_faults(watch))
    applied = read_applied(state_root, family_name)

    if not report.ok:
        return _keep_last_good(
            state_root, family_name, report, registry.revision, folded, applied, watch
        )

    # A refusal needs the OLD file to prove it, so both halves are checked:
    # no snapshot, no diff, and no way for `name` or `kind` to have moved.
    family = registry.families[family_name]
    diff = classify(applied.family, family) if applied is not None else None
    if applied is not None and diff is not None and diff.refused:
        return _refuse_immutable(
            state_root, family, registry.revision, folded, applied, diff, watch
        )

    return _converge(
        registry=registry,
        family=family,
        report=report,
        applied=applied,
        diff=diff,
        state_root=state_root,
        actors=actors,
        folded=folded,
        spend=spend,
        chaperone=watch,
    )


# --- the pass ---------------------------------------------------------------


def _converge(
    *,
    registry: Registry,
    family: FamilyFile,
    report: Report,
    applied: AppliedState | None,
    diff: Diff | None,
    state_root: Path,
    actors: Actors,
    folded: tuple[FaultEntry, ...],
    spend: SpendRead,
    chaperone: PepReport,
) -> ReconcileResult:
    ran: list[ReconcileStep] = []
    faults = list(folded)

    # The app table of `policy.toml` has a shape check, and it comes before
    # the key step. A pass that refuses the table then ran no step:
    # LiteLLM, `creds.json` and the last table stay as they are (contract
    # 05 §3.1).
    index = _index_of(registry)
    try:
        steps.check_policy(family, index)
    except PolicyShapeError as refusal:
        return _refuse(
            state_root, report, registry.revision, folded, applied, chaperone, str(refusal)
        ).noted("refused: app table of policy.toml")

    # Decision CH71: an `egress.json` over the cap of the vmm is refused
    # here too, before the key step, so no write lands and the last good
    # file stays (invariant 19).
    try:
        egress_file = reach_files.render_egress_file(family)
    except reach_files.EgressTooLargeError as refusal:
        return _refuse(
            state_root, report, registry.revision, folded, applied, chaperone, str(refusal)
        ).noted("refused: egress.json over its cap")

    refresh = KeyRefresh.UPDATE if _needs(diff, Step.UPDATE_KEY) else KeyRefresh.KEEP
    creds, block, key_faults = steps.ensure_credentials(family, state_root, actors.litellm, refresh)
    faults.extend(key_faults)
    if creds is None:
        # Invariant 13 orders a DELETE, credentials before the VMs. A
        # failed MINT is the opposite case: nothing downstream depends on a
        # key that does not exist, so there is nothing more to do.
        return _publish(
            state_root,
            family,
            report,
            registry.revision,
            applied,
            tuple(faults),
            block,
            ran=(),
            note="key_mint_failed",
            spend_block=None,
            chaperone=chaperone,
        )

    if refresh is KeyRefresh.UPDATE and applied is not None:
        ran.append(ReconcileStep.UPDATE_KEY)

    # The table of the family in `policy.toml` is landed on each pass. A
    # server file moves what `all` grants while the family file stays put,
    # and `land_policy` writes nothing when the table did not move. A new
    # table that the check refuses is not written, so the last good table
    # stays in `policy.toml`. A family left out reads `invalid` with the one
    # error line of the check or of the assembly, and the pass goes no
    # further.
    standing = steps.land_policy(
        family, index, state_root, creds, actors.policy, rev=registry.revision
    )
    if isinstance(standing, LeftOut):
        return _refuse(
            state_root, report, registry.revision, folded, applied, chaperone, standing.reason
        ).noted("refused: left out of policy.toml")

    if _needs(diff, Step.WRITE_GRANTS):
        ran.append(ReconcileStep.WRITE_GRANTS)

    if steps.write_config(family, state_root, *_config_text(registry, family)):
        ran.append(ReconcileStep.WRITE_CONFIG)

    # Contract 01 §6.1.1, contract 05 §3.4's `set_egress`: on every pass,
    # so each file holds the list of the revision this pass converges to
    # (decision CH69 rule 6). The vmm reads both files (decision CH6-5).
    egress_moved = reach_files.write_egress_file(egress_file, state_root)
    repos_moved = reach_files.write_repos_file(family, state_root)
    if egress_moved or repos_moved:
        ran.append(ReconcileStep.SET_EGRESS)

    timers = apply_timers(family, actors.units)
    if timers.changed:
        ran.append(ReconcileStep.WRITE_TIMERS)

    timer_faults = _timer_faults(family, timers)
    faults.extend(timer_faults)

    # Contract 05 §6.4. Beside the timers, because a cron trigger and a
    # webhook trigger are the same declaration wearing two faces: one needs
    # a systemd unit, the other needs a bearer.
    webhooks = ensure_webhooks(family, state_root)

    # Decision CH23-1: `runtime.json` is the one home of `model_live_at`.
    settled = steps.model_live_at(family.name, state_root)
    family_file = _family_text(registry, report)
    done = family_file is not None
    if family_file is not None:
        write_applied(state_root, family.name, rev=registry.revision, family_text=family_file)

    # Read spend BEFORE the faults are frozen: a failed read raises
    # `spend_unknown`, and a fault the document does not carry is a fault
    # nobody sees (contract 05 §7 rule 4).
    spend_block = _spend_block(creds, actors.litellm, spend, faults)
    return _publish(
        state_root,
        family,
        report,
        registry.revision,
        applied if not done else None,
        tuple(faults),
        block,
        ran=tuple(ran),
        note=_note(diff, done),
        spend_block=spend_block,
        settled=settled,
        applied_now=done,
        webhooks=webhooks,
        chaperone=chaperone,
    )


# --- the three refusals -------------------------------------------------------


def _keep_last_good(
    state_root: Path,
    family_name: str,
    report: Report,
    revision: str,
    folded: tuple[FaultEntry, ...],
    applied: AppliedState | None,
    chaperone: PepReport,
) -> ReconcileResult:
    """Invariant 19: a bad definition yields a report. The last good
    definition keeps serving and nothing is touched (contract 05 §3.1)."""
    validation = ValidationBlock(
        rev=revision,
        checked_at=now_rfc3339(),
        ok=False,
        never_valid=applied is None,
        error_count=report.errors,
        warning_count=report.warnings,
        report_path=str(paths.validation_path(state_root, family_name)),
        first_error=report.first_error,
    )
    doc = StatusDocument(
        family=family_name,
        kind=applied.family.kind if applied is not None else "",
        state=FamilyState.INVALID,
        written_at=now_rfc3339(),
        registry_rev=revision,
        applied_rev=applied.rev if applied is not None else "",
        config_rev=applied.rev if applied is not None else "",
        validation=validation,
        faults=folded,
        # A rotation does not wait for a valid file, so the epoch is read
        # on every pass. Without it `attendance` sends epoch 1, and a
        # resident process keeps the key the rotation deleted.
        credentials=steps.kept_credentials(family_name, state_root),
        # The last good definition is what serves, so its limits are what
        # `attendance` must still enforce. Publishing the defaults instead
        # would silently move `max_running_turns` on a family that never
        # changed (contract 05 §3.1).
        limits=steps.limits_for(applied.family) if applied is not None else LimitsBlock(),
        chaperone=chaperone,
    )
    write_status(paths.status_path(state_root, family_name), doc)
    return ReconcileResult(family_name, doc, (), "invalid")


def _refuse_immutable(
    state_root: Path,
    family: FamilyFile,
    revision: str,
    folded: tuple[FaultEntry, ...],
    applied: AppliedState,
    diff: Diff,
    chaperone: PepReport,
) -> ReconcileResult:
    """`name` and `kind` cannot move (contract 01 §3.1). Only the OLD
    revision proves it, so no single-file validation can catch this and
    the reconciler has to.

    CONTRACT-QUESTION: contract 05 §3 has no state for it.
    `invalid` is the one that keeps the last good definition serving,
    which is what §3.1 wants for every other bad edit."""
    first = "; ".join(one.detail for one in diff.refused)[:FIRST_ERROR_CHARS]
    validation = ValidationBlock(
        rev=revision,
        checked_at=now_rfc3339(),
        ok=False,
        never_valid=False,
        error_count=len(diff.refused),
        warning_count=0,
        report_path=str(paths.validation_path(state_root, family.name)),
        first_error=first,
    )
    doc = StatusDocument(
        family=family.name,
        kind=applied.family.kind,
        state=FamilyState.INVALID,
        written_at=now_rfc3339(),
        registry_rev=revision,
        applied_rev=applied.rev,
        config_rev=applied.rev,
        validation=validation,
        faults=folded,
        credentials=steps.kept_credentials(family.name, state_root),
        limits=steps.limits_for(applied.family),
        chaperone=chaperone,
    )
    write_status(paths.status_path(state_root, family.name), doc)
    return ReconcileResult(family.name, doc, (), "refused: immutable field moved")


def _refuse(
    state_root: Path,
    report: Report,
    revision: str,
    folded: tuple[FaultEntry, ...],
    applied: AppliedState | None,
    chaperone: PepReport,
    reason: str,
) -> ReconcileResult:
    """One more error of the validation report, and the family reads
    `invalid`. The last applied state keeps serving."""
    issue = Issue(Severity.ERROR, WHOLE_FILE, reason)
    refused = Report(
        family=report.family,
        file=report.file,
        issues=(*report.issues, issue),
        state=FamilyState.INVALID,
    )
    write_validation_report(paths.validation_path(state_root, report.family), refused)
    return _keep_last_good(state_root, report.family, refused, revision, folded, applied, chaperone)


# --- publishing ---------------------------------------------------------------


def _publish(
    state_root: Path,
    family: FamilyFile,
    report: Report,
    revision: str,
    applied: AppliedState | None,
    faults: tuple[FaultEntry, ...],
    credentials: CredentialsBlock | None,
    *,
    ran: tuple[ReconcileStep, ...],
    note: str,
    spend_block: dict[str, Any] | None,
    settled: str | None = None,
    applied_now: bool = False,
    webhooks: tuple[WebhookToken, ...] = (),
    chaperone: PepReport,
) -> ReconcileResult:
    """Contract 05 §2: one document, rewritten atomically on every state
    change."""
    applied_rev = revision if applied_now else (applied.rev if applied is not None else "")
    state = _state_of(faults, applied_rev, revision, settled)
    validation = ValidationBlock(
        rev=revision,
        checked_at=now_rfc3339(),
        ok=True,
        never_valid=False,
        error_count=0,
        warning_count=report.warnings,
        report_path=str(paths.validation_path(state_root, family.name)),
        first_error=None,
    )
    doc = StatusDocument(
        family=family.name,
        kind=str(family.kind),
        state=state,
        written_at=now_rfc3339(),
        registry_rev=revision,
        applied_rev=applied_rev,
        config_rev=applied_rev,
        validation=validation,
        credentials=credentials,
        limits=steps.limits_for(family),
        triggers=TriggersBlock(webhooks, enqueue=accepts_dispatch(family)),
        faults=faults,
        reconcile=_reconcile_block(state, applied_rev, revision, ran),
        spend=spend_block,
        chaperone=chaperone,
    )
    write_status(paths.status_path(state_root, family.name), doc)
    return ReconcileResult(family.name, doc, ran, note)


def _state_of(
    faults: tuple[FaultEntry, ...],
    applied_rev: str,
    revision: str,
    settled: str | None,
) -> FamilyState:
    if faults:
        return FamilyState.DEGRADED

    if applied_rev != revision:
        return FamilyState.RECONCILING

    # Contract 05 §7 rule 6: a model change is not live until LiteLLM's
    # worker cache has turned over, so `in_sync` may not be claimed yet.
    if settled is not None and now_rfc3339() < settled:
        return FamilyState.RECONCILING

    return FamilyState.IN_SYNC


def _reconcile_block(
    state: FamilyState, applied_rev: str, revision: str, ran: tuple[ReconcileStep, ...]
) -> ReconcileBlock | None:
    """Contract 05 §3.4. Null unless `reconciling` (§2.1). A pass that ran
    no step names `validate`: it validated the file, and it waits for the
    model cache (§7 rule 6) or for a family file named `family.toml`."""
    if state is not FamilyState.RECONCILING:
        return None

    return ReconcileBlock(
        since=now_rfc3339(),
        from_rev=applied_rev,
        to_rev=revision,
        step=ran[-1] if ran else ReconcileStep.VALIDATE,
        attempts=1,
    )


def _spend_block(
    creds: Credentials,
    litellm: LiteLLMKeys,
    spend: SpendRead,
    faults: list[FaultEntry],
) -> dict[str, Any] | None:
    """Contract 05 §7. A failed read raises the non-blocking fault
    `spend_unknown` and publishes nothing new, so a reader comparing
    `as_of` with `written_at` never sees a stale number as current."""
    if spend is SpendRead.SKIP:
        return None

    try:
        read: Spend = litellm.read_spend(creds.litellm_key)
    except LiteLLMError as exc:
        faults.append(
            FaultEntry(
                code="spend_unknown",
                blocks_turns=False,
                since=now_rfc3339(),
                source="caregiver",
                detail={"message": str(exc)},
            )
        )
        return None

    return {
        "window": SPEND_WINDOW,
        "spend_usd": read.spend_usd,
        "budget_usd": read.budget_usd,
        "as_of": now_rfc3339(),
        "source": "litellm",
    }


# --- small answers ---------------------------------------------------------------


def _pep_faults(chaperone: PepReport) -> tuple[FaultEntry, ...]:
    """Contract 05 §3.3's `pep_unreachable`, raised on EVERY family.

    One PEP serves the whole fleet, so one outage is every family's
    outage — the same reading §3.3 gives `audit_unreadable`. `caregiver`
    clears it itself at the first answer: no turn, no restart and no
    human, because a family whose tool calls all fail is not a family
    anyone is about to dial on purpose.

    It does NOT block turns. A chat with no tools still answers, a PEP
    restart of a few seconds would otherwise become refused turns across
    eleven families, and §3.3.1 rule 8 exists because this contract has
    already met three blocking faults nothing could clear. The cost is
    real and belongs in the row, not in a comment: an autonomous family
    that fires during an outage runs a turn that burns model spend with
    no tools."""
    fault = chaperone.fault()

    return (fault,) if fault is not None else ()


def _timer_faults(family: FamilyFile, timers: TimerOutcome) -> list[FaultEntry]:
    """Contract 05 §3.3's `timer_enable_failed`, one entry for the pass.

    A timer file nothing enabled never fires, and until this the pass said
    nothing at all about it — the failure nobody goes looking for, because
    a schedule that does not run leaves no trace anywhere.

    It does NOT block turns. A missed wake is not a permission, and taking
    an autonomous family off the air over one would turn a lost schedule
    into a lost family.

    A cron line with no `OnCalendar` spelling is a different thing and
    still raises nothing: `timers.py` logs it and writes no unit."""
    if not timers.unenabled:
        return []

    return [
        FaultEntry(
            code=TIMER_ENABLE_FAILED,
            blocks_turns=False,
            since=now_rfc3339(),
            source="caregiver",
            detail={
                "timers": list(timers.unenabled),
                "message": (
                    f"systemd does not report {', '.join(timers.unenabled)} as enabled "
                    f"for family {family.name}; that schedule does not fire"
                ),
            },
        )
    ]


def _needs(diff: Diff | None, step: Step) -> bool:
    """A family with no applied snapshot needs every step. One with a
    snapshot needs what `classify()` names."""
    return step in diff.steps() if diff is not None else True


def _index_of(registry: Registry) -> Index:
    return Index(
        kinds={name: one.kind for name, one in registry.families.items()},
        servers=registry.servers,
        skills=registry.skills,
    )


def _config_text(registry: Registry, family: FamilyFile) -> tuple[str, dict[str, str]]:
    skills = {name: _text(registry.skill_file(name)) for name in family.skills}
    return _text(registry.instructions(family.name)), skills


def _family_text(registry: Registry, report: Report) -> str | None:
    """The text of the family file that the loader read. The report of the
    family names that file. `None` for a file that is not named
    `family.toml`. The pass is then not done: it writes no snapshot, and
    the status document keeps the old revision as `applied_rev`."""
    path = registry.root / report.file
    if path.name != FAMILY_FILE:
        return None

    return _text(path)


def _text(path: Path) -> str:
    """An unreadable registry file reads empty rather than raising. The
    validation report is where a missing file is reported (invariant 19)."""
    # CONTRACT-QUESTION: contract 01 §6.1 has no rule for an instructions
    # file or a skill file that is not UTF-8 text, and the validator does
    # not refuse one. The reading here is that of a file that this pass
    # cannot open, and of the registry revision: an empty file. The pass
    # then writes an empty file into the config mount and reports nothing.
    # To keep the last good text, the validator must refuse the file, so
    # that the family reads `invalid`.
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _note(diff: Diff | None, done: bool) -> str:
    if diff is None:
        return "first apply" if done else "first apply incomplete"

    return "live" if diff.changed else "no change"
