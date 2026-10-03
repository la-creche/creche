"""One reconcile pass over one family (contract 05).

The operator's invariant 9 is the point of the whole rework: "A permission change
needs one action and no manual apply. It never ends a session. A removal
applies at once." This module is that sentence as code.

    registry file  --classify()-->  live axes        (nothing is replaced)
                                \\-->  replace axes   (a new sandbox, a switch)

Four rules shape every branch below.

1. **The applied snapshot moves last.** An interrupted pass leaves the old
   snapshot, so the next pass computes the same diff and repeats the same
   steps. Every step is idempotent, so repeating is free.
2. **Narrowing before widening.** An interruption then leaves LESS reach
   than the target, never more (invariant 9). Egress removals run before
   egress allows, and a grant rewrite carries both directions in one
   atomic file.
3. **An invalid file changes nothing** (invariant 19). The last good
   definition keeps serving and the report says why.
4. **A refused switch keeps both sandboxes.** `attendance` owns the sessions.
   Destroying the outgoing sandbox on a refusal would end every one of
   them."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from agent_family import (
    Diff,
    FamilyFile,
    FamilyState,
    Index,
    Registry,
    Report,
    Step,
    SwitchMode,
    classify,
)

from . import paths, sandboxes, steps
from .applied import AppliedState, read_applied, write_applied
from .clock import now_rfc3339, seconds_from_now
from .credentials import Credentials
from .driver import SandboxDriver
from .egress import EgressConfig
from .faults import FaultEntry
from .images import SandboxImages
from .litellm_keys import SPEND_WINDOW, LiteLLMError, LiteLLMKeys, Spend
from .mcp_wire import McpReport
from .pep_watch import PepReport, unwatched
from .sandboxes import SandboxRecord
from .status import (
    CredentialsBlock,
    LimitsBlock,
    SandboxLifecycle,
    StatusDocument,
    TriggersBlock,
    ValidationBlock,
    accepts_dispatch,
    write_status,
    write_validation_report,
)
from .steps import KeyRefresh
from .switch import SwitchClient, SwitchError, SwitchRequest
from .timers import TimerOutcome, UnitWriter, apply_timers
from .webhook_tokens import WebhookToken, ensure_webhooks

log = logging.getLogger("agent_managerd.reconcile")

#: Contract 05 §7 rule 6: a `/key/update` can be served stale by another
#: LiteLLM worker for this long, so a model change is not live — and the
#: family is not `in_sync` — until it has passed. Probe 0b measured 7 s
#: across four workers on this host.
MODEL_CACHE_S: Final = 10

#: Contract 05 §3.2: `first_error` is a convenience for the noticeboard.
FIRST_ERROR_CHARS: Final = 200

#: Contract 05 §3.4's two step names that `Step` does not carry. They are
#: `managerd`'s own actions on `attendance` and on sbx, not the landing of
#: any one family-file field, so `agent_family` has no member for them.
#: `SWITCH_STEP` is re-exported from `sandboxes`, which makes the call.
SWITCH_STEP: Final = sandboxes.SWITCH_STEP
DESTROY_STEP: Final = "destroy_sandbox"

#: `triggers:` lands in the timer set, which is
#: why contract 01 §6's table gives the field `Step.NONE`: no step of
#: contract 05 §3.4 touches it.
TIMERS_STEP: Final = "write_timers"

#: Contract 05 §3.3. A generated timer systemd does not report as enabled.
#: It never fires, and a schedule that does not run leaves no other trace.
TIMER_ENABLE_FAILED: Final = "timer_enable_failed"

#: Contract 05 §4.3 step 5b. `_replace` calls this with the steps it has
#: run so far, to publish the status document BEFORE the §5 call. The
#: document is the only thing that crosses between the two services (§1),
#: so a `to` it does not name is a `to` `attendance` refuses.
PublishNow = Callable[[tuple[str, ...]], None]

#: Answers True once the service has been told to stop. A pass consults it
#: between two steps and never inside one.
StopCheck = Callable[[], bool]


def _never() -> bool:
    """The default for every caller that is not the watch loop. A one-shot
    verb has nothing to stop for: its process ends when it ends."""
    return False


class _Halted(RuntimeError):
    """A stop arrived between two steps of the sandbox axis.

    It carries the steps that DID run, so the pass still publishes one
    honest document before it returns, and the applied snapshot stays
    where it was: an interrupted pass must look interrupted, never
    finished (the module docstring's rule 1)."""

    def __init__(self, ran: tuple[str, ...]) -> None:
        super().__init__("stopped between steps")
        self.ran = ran


def _halt_if(stop: StopCheck, ran: list[str]) -> None:
    """The one place a step boundary is checked. Every call site sits
    directly before a call that can take minutes: `sbx create`, an `sbx
    policy` sweep, the §5 call, or a destroy."""
    if stop():
        raise _Halted(tuple(ran))


class SpendRead(StrEnum):
    """Contract 05 §7 rule 3: a one-shot apply publishes `spend: null` and
    raises no `spend_unknown`, because nothing was attempted that could
    fail. Spend over time is the watch loop's fact."""

    READ = "read"
    SKIP = "skip"


class FamilyNotFoundError(RuntimeError):
    """No `families/<name>/` directory in the registry at all — not the
    same as an invalid `family.yaml`, which does have a directory and gets
    a report."""


@dataclass(frozen=True)
class Actors:
    """Everything outside this process that a reconcile may touch. Each is
    a Protocol with a fake, so no test needs a host.

    No field has a default. A defaulted `units` would let a caller that
    forgot it write no timer and say nothing, and an autonomous family
    whose schedule silently never fires is the failure nobody goes
    looking for."""

    driver: SandboxDriver
    litellm: LiteLLMKeys
    switch: SwitchClient
    egress: EgressConfig
    units: UnitWriter


@dataclass(frozen=True)
class ReconcileResult:
    """What one pass did. `ran` is the step list for the pass's one log
    line: family, change class, action, result."""

    family: str
    status: StatusDocument
    ran: tuple[str, ...]
    note: str

    @property
    def ok(self) -> bool:
        return self.status.state is FamilyState.IN_SYNC

    def log_line(self) -> str:
        steps_text = ",".join(self.ran) if self.ran else "-"
        return f"{self.family}: {self.note} steps={steps_text} state={self.status.state}"


def reconcile_family(
    registry: Registry,
    family_name: str,
    *,
    state_root: Path,
    image: str,
    python_image: str = "",
    actors: Actors,
    spend: SpendRead = SpendRead.SKIP,
    stop: StopCheck = _never,
    pep: PepReport | None = None,
    mcp: McpReport | None = None,
) -> ReconcileResult:
    """Validate, diff, act, publish. Never raises on content.

    `image` and `python_image` are one reference per flavor of contract 01
    §3.9. Two arguments rather than one mapping, because they are two
    platform builds and two CLI flags, and the family file picks between
    them by name.

    `stop` is the watch loop's SIGTERM, consulted between two steps of the
    sandbox axis and never inside one.

    `pep` is the fleet's one PEP reading, taken by the loop's watch and
    handed to every family's pass (contract 05 §3.3 `pep_unreachable`). A
    caller that passes none publishes `pep.watch: off`, because it has no
    interval to probe over and may not claim the PEP answered.

    `mcp` is the fleet's one MCP pass reading, the same shape and for the
    same reason (contract 05 §3.3 `mcp_install_held`). One registry and
    one `/opt/mcp` serve every family, so a declared server nobody can
    install belongs to all of them. A caller that passes none raises the
    fault on nobody: `apply-once` files no MCP request."""
    report = registry.reports.get(family_name)
    if report is None:
        raise FamilyNotFoundError(family_name)

    watch = pep if pep is not None else unwatched()
    write_validation_report(paths.validation_path(state_root, family_name), report)
    live = _live_records(state_root, family_name)
    folded = (
        *steps.read_faults(state_root, family_name, tuple(one.id for one in live)),
        *_pep_faults(watch),
        *_mcp_faults(mcp),
    )
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
        images=SandboxImages(base=image, python=python_image),
        actors=actors,
        folded=folded,
        spend=spend,
        stop=stop,
        pep=watch,
        mcp=mcp,
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
    images: SandboxImages,
    actors: Actors,
    folded: tuple[FaultEntry, ...],
    spend: SpendRead,
    stop: StopCheck,
    pep: PepReport,
    mcp: McpReport | None,
) -> ReconcileResult:
    ran: list[str] = []
    faults = list(folded)

    refresh = KeyRefresh.UPDATE if _needs(diff, Step.UPDATE_KEY) else KeyRefresh.KEEP
    creds, block, key_faults = steps.ensure_credentials(family, state_root, actors.litellm, refresh)
    faults.extend(key_faults)
    if creds is None:
        # Invariant 13 orders a DELETE, credentials before the sandbox. A
        # failed MINT is the opposite case: nothing downstream depends on a
        # key that does not exist, so there is nothing more to do.
        return _publish(
            state_root,
            family,
            report,
            registry.revision,
            applied,
            tuple(faults),
            (),
            block,
            ran=(),
            note="key_mint_failed",
            spend_block=None,
            pep=pep,
        )

    if refresh is KeyRefresh.UPDATE and applied is not None:
        ran.append(str(Step.UPDATE_KEY))

    # A missing grant file is the PEP refusing every call for this family,
    # so it is rewritten whether or not the diff asked. So is a stale one: a
    # server file moves what `all` grants while the family file stays put.
    # It is not rewritten on every pass: `write_grants` mints a fresh `rev`
    # each call, and the loop looks every two seconds.
    index = _index_of(registry)
    if _needs(diff, Step.WRITE_GRANTS) or not steps.grants_current(family, index, state_root):
        steps.write_grants(family, index, state_root, creds)
        ran.append(str(Step.WRITE_GRANTS))

    if steps.write_config(family, state_root, *_config_text(registry, family)):
        ran.append(str(Step.WRITE_CONFIG))

    timers = apply_timers(family, actors.units)
    if timers.changed:
        ran.append(TIMERS_STEP)

    timer_faults = _timer_faults(family, timers)
    faults.extend(timer_faults)

    # Contract 05 §6.4. Beside the timers, because a cron trigger and a
    # webhook trigger are the same declaration wearing two faces: one needs
    # a systemd unit, the other needs a bearer.
    webhooks = ensure_webhooks(family, state_root)
    so_far_faults = tuple(faults)

    def publish_now(step_list: tuple[str, ...]) -> None:
        """Contract 05 §4.3 step 5b, for the moment between the create and
        the §5 call. The pass is not done, so the applied snapshot does not
        move and the family is `reconciling`, WHATEVER moved. `in_flight`
        says so: when only the image moved, the registry did not, the two
        revisions are equal, and without it the document would read
        `in_sync` from inside a three-minute create."""
        _publish(
            state_root,
            family,
            report,
            registry.revision,
            applied,
            so_far_faults,
            _live_records(state_root, family.name),
            block,
            ran=step_list,
            note="replace",
            spend_block=None,
            webhooks=webhooks,
            in_flight=True,
            pep=pep,
        )

    halted = False
    # Contract 01 §3.9. Resolved here and not inside the sandbox axis, so
    # the steps above it — the grant file above all — still run for a
    # family this host cannot build a sandbox for.
    image = images.resolve(family.sandbox.image)
    if image is None:
        live, sandbox_faults, ran_sandbox = (
            None,
            (sandboxes.unconfigured_image_fault(family, images),),
            (),
        )
    else:
        try:
            live, sandbox_faults, ran_sandbox = _sandboxes_pass(
                family=family,
                applied=applied,
                diff=diff,
                state_root=state_root,
                image=image,
                actors=actors,
                publish=publish_now,
                stop=stop,
            )
        except _Halted as cut:
            halted, live, sandbox_faults, ran_sandbox = True, None, (), cut.ran

    ran.extend(ran_sandbox)

    # Both fault files are read AGAIN, now the pass has acted. A §5 call
    # completes a channel handshake, and contract 05 §3.3.1 rule 8 makes that
    # clear every fault `attendance` raised for the family. Publishing the
    # entries read at the top of the pass would report a fault this pass
    # itself just cleared, and leave the family `degraded` until the next one.
    records = _live_records(state_root, family.name)
    faults = [
        *steps.read_faults(state_root, family.name, tuple(one.id for one in records)),
        *_pep_faults(pep),
        *_mcp_faults(mcp),
        *key_faults,
        *timer_faults,
        *sandbox_faults,
    ]

    settled = _model_live_at(applied, diff, ran)
    done = not halted and live is not None and not sandbox_faults
    # `done` already implies a resolved image — an unresolved one is a
    # fault, and a fault is not done. The second half is what lets the
    # type say so.
    if done and image is not None:
        write_applied(
            state_root,
            family.name,
            rev=registry.revision,
            image=image,
            family_text=_family_text(registry, family.name),
            model_live_at=settled,
        )

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
        records,
        block,
        ran=tuple(ran),
        note="stopped between steps" if halted else _note(diff, done),
        spend_block=spend_block,
        settled=settled,
        applied_now=done,
        webhooks=webhooks,
        pep=pep,
    )


