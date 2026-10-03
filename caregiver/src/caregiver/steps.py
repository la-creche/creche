"""One function per reconcile step (contract 05 §3.4's step names).

`apply.py` runs all of them once. `reconcile.py` runs the ones a diff
asks for. They live here so both callers run the SAME step, which is what
makes "apply once" and "converge forever" agree about what a family looks
like when it is up.

Every step is idempotent. Running it twice costs time and changes nothing,
which is what lets an interrupted pass simply repeat."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from agent_family import FamilyFile, Index, Kind, duration_s

from . import paths
from .config_mount import RuntimeConfig, config_mount_matches, write_config_mount
from .credentials import Credentials, mint_token, read_creds, token_sha256, write_creds
from .driver import DriverError, SandboxDriver
from .egress import EgressConfig
from .faults import FaultEntry, drop_superseded, read_fault_file, rescope_by_fleet
from .grants import build_grant_file, grant_file_matches, write_grant_file
from .litellm_keys import LiteLLMError, LiteLLMKeys
from .playpen_env import write_playpen_env
from .status import CredentialsBlock, LimitsBlock, RotationState, now_rfc3339

#: Contract 01 §3.12, §3.14: what a thin/autonomous family gets when it
#: leaves the field unset. Not imported from `agent_family`: `grammar.py`'s
#: own defaults are that package's internal pydantic defaults, not
#: published for a caller to reuse.
DEFAULT_JOB_TIMEOUT_S: Final = 120
DEFAULT_MAX_RUNNING_TURNS: Final = 1

#: Contract 05 §3.3.1: the two services that write a fault file.
FAULT_SOURCES: Final = ("sessiond", "pep")


def read_faults(
    state_root: Path, family_name: str, live_sandboxes: tuple[str, ...] = ()
) -> tuple[FaultEntry, ...]:
    """Contract 05 §3.3.1: fold both writers' open faults. A missing file
    (no fault raised) or an unreadable one contributes nothing —
    `read_fault_file` already treats both as "no fault", never as a reason
    to stop.

    `live_sandboxes` is the family's fleet, which decides `blocks_turns` for
    the one code §3.3 states fleet-wide (`faults.rescope_by_fleet`). A caller
    that passes none gets the per-code value, which is the conservative
    reading: a fault that stops turns rather than one that does not.

    A `grants_stale` older than the grant file on disk is dropped here, not
    reported: it names a revision this pass already replaced (§3.3.1 rule 9,
    `faults.drop_superseded`)."""
    collected: list[FaultEntry] = []
    for source in FAULT_SOURCES:
        fault_file = read_fault_file(paths.fault_path(state_root, source, family_name), source)
        if fault_file is not None:
            collected.extend(fault_file.faults)

    current = drop_superseded(tuple(collected), paths.grant_path(state_root, family_name))

    return rescope_by_fleet(current, live_sandboxes)


class KeyRefresh(StrEnum):
    """Whether an existing key's model and budget go to LiteLLM again.

    A one-shot apply pushes them every run, because it runs when a human
    asks. The reconcile loop runs every few seconds, so it pushes only
    when the diff says the model or the budget moved."""

    UPDATE = "update"
    KEEP = "keep"


def ensure_credentials(
    family: FamilyFile,
    state_root: Path,
    litellm: LiteLLMKeys,
    refresh: KeyRefresh = KeyRefresh.UPDATE,
) -> tuple[Credentials | None, CredentialsBlock | None, tuple[FaultEntry, ...]]:
    """Mint once, refresh after (contract 05 §6.2, the epoch of §6). A
    LiteLLM failure is `key_mint_failed`, the one code §3.3 gives this
    exact step."""
    creds_path = paths.creds_path(state_root, family.name)
    existing = read_creds(creds_path)
    if existing is not None and refresh is KeyRefresh.KEEP:
        return existing, credentials_block(family.name, existing), ()

    models = [family.model.router]
    try:
        if existing is None:
            litellm_key = litellm.ensure_key(family.name, models, family.model.budget_usd_per_day)
            pep_token = mint_token()
            epoch = 1
        else:
            litellm.update_key(existing.litellm_key, models, family.model.budget_usd_per_day)
            litellm_key = existing.litellm_key
            pep_token = existing.pep_token
            epoch = existing.epoch
    except LiteLLMError as exc:
        return None, None, (_key_fault(exc),)

    written_at = now_rfc3339()
    creds = Credentials(epoch, litellm_key, pep_token, written_at)
    write_creds(creds_path, creds)
    return creds, credentials_block(family.name, creds), ()


def update_key(
    family: FamilyFile, creds: Credentials, litellm: LiteLLMKeys
) -> tuple[FaultEntry, ...]:
    """Contract 05 §3.4's `update_key` on its own: the family's model and
    budget, on the key that already exists. A removed model takes up to
    10 s to be refused on every worker; a lowered budget refuses the next
    request at once (contract 01 §6.2)."""
    try:
        litellm.update_key(
            creds.litellm_key, [family.model.router], family.model.budget_usd_per_day
        )
    except LiteLLMError as exc:
        return (_key_fault(exc),)

    return ()


def credentials_block(family_name: str, creds: Credentials) -> CredentialsBlock:
    """Contract 05 §6.1: ids and an epoch, never a value. The document is
    mode 0644 precisely so that rule is easy to check."""
    return CredentialsBlock(
        epoch=creds.epoch,
        key_id=f"family-{family_name}",
        token_id=f"family-{family_name}",
        rotated_at=creds.written_at,
        next_rotation_at=None,
        rotation_state=RotationState.SETTLED,
    )


def write_grants(family: FamilyFile, index: Index, state_root: Path, creds: Credentials) -> None:
    """Contract 05 §3.4's `write_grants`. The PEP re-reads the file per
    call, so a rewrite IS the change landing (contract 01 §6).

    Every digest the file lists is accepted (contract 04 §2.2). During a
    graceful token rotation that is two: the new one and the one the
    previous epoch used, until its overlap runs out (contract 05 §6.3
    step 5). `accepted_tokens` decides which, from `creds.json` alone."""
    digests = tuple(token_sha256(one) for one in creds.accepted_tokens(now_rfc3339()))
    grant = build_grant_file(family, index, rev=grant_revision(), token_sha256=digests)
    write_grant_file(paths.grant_path(state_root, family.name), grant)


def grants_current(family: FamilyFile, index: Index, state_root: Path) -> bool:
    """Is the grant file what `write_grants` would write now, `rev` and the
    token digests aside? `all` expands against the server files at read
    time (contract 01 §3.4 rule 4), so a server file moves a family's grants
    while its own file stays put, and no diff of the family file sees it."""
    grant = build_grant_file(family, index, rev="", token_sha256=())
    return grant_file_matches(paths.grant_path(state_root, family.name), grant)


def grant_revision() -> str:
    """Contract 04 §1.2: "opaque, changes on every write." A fresh value
    each call satisfies that literally, with no format the contract does
    not actually require."""
    return uuid.uuid4().hex


def write_config(
    family: FamilyFile, state_root: Path, instructions: str, skills: dict[str, str]
) -> bool:
    """Contract 05 §3.4's `write_config`: `instructions.md`, `skills/` and
    `runtime.json`, one revision for the directory (contract 01 §6.1).

    Answers whether anything changed. An unchanged mount is left alone, so
    a family whose file did not move keeps its directory — and its
    inode — across a reconcile that ran for another family's sake."""
    target = paths.config_dir(state_root, family.name)
    runtime = RuntimeConfig(
        shell=family.shell,
        sandbox_tools=tuple(family.sandbox_tools),
        model_alias=family.model.router,
        system_prompt=family.system_prompt,
    )
    if config_mount_matches(target, instructions=instructions, skills=skills, runtime=runtime):
        return False

    write_config_mount(target, instructions=instructions, skills=skills, runtime=runtime)
    return True


