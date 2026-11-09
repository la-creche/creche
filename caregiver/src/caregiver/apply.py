"""apply-once: bring one family up from its registry file: the family key,
the family token, the table of `policy.toml`, the config directory and the
reach files.

It does not keep a family converged or rotate its credentials: `loop.py`
and `rotate.py` do. It starts no VM: the vmm starts one VM per session at
its first prompt (decision CH1). Re-running `apply_once` is safe: it mints
credentials only once and reuses them after."""

from __future__ import annotations

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

from . import paths, reach_files, steps
from .atomic import read_json
from .credentials import read_creds
from .faults import FaultEntry
from .litellm_keys import LiteLLMKeys
from .policy_file import PolicyShapeError
from .policy_fleet import FleetPolicy, LeftOut
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
    # The fences of the host (`agent_family.fences`). Required: an apply
    # with no fences would apply a family that the fences refuse.
    fences: Fences,
    litellm: LiteLLMKeys,
    policy: FleetPolicy,
    host: HostFacts | None = None,
) -> ApplyResult:
    """Validate, then -- only if the file is valid -- mint or refresh
    credentials, write the table of `policy.toml`, the config directory
    and the reach files, and publish the status document.

    A table that `chaperone check` refuses, for its shape or in the
    assembly, stops the apply as an invalid file does. Its one error line
    is the first error of the report.

    On an invalid file: write the validation report, write a status
    document reporting `invalid`, and stop (invariant 19). Nothing this
    family already had (its table of `policy.toml`, its config directory,
    its reach files, its credentials) is touched, so the last good definition
    keeps serving."""
    registry = load_registry(registry_root, host, fences)
    report = registry.reports.get(family_name)
    if report is None or (report.ok and family_name not in registry.families):
        raise FamilyNotFoundError(family_name)

    write_validation_report(paths.validation_path(state_root, family_name), report)
    faults = steps.read_faults(state_root, family_name)
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
    )
    return _apply_valid(
        family=family,
        report=report,
        registry_revision=registry.revision,
        instructions=registry.instructions(family_name).read_text(encoding="utf-8"),
        skills={
            name: registry.skill_file(name).read_text(encoding="utf-8") for name in family.skills
        },
        index=index,
        state_root=state_root,
        litellm=litellm,
        policy=policy,
        faults=faults,
        never_applied_before=never_applied_before,
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
    skills: dict[str, str],
    index: Index,
    state_root: Path,
    litellm: LiteLLMKeys,
    policy: FleetPolicy,
    faults: tuple[FaultEntry, ...],
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

    # Decision CH71: an `egress.json` over the cap of the vmm stops the
    # apply before any write, as a refused table does.
    try:
        egress_file = reach_files.render_egress_file(family)
    except reach_files.EgressTooLargeError as refusal:
        return refuse(str(refusal))

    creds, credentials_block, key_faults = steps.ensure_credentials(family, state_root, litellm)
    all_faults = (*faults, *key_faults)
    if creds is None:
        # invariant 13 orders a DELETE (credentials before the VMs). A
        # failed MINT is the opposite case: nothing downstream depends on
        # a key that does not exist, so there is nothing more to do.
        return _degraded(
            family, report, registry_revision, state_root, all_faults, credentials_block
        )

    standing = steps.land_policy(family, index, state_root, creds, policy, rev=registry_revision)
    if isinstance(standing, LeftOut):
        return refuse(standing.reason)

    steps.write_config(family, state_root, instructions, skills)
    # Contract 01 §6.1.1, decision CH69 rule 6: the egress allowlist and the
    # clone list of the vmm, as a pass writes them.
    reach_files.write_egress_file(egress_file, state_root)
    reach_files.write_repos_file(family, state_root)
    # Contract 05 §6.4, invariant 16: a declared webhook opens a live route
    # with no second, hand-made act. Beside the other credentials
    # (invariant 13).
    webhooks = ensure_webhooks(family, state_root)

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
    doc = StatusDocument(
        family=family.name,
        kind=str(family.kind),
        state=state,
        written_at=now_rfc3339(),
        registry_rev=registry_revision,
        applied_rev=registry_revision,
        config_rev=registry_revision,
        validation=validation,
        credentials=credentials_block,
        limits=steps.limits_for(family),
        triggers=TriggersBlock(webhooks, enqueue=accepts_dispatch(family)),
        faults=all_faults,
    )
    write_status(paths.status_path(state_root, family.name), doc)
    return ApplyResult(ok=not all_faults, status=doc)


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