def _sandboxes_pass(
    *,
    family: FamilyFile,
    applied: AppliedState | None,
    diff: Diff | None,
    state_root: Path,
    image: str,
    actors: Actors,
    publish: PublishNow,
    stop: StopCheck,
) -> tuple[SandboxRecord | None, tuple[FaultEntry, ...], tuple[str, ...]]:
    """The egress axis, then the replace axis. Egress first because it
    lands on whatever is running now, including a sandbox about to die: a
    removal must not wait for a replacement to finish."""
    ran: list[str] = []
    _retire_planned(state_root, family.name, actors.driver)
    records = sandboxes.read_ledger(state_root, family.name)
    serving = _serving(records, applied)

    if _needs(diff, Step.SET_EGRESS) and serving is not None:
        _halt_if(stop, ran)
        live = tuple(
            steps.LiveSandbox(one.id, one.allow)
            for one in records
            if one.state in sandboxes.LIVE_STATES
        )
        faults = steps.apply_egress(family, live, actors.driver, actors.egress)
        ran.append(str(Step.SET_EGRESS))
        if faults:
            return serving, faults, tuple(ran)

        sandboxes.set_allow(state_root, family.name, actors.egress.allowed(family.egress))
        records = sandboxes.read_ledger(state_root, family.name)
        serving = _serving(records, applied)

    if serving is None:
        created, create_faults, ran_now = _first_sandbox(
            family, state_root, image, actors, publish, ran, stop
        )
        if created is None:
            return None, create_faults, ran_now

        serving = created

    if serving.spec_hash == sandboxes.spec_hash_of(family, image):
        return _promote(
            family=family,
            serving=serving,
            state_root=state_root,
            actors=actors,
            publish=publish,
            ran=ran,
            stop=stop,
        )

    return _replace(
        family=family,
        serving=serving,
        records=records,
        diff=diff,
        state_root=state_root,
        image=image,
        actors=actors,
        publish=publish,
        ran=ran,
        stop=stop,
    )


