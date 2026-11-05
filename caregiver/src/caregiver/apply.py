"""apply-once: bring one family up from its registry file: create the
sandbox, the family key and the family token from the file.

It does not keep a family converged, switch a sandbox or rotate its
credentials: `loop.py`, `switch.py` and `rotate.py` do. Re-running
`apply_once` is safe: it mints
credentials only once and reuses them after, and it creates a sandbox only
when the family has none yet -- deciding whether a *changed* file needs a
new one is the reconciler's classify()-based job, not this one's."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_family import (
    FamilyFile,
    FamilyState,
    Fences,
    HostFacts,
    Index,
    Issue,
    Report,
    Severity,
    load_registry,
)

from . import paths, sandboxes, steps
from .atomic import read_json
from .credentials import read_creds
from .driver import SandboxDriver
from .egress import EgressConfig
from .faults import FaultEntry
from .images import SandboxImages
from .lan import Port, url
from .litellm_keys import LiteLLMKeys
from .playpen_env import write_playpen_env
from .policy_file import PolicyShapeError
from .policy_fleet import FleetPolicy, LeftOut
from .sandboxes import SandboxRecord
from .status import (
    CredentialsBlock,
    LimitsBlock,
    StatusDocument,
    TriggersBlock,
    ValidationBlock,
    accepts_dispatch,
    now_rfc3339,
    write_status,
    write_validation_report,
)
from .switch import SwitchClient
from .webhook_tokens import ensure_webhooks


class FamilyNotFoundError(RuntimeError):
    """The registry holds no `family.toml` for the name: no
    `families/<name>/` directory, or a directory with no file (contract 01
    section 5.6 rule 3). That is not the same as an invalid `family.toml`,
    which gets a report."""


@dataclass(frozen=True)
class ApplyResult:
    ok: bool
    status: StatusDocument


def apply_once(
    registry_root: Path,
    family_name: str,
    *,
    state_root: Path,
    # Contract 05 §4.1: the approved sandbox image digest is contract
    # 06's answer, so it is taken as a parameter rather than resolved here.
    image: str,
    # The second flavor of contract 01 §3.9, same channel as the first.
    # Empty means this host was given none, and a family that asks for it
    # gets `image_flavor_unconfigured` rather than a base sandbox.
    python_image: str = "",
    # The fences of the host (`agent_family.fences`). Required: an apply
    # with no fences would apply a family that the fences refuse.
    fences: Fences,
    driver: SandboxDriver,
    litellm: LiteLLMKeys,
    policy: FleetPolicy,
    host: HostFacts | None = None,
    egress: EgressConfig | None = None,
    # Contract 05 §4.3 steps 6 and 7. Optional, because `up` runs this verb
    # before `attendance` is listening: with no client the sandbox stays
    # `creating`, which is what §4.2 rule 4 requires when no handshake ran.
    switch: SwitchClient | None = None,
) -> ApplyResult:
    """Validate, then -- only if the file is valid -- mint or refresh
    credentials, write the table of `policy.toml` and the config mount,
    create the sandbox if none exists yet, and publish the status document.

    A table that `chaperone check` refuses, for its shape or in the
    assembly, stops the apply as an invalid file does. Its one error line
    is the first error of the report.

    On an invalid file: write the validation report, write a status
    document reporting `invalid`, and stop (invariant 19). Nothing this
    family already had (its table of `policy.toml`, its config mount, its
    credentials, its sandbox) is touched, so the last good definition
    keeps serving."""
    registry = load_registry(registry_root, host, fences)
    report = registry.reports.get(family_name)
    if report is None or (report.ok and family_name not in registry.families):
        raise FamilyNotFoundError(family_name)

    write_validation_report(paths.validation_path(state_root, family_name), report)
    faults = steps.read_faults(state_root, family_name, _live_ids(state_root, family_name))
    never_applied_before = read_creds(paths.creds_path(state_root, family_name)) is None

    if not report.ok:
        return _stopped_on_invalid(
            state_root, family_name, report, registry.revision, faults, never_applied_before
        )

    family = registry.families[family_name]
    index = Index(
        kinds={name: one.kind for name, one in registry.families.items()},
        servers=registry.servers,
        skills=registry.skills,
        skill_texts=registry.skill_texts,
    )
    return _apply_valid(
        family=family,
        report=report,
        registry_revision=registry.revision,
        instructions=registry.instructions(family_name).read_text(encoding="utf-8"),
        skills=steps.skill_files(registry, family),
        index=index,
        state_root=state_root,
        images=SandboxImages(base=image, python=python_image),
        driver=driver,
        litellm=litellm,
        policy=policy,
        faults=faults,
        egress=egress if egress is not None else EgressConfig(),
        switch=switch,
        never_applied_before=never_applied_before,
    )


def _live_ids(state_root: Path, family_name: str) -> tuple[str, ...]:
    """The family's live fleet, which decides `blocks_turns` for the one code
    contract 05 §3.3 states fleet-wide (`faults.rescope_by_fleet`)."""
    return tuple(
        one.id
        for one in sandboxes.read_ledger(state_root, family_name)
        if one.state in sandboxes.LIVE_STATES
    )


def _stopped_on_invalid(
    state_root: Path,
    family_name: str,
    report: Report,
    registry_revision: str,
    faults: tuple[FaultEntry, ...],
    never_applied_before: bool,
) -> ApplyResult:
    previous = _read_previous_status_json(paths.status_path(state_root, family_name))
    validation = ValidationBlock(
        rev=registry_revision,
        checked_at=now_rfc3339(),
        ok=False,
        never_valid=never_applied_before,
        error_count=report.errors,
        warning_count=report.warnings,
        report_path=str(paths.validation_path(state_root, family_name)),
        first_error=report.first_error,
    )
    doc = StatusDocument(
        family=family_name,
        kind=_previous_str(previous, "kind", ""),
        state=FamilyState.INVALID,
        written_at=now_rfc3339(),
        registry_rev=registry_revision,
        applied_rev=_previous_str(previous, "applied_rev", ""),
        config_rev=_previous_str(previous, "config_rev", ""),
        validation=validation,
        faults=faults,
        credentials=steps.kept_credentials(family_name, state_root),
        limits=LimitsBlock(),
    )
    write_status(paths.status_path(state_root, family_name), doc)
    return ApplyResult(ok=False, status=doc)


def _previous_str(previous: dict[str, Any] | None, key: str, default: str) -> str:
    if previous is None:
        return default

    value = previous.get(key)
    return value if isinstance(value, str) else default


def _read_previous_status_json(path: Path) -> dict[str, Any] | None:
    return read_json(path)


def _apply_valid(
    *,
    family: FamilyFile,
    report: Report,
    registry_revision: str,
    instructions: str,
    skills: Mapping[str, Mapping[str, str]],
    index: Index,
    state_root: Path,
    images: SandboxImages,
    driver: SandboxDriver,
    litellm: LiteLLMKeys,
    policy: FleetPolicy,
    faults: tuple[FaultEntry, ...],
    egress: EgressConfig,
    switch: SwitchClient | None,
    never_applied_before: bool,
) -> ApplyResult:
    def refuse(reason: str) -> ApplyResult:
        issue = Issue(Severity.ERROR, steps.WHOLE_FILE, reason)
        refused = Report(report.family, report.file, (*report.issues, issue), FamilyState.INVALID)
        write_validation_report(paths.validation_path(state_root, family.name), refused)
        return _stopped_on_invalid(
            state_root, family.name, refused, registry_revision, faults, never_applied_before
        )

    try:
        steps.check_policy(family, index)
    except PolicyShapeError as refusal:
        return refuse(str(refusal))

    creds, credentials_block, key_faults = steps.ensure_credentials(family, state_root, litellm)
    all_faults = (*faults, *key_faults)
    if creds is None:
        # invariant 13 orders a DELETE (credentials before the sandbox). A
        # failed MINT is the opposite case: nothing downstream depends on
        # a key that does not exist, so there is nothing more to do.
        return _degraded(
            family, report, registry_revision, state_root, all_faults, credentials_block
        )

    standing = steps.land_policy(family, index, state_root, creds, policy, rev=registry_revision)
    if isinstance(standing, LeftOut):
        return refuse(standing.reason)

    steps.write_config(family, state_root, instructions, skills)
    # Contract 05 §6.4, invariant 16: a declared webhook opens a live route
    # with no second, hand-made act. Beside the other credentials, before
    # any sandbox exists (invariant 13).
    webhooks = ensure_webhooks(family, state_root)

    record, sandbox_faults = _ensure_sandbox(family, state_root, images, driver, egress)
    # Both fault files are read AGAIN, after the sandbox step, so the
    # document holds the faults of the fleet as it stands now.
    all_faults = (
        *steps.read_faults(state_root, family.name, _live_ids(state_root, family.name)),
        *key_faults,
        *sandbox_faults,
    )

    state = FamilyState.DEGRADED if all_faults else FamilyState.IN_SYNC
    validation = ValidationBlock(
        rev=registry_revision,
        checked_at=now_rfc3339(),
        ok=True,
        never_valid=False,
        error_count=0,
        warning_count=report.warnings,
        report_path=str(paths.validation_path(state_root, family.name)),
        first_error=None,
    )

    def publish(one: SandboxRecord | None) -> ApplyResult:
        doc = StatusDocument(
            family=family.name,
            kind=str(family.kind),
            state=state,
            written_at=now_rfc3339(),
            registry_rev=registry_revision,
            applied_rev=registry_revision,
            config_rev=registry_revision,
            validation=validation,
            sandboxes=(sandboxes.status_of(one),) if one is not None else (),
            credentials=credentials_block,
            limits=steps.limits_for(family),
            triggers=TriggersBlock(webhooks, enqueue=accepts_dispatch(family)),
            faults=all_faults,
        )
        write_status(paths.status_path(state_root, family.name), doc)

        return ApplyResult(ok=not all_faults, status=doc)

    if record is None or switch is None:
        return publish(record)

    # Contract 05 §4.3 step 5b then steps 6 and 7. The document is published
    # FIRST because it is the only thing that crosses to `attendance` (§1), so
    # a sandbox it does not name is one §5.3 rule 8 refuses.
    publish(record)
    promoted, _step = sandboxes.promote_sandbox(state_root, family.name, record, switch)

    return publish(promoted)


def _ensure_sandbox(
    family: FamilyFile,
    state_root: Path,
    images: SandboxImages,
    driver: SandboxDriver,
    egress: EgressConfig,
) -> tuple[SandboxRecord | None, tuple[FaultEntry, ...]]:
    """A family that already has a live sandbox keeps it. One that does
    not gets one, through `sandboxes.create_sandbox` — the one creation
    path, so no caller can skip §4.3's egress proof.

    Whether a *changed* file needs a new sandbox is `classify()`'s answer
    and the reconciler's decision, never this verb's.

    A `planned` row goes first, for the same reason the reconciler retires
    one: it is a create a kill cut short, and the VM it may have built
    never had its canaries probed. "The VM exists" is not the test — `up`
    runs this verb right after a restart, which is exactly when a leftover
    is there to find."""
    sandboxes.fail_planned(state_root, family.name, driver)
    live = sandboxes.live_record(state_root, family.name)
    if live is not None and live.id in driver.list_names():
        # Republished, not only written at create: a family whose
        # `supervisor.env` went missing would fail every turn, and an apply
        # is the one thing that can put it back.
        _write_playpen_env(state_root, family, live.id)
        return live, ()

    # Contract 01 §3.9: a flavor this host has no image for creates
    # nothing. A fall-back to `base` would run the family on a filesystem
    # its file does not ask for, and say nothing.
    image = images.resolve(family.sandbox.image)
    if image is None:
        return None, (sandboxes.unconfigured_image_fault(family, images),)

    outcome = sandboxes.create_sandbox(
        family,
        state_root=state_root,
        image=image,
        driver=driver,
        egress=egress,
    )
    if outcome.record is None:
        return None, (outcome.fault,) if outcome.fault is not None else ()

    return outcome.record, ()


def _write_playpen_env(state_root: Path, family: FamilyFile, sandbox: str) -> None:
    """Contract 03 section 7.1: `sbx exec` forwards no host environment, so
    `--env-file` is the only way the three mount paths reach the
    playpen. sbx mounts each directory at its own HOST path, so the file
    names those paths and rewrites nothing."""
    write_playpen_env(
        paths.playpen_env_path(state_root, family.name, sandbox),
        state_root=state_root,
        family=family.name,
        sandbox=sandbox,
        pep_url=url(Port.PEP),
        litellm_base_url=url(Port.LITELLM),
        index_root=paths.index_root(family),
    )


def _degraded(
    family: FamilyFile,
    report: Report,
    registry_revision: str,
    state_root: Path,
    faults: tuple[FaultEntry, ...],
    credentials: CredentialsBlock | None,
) -> ApplyResult:
    validation = ValidationBlock(
        rev=registry_revision,
        checked_at=now_rfc3339(),
        ok=True,
        never_valid=False,
        error_count=0,
        warning_count=report.warnings,
        report_path=str(paths.validation_path(state_root, family.name)),
        first_error=None,
    )
    previous = _read_previous_status_json(paths.status_path(state_root, family.name))
    doc = StatusDocument(
        family=family.name,
        kind=str(family.kind),
        state=FamilyState.DEGRADED,
        written_at=now_rfc3339(),
        registry_rev=registry_revision,
        applied_rev=_previous_str(previous, "applied_rev", ""),
        config_rev=_previous_str(previous, "config_rev", ""),
        validation=validation,
        credentials=credentials,
        limits=steps.limits_for(family),
        faults=faults,
    )
    write_status(paths.status_path(state_root, family.name), doc)
    return ApplyResult(ok=False, status=doc)