def publish_playpen_env(state_root: Path, family_name: str, sandbox: str) -> None:
    """Rewrite one sandbox's `supervisor.env` (contract 03 §7.1).

    A create writes it once. This puts it back for a sandbox an earlier
    pass created and then left: a sandbox whose file is missing publishes
    no path, and `attendance` refuses every turn on it with
    `sandbox_unavailable` rather than dialling a playpen that would
    answer `fatal` (contract 05 §4.1.1 rule 4). Nothing else can repair it,
    because `caregiver` is the file's only writer."""
    write_playpen_env(
        paths.playpen_env_path(state_root, family_name, sandbox),
        state_root=state_root,
        family=family_name,
        sandbox=sandbox,
    )


@dataclass(frozen=True)
class LiveSandbox:
    """One sandbox an egress edit lands on, and the rows it carries now.

    Two sandboxes of one family can carry different rows: a replacement
    created in an earlier pass already holds the new list. Diffing each
    against its own rows is what keeps `sbx policy rm` from being asked
    for a row that is not there."""

    id: str
    allow: tuple[str, ...]


def apply_egress(
    family: FamilyFile,
    live: tuple[LiveSandbox, ...],
    driver: SandboxDriver,
    egress: EgressConfig,
) -> tuple[FaultEntry, ...]:
    """Contract 05 §3.4's `set_egress`, on every live sandbox.

    **Removals first, across every sandbox.** An interruption between the
    two halves then leaves LESS reach than the target, never more
    (invariant 9). Both plane endpoints survive every edit: they are
    `caregiver`'s own config and no family file may name them (contract 01
    §3.7 rules 4 and 6)."""
    wanted = egress.allowed(family.egress)
    try:
        for one in live:
            driver.remove_egress(one.id, tuple(row for row in one.allow if row not in wanted))

        for one in live:
            driver.set_egress(one.id, tuple(row for row in wanted if row not in one.allow))
    except DriverError as exc:
        fault = FaultEntry(
            code="egress_assert_failed",
            blocks_turns=True,
            since=now_rfc3339(),
            source="managerd",
            detail={"message": str(exc)},
        )
        return (fault,)

    return ()


def limits_for(family: FamilyFile) -> LimitsBlock:
    """Contract 05 §2.1: two come from `family.yaml`; `max_queued_turns`
    is `attendance`'s own constant, which `LimitsBlock`'s default mirrors."""
    if family.kind == Kind.AUTONOMOUS:
        turns = (
            family.max_running_turns
            if family.max_running_turns is not None
            else DEFAULT_MAX_RUNNING_TURNS
        )
        return LimitsBlock(max_running_turns=turns)

    if family.kind == Kind.THIN:
        timeout = family.job.timeout if family.job is not None else f"{DEFAULT_JOB_TIMEOUT_S}s"
        return LimitsBlock(job_timeout_s=duration_s(timeout))

    return LimitsBlock()


def _key_fault(exc: LiteLLMError) -> FaultEntry:
    return FaultEntry(
        code="key_mint_failed",
        blocks_turns=True,
        since=now_rfc3339(),
        source="managerd",
        detail={"message": str(exc)},
    )