def _retire_planned(state_root: Path, family_name: str, driver: SandboxDriver) -> None:
    """A `planned` row is a create a kill cut short (`sandboxes.fail_planned`
    carries the reasoning). One line, so the reader of a journal knows an
    id went missing on purpose."""
    for sandbox_id in sandboxes.fail_planned(state_root, family_name, driver):
        log.warning(
            "%s: %s was left planned by a kill; retired, id not reused", family_name, sandbox_id
        )


def _first_sandbox(
    family: FamilyFile,
    state_root: Path,
    image: str,
    actors: Actors,
    publish: PublishNow,
    ran: list[str],
    stop: StopCheck,
) -> tuple[SandboxRecord | None, tuple[FaultEntry, ...], tuple[str, ...]]:
    """No sandbox serves this family, so contract 05 §4.3 steps 1 to 5a run
    with nothing to switch away from. `_promote` then runs steps 6 and 7 on
    what this built."""
    _halt_if(stop, ran)
    _publish_step(publish, ran, Step.CREATE_SANDBOX)
    outcome = sandboxes.create_sandbox(
        family,
        state_root=state_root,
        image=image,
        driver=actors.driver,
        egress=actors.egress,
    )
    ran.append(str(Step.CREATE_SANDBOX))
    if outcome.record is None:
        return None, (outcome.fault,) if outcome.fault is not None else (), tuple(ran)

    return outcome.record, (), tuple(ran)


def _publish_step(publish: PublishNow, ran: list[str], step: Step) -> None:
    """Say what the pass is ABOUT to do, before it does it.

    A create costs one to three and a half minutes on the host and
    publishes nothing while it runs. Without this line the document a
    reader sees for all of it is the one the LAST pass wrote, which says
    `in_sync` and goes stale (contract 05 §2 rule 5).
    §3.4's `step` is exactly this: the step now in flight."""
    publish((*ran, str(step)))


def _promote(
    *,
    family: FamilyFile,
    serving: SandboxRecord,
    state_root: Path,
    actors: Actors,
    publish: PublishNow,
    ran: list[str],
    stop: StopCheck,
) -> tuple[SandboxRecord | None, tuple[FaultEntry, ...], tuple[str, ...]]:
    """Contract 05 §4.3 steps 6 and 7, through the one call of §1.

    `sandboxes.promote_sandbox` carries the reasoning and makes the call.
    What belongs here is the moment before it: §4.3 step 5b, for the same
    reason a replacement publishes first. A sandbox the status document does
    not name is one `attendance` will not dial, and §5.3 rule 8 refuses it."""
    if serving.state is SandboxLifecycle.READY:
        return serving, (), tuple(ran)

    _halt_if(stop, ran)
    publish(tuple(ran))
    promoted, step = sandboxes.promote_sandbox(state_root, family.name, serving, actors.switch)

    return promoted, (), (*ran, step) if step else tuple(ran)


def _replace(
    *,
    family: FamilyFile,
    serving: SandboxRecord,
    records: tuple[SandboxRecord, ...],
    diff: Diff | None,
    state_root: Path,
    image: str,
    actors: Actors,
    publish: PublishNow,
    ran: list[str],
    stop: StopCheck,
) -> tuple[SandboxRecord | None, tuple[FaultEntry, ...], tuple[str, ...]]:
    """Contract 05 §4.3 then §5 then §4.4: make before break.

    An ADDITION drains — running turns finish on the outgoing sandbox. A
    REMOVAL interrupts at once, because a turn that still holds the reach
    the edit removed must not be allowed to finish (§5.3 rule 3)."""
    wanted = sandboxes.spec_hash_of(family, image)
    incoming = _with_hash(records, wanted, besides=serving.id)
    if incoming is None:
        _halt_if(stop, ran)
        _publish_step(publish, ran, Step.CREATE_SANDBOX)
        outcome = sandboxes.create_sandbox(
            family,
            state_root=state_root,
            image=image,
            driver=actors.driver,
            egress=actors.egress,
        )
        ran.append(str(Step.CREATE_SANDBOX))
        if outcome.record is None:
            return serving, (outcome.fault,) if outcome.fault is not None else (), tuple(ran)

        incoming = outcome.record
    else:
        # Adopted, not created: a previous pass made it and was interrupted.
        # Its `supervisor.env` is rewritten because a sandbox whose file went
        # missing can never be dialled (contract 05 §4.1.1 rule 4), and this
        # is the only writer that could put it back. `apply_once` republishes
        # for the same reason.
        steps.publish_playpen_env(state_root, family.name, incoming.id)

    mode = (
        diff.switch_mode if diff is not None and diff.switch_mode is not None else SwitchMode.DRAIN
    )
    request = SwitchRequest(
        family=family.name,
        outgoing=serving.id,
        to=incoming.id,
        mode=mode,
        reason=diff.reason() if diff is not None else f"image moved to {image}",
    )
    # Contract 05 §4.3 step 5b. `attendance` knows this family only through
    # the status document (§1), so a sandbox the document does not name is
    # one it will not dial, and §5.3 rule 8 refuses the call.
    _halt_if(stop, ran)
    publish(tuple(ran))

    try:
        actors.switch.switch(request)
    except SwitchError as exc:
        # Both sandboxes stay. `attendance` still sends every turn to the
        # outgoing one, and destroying it would end every session (§5.3
        # rule 1). The next pass retries the same call, which §5.3 rule 7
        # makes idempotent.
        return None, (), (*ran, f"{SWITCH_STEP}:refused({exc})")

    ran.append(SWITCH_STEP)
    # §4.3 step 7. §5.3 rule 8 runs the incoming handshake BEFORE anything
    # moves, so an answer that switched is the same evidence `_promote` reads.
    promoted = sandboxes.mark_ready(state_root, family.name, incoming.id)
    # A stop here keeps the outgoing sandbox. The next pass finds the
    # applied snapshot still naming its spec, repeats the switch §5.3 rule
    # 7 makes idempotent, and destroys it then.
    _halt_if(stop, ran)
    sandboxes.destroy_sandbox(state_root, family.name, serving, actors.driver)
    ran.append(DESTROY_STEP)
    return promoted or incoming, (), tuple(ran)


# --- the two refusals ---------------------------------------------------------


def _keep_last_good(
    state_root: Path,
    family_name: str,
    report: Report,
    revision: str,
    folded: tuple[FaultEntry, ...],
    applied: AppliedState | None,
    pep: PepReport,
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
        sandboxes=tuple(sandboxes.status_of(one) for one in _live_records(state_root, family_name)),
        faults=folded,
        # The last good definition is what serves, so its limits are what
        # `attendance` must still enforce. Publishing the defaults instead
        # would silently move `max_running_turns` on a family that never
        # changed (contract 05 §3.1).
        limits=steps.limits_for(applied.family) if applied is not None else LimitsBlock(),
        pep=pep,
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
    pep: PepReport,
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
        sandboxes=tuple(sandboxes.status_of(one) for one in _live_records(state_root, family.name)),
        faults=folded,
        limits=steps.limits_for(applied.family),
        pep=pep,
    )
    write_status(paths.status_path(state_root, family.name), doc)
    return ReconcileResult(family.name, doc, (), "refused: immutable field moved")


# --- publishing ---------------------------------------------------------------


def _publish(
    state_root: Path,
    family: FamilyFile,
    report: Report,
    revision: str,
    applied: AppliedState | None,
    faults: tuple[FaultEntry, ...],
    live: tuple[SandboxRecord, ...],
    credentials: CredentialsBlock | None,
    *,
    ran: tuple[str, ...],
    note: str,
    spend_block: dict[str, Any] | None,
    settled: str | None = None,
    applied_now: bool = False,
    webhooks: tuple[WebhookToken, ...] = (),
    in_flight: bool = False,
    pep: PepReport,
) -> ReconcileResult:
    """Contract 05 §2: one document, rewritten atomically on every state
    change."""
    applied_rev = revision if applied_now else (applied.rev if applied is not None else "")
    state = _state_of(faults, applied_rev, revision, settled, in_flight=in_flight)
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
        sandboxes=tuple(sandboxes.status_of(one) for one in live),
        credentials=credentials,
        limits=steps.limits_for(family),
        triggers=TriggersBlock(webhooks, enqueue=accepts_dispatch(family)),
        faults=faults,
        reconcile=_reconcile_block(state, applied_rev, revision, ran),
        spend=spend_block,
        pep=pep,
    )
    write_status(paths.status_path(state_root, family.name), doc)
    return ReconcileResult(family.name, doc, ran, note)


def _state_of(
    faults: tuple[FaultEntry, ...],
    applied_rev: str,
    revision: str,
    settled: str | None,
    *,
    in_flight: bool = False,
) -> FamilyState:
    if faults:
        return FamilyState.DEGRADED

    # A pass that is inside a step has not converged, even when the registry
    # did not move: a new sandbox IMAGE replaces the sandbox under one
    # unchanged revision.
    if in_flight or applied_rev != revision:
        return FamilyState.RECONCILING

    # Contract 05 §7 rule 6: a model change is not live until LiteLLM's
    # worker cache has turned over, so `in_sync` may not be claimed yet.
    if settled is not None and now_rfc3339() < settled:
        return FamilyState.RECONCILING

    return FamilyState.IN_SYNC


def _reconcile_block(
    state: FamilyState, applied_rev: str, revision: str, ran: tuple[str, ...]
) -> dict[str, Any] | None:
    """Contract 05 §3.4. Null unless `reconciling` (§2.1)."""
    if state is not FamilyState.RECONCILING:
        return None

    return {
        "since": now_rfc3339(),
        "from_rev": applied_rev,
        "to_rev": revision,
        "step": _step_name(ran[-1]) if ran else str(Step.CREATE_SANDBOX),
        "attempts": 1,
        "needs_switch": any(one.startswith(SWITCH_STEP) for one in ran),
    }


def _step_name(step: str) -> str:
    """Contract 05 §3.4 fixes eight step names and `step` must be one of
    them. `ran` carries a refusal's reason after a colon, for the log line,
    and that detail may not reach the document."""
    name, _, _ = step.partition(":")
    return name


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
                source="managerd",
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


def _pep_faults(pep: PepReport) -> tuple[FaultEntry, ...]:
    """Contract 05 §3.3's `pep_unreachable`, raised on EVERY family.

    One PEP serves the whole fleet, so one outage is every family's
    outage — the same reading §3.3 gives `audit_unreadable`. `managerd`
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
    fault = pep.fault()

    return (fault,) if fault is not None else ()


def _mcp_faults(mcp: McpReport | None) -> tuple[FaultEntry, ...]:
    """Contract 05 §3.3's `mcp_install_held`, raised on EVERY family.

    A declared MCP server is not installed, and root has already
    answered the release that would install it, so `managerd` files no
    new request until the question changes or the floor runs out
    (`mcp_release` rule 3). Without this row the only trace would be a
    log line, and a log line every twenty seconds is the silence
    `pep_unreachable` was added to end.

    It does NOT block turns. A family whose grants name that server has
    one tool fewer, and every other turn it takes is unaffected."""
    fault = mcp.fault() if mcp is not None else None

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
            source="managerd",
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


def _family_text(registry: Registry, family_name: str) -> str:
    return _text(registry.root / "families" / family_name / "family.yaml")


def _text(path: Path) -> str:
    """An unreadable registry file reads empty rather than raising. The
    validation report is where a missing file is reported (invariant 19)."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _live_records(state_root: Path, family_name: str) -> tuple[SandboxRecord, ...]:
    return tuple(
        one
        for one in sandboxes.read_ledger(state_root, family_name)
        if one.state in sandboxes.LIVE_STATES
    )


def _serving(
    records: tuple[SandboxRecord, ...], applied: AppliedState | None
) -> SandboxRecord | None:
    """The sandbox that carries the revision now live.

    After a crash between a switch and a destroy, the applied snapshot
    still names the OLD spec and that sandbox is gone. The newest live one
    is then what serves, and adopting it is what makes the next pass
    converge instead of building a third."""
    if applied is not None:
        match = _with_hash(records, sandboxes.spec_hash_of(applied.family, applied.image))
        if match is not None:
            return match

    return next((one for one in reversed(records) if one.state in sandboxes.LIVE_STATES), None)


def _with_hash(
    records: tuple[SandboxRecord, ...], spec_hash: str, besides: str | None = None
) -> SandboxRecord | None:
    for record in reversed(records):
        if record.state not in sandboxes.LIVE_STATES or record.spec_hash != spec_hash:
            continue

        if record.id != besides:
            return record

    return None


def _model_live_at(applied: AppliedState | None, diff: Diff | None, ran: list[str]) -> str | None:
    """When a `/key/update` may be treated as live everywhere (§7 rule 6).

    An unchanged pass keeps the deadline the last model change set, so the
    10 seconds are counted once and not restarted by every poll."""
    if str(Step.UPDATE_KEY) in ran and _model_moved(diff):
        return seconds_from_now(MODEL_CACHE_S)

    return applied.model_live_at if applied is not None else None


def _model_moved(diff: Diff | None) -> bool:
    if diff is None:
        return False

    return any(one.field == "model.router" for one in diff.changes)


def _note(diff: Diff | None, done: bool) -> str:
    if diff is None:
        return "first apply" if done else "first apply incomplete"

    if not diff.changed:
        return "no change"

    return "replace" if diff.needs_switch else "live"
