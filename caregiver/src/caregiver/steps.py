"""One function per reconcile step (contract 05 §3.4's step names).

`apply.py` runs all of them once. `reconcile.py` runs the ones a diff
asks for. They live here so both callers run the SAME step, which is what
makes "apply once" and "converge forever" agree about what a family looks
like when it is up.

Every step is idempotent. Running it twice costs time and changes nothing,
which is what lets an interrupted pass simply repeat."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from agent_family import FamilyFile, Index, Kind, Registry, duration_s

from . import paths
from .config_mount import SKILL_FILE, RuntimeConfig, config_mount_matches, write_config_mount
from .credentials import Credentials, mint_token, read_creds, token_sha256, write_creds
from .driver import DriverError, SandboxDriver
from .egress import EgressConfig
from .faults import FaultEntry, read_fault_file, rescope_by_fleet
from .lan import Port, url
from .litellm_keys import LiteLLMError, LiteLLMKeys
from .playpen_env import write_playpen_env
from .policy_file import build_app
from .policy_fleet import FleetPolicy, Standing, land_app, land_digests
from .status import CredentialsBlock, LimitsBlock, RotationState, now_rfc3339

#: Contract 01 §3.12, §3.14: what a thin/autonomous family gets when it
#: leaves the field unset. Not imported from `agent_family`: `grammar.py`'s
#: own defaults are that package's internal pydantic defaults, not
#: published for a caller to reuse.
DEFAULT_JOB_TIMEOUT_S: Final = 120
DEFAULT_MAX_RUNNING_TURNS: Final = 1

log = logging.getLogger("caregiver.steps")

#: The place of an issue that no one field of the family file owns. The
#: validator of `agent_family` writes the same text for an issue of the
#: whole file.
#:
#: CONTRACT-QUESTION: contract 01 §7 gives `loc` as a field path, and it
#: has no rule for an issue with no field. The reading taken is the text of
#: the validator. A reader that takes a field path alone finds no field of
#: this name. Another text costs this constant and one test.
WHOLE_FILE: Final = "<document>"

#: Contract 05 §3.3.1: the two services that write a fault file.
FAULT_SOURCES: Final = ("attendance", "pep")

#: One digest, for a check of the shape of an app table before the key
#: step gives the family its token.
_ONE_DIGEST: Final = token_sha256("")


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
    reading: a fault that stops turns rather than one that does not."""
    collected: list[FaultEntry] = []
    for source in FAULT_SOURCES:
        fault_file = read_fault_file(paths.fault_path(state_root, source, family_name), source)
        if fault_file is not None:
            collected.extend(fault_file.faults)

    return rescope_by_fleet(tuple(collected), live_sandboxes)


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
    # CONTRACT-QUESTION: contract 03 §12 rule 3 says that the epoch
    # increases on every write. It has no rule for a `creds.json` that is
    # present and does not read. This step takes it as an absent file, as
    # it did for text that is not JSON. It mints a key and a token, writes
    # epoch 1 and says so in the log, so the published epoch can go down.
    # To keep the rule, the step must refuse the write, and the family then
    # has no credentials until the operator repairs the file.
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
    if existing is None and os.path.exists(creds_path):
        log.error(
            "%s: creds.json does not read. The pass replaces it with a new key, "
            "a new token and epoch 1",
            family.name,
        )

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


def kept_credentials(family_name: str, state_root: Path) -> CredentialsBlock | None:
    """Contract 05 §6.1 for an `invalid` document, whose pass mints nothing
    and refreshes nothing. A rotation raises the epoch in `creds.json`
    whatever the family file says, and the status document is the only
    place `attendance` reads it. `None` for a family that never had
    credentials."""
    creds = read_creds(paths.creds_path(state_root, family_name))
    if creds is None:
        return None

    return credentials_block(family_name, creds)


def check_policy(family: FamilyFile, index: Index) -> None:
    """The shape check of `land_policy`, with no write and no key.

    It raises `PolicyShapeError` for an app table that `chaperone check`
    would refuse for its shape alone, for example an exposed tool name over
    50 characters. A pass calls it before the key step."""
    build_app(family, index, (_ONE_DIGEST,), rev="")


def land_policy(
    family: FamilyFile,
    index: Index,
    state_root: Path,
    creds: Credentials,
    policy: FleetPolicy,
    *,
    rev: str,
) -> Standing:
    """The table of the family in the one `policy.toml` of the Rust
    chaperone: its grants from the family file, and each digest that
    `creds.json` accepts now. `rev` is the registry revision, kept in the
    note."""
    app = build_app(family, index, _accepted_digests(creds), rev=rev)
    return land_app(state_root, policy, family.name, app)


def policy_digests(
    family_name: str, state_root: Path, creds: Credentials, policy: FleetPolicy
) -> Standing | None:
    """The digests `land_policy` would list, in the fragment as it stands.
    No family file goes in, so no grant moves. `None` when the family has
    no fragment."""
    return land_digests(state_root, policy, family_name, _accepted_digests(creds))


def has_policy(family_name: str, state_root: Path) -> bool:
    """Would `policy_digests` find a fragment to keep?"""
    return paths.policy_fragment_path(state_root, family_name).is_file()


def _accepted_digests(creds: Credentials) -> tuple[str, ...]:
    return tuple(token_sha256(one) for one in creds.accepted_tokens(now_rfc3339()))


def skill_files(registry: Registry, family: FamilyFile) -> dict[str, Mapping[str, str]]:
    """The files of every skill the family grants, by skill name.

    A skill's own directory decides the keys: `SKILL.md` plus each helper
    file its frontmatter declares. A skill the registry does not hold
    stages an empty `SKILL.md`, which is what a granted skill has always
    staged."""
    staged: dict[str, Mapping[str, str]] = {}
    for name in family.skills:
        content = registry.skill_texts.get(name)
        staged[name] = content.files if content is not None else {SKILL_FILE: ""}

    return staged


def write_config(
    family: FamilyFile,
    state_root: Path,
    instructions: str,
    skills: Mapping[str, Mapping[str, str]],
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


def publish_playpen_env(state_root: Path, family: FamilyFile, sandbox: str) -> None:
    """Rewrite one sandbox's `supervisor.env` (contract 03 §7.1).

    A create writes it once. This puts it back for a sandbox an earlier
    pass created and then left: a sandbox whose file is missing publishes
    no path, and `attendance` refuses every turn on it with
    `sandbox_unavailable` rather than dialling a playpen that would
    answer `fatal` (contract 05 §4.1.1 rule 4). Nothing else can repair it,
    because `caregiver` is the file's only writer."""
    write_playpen_env(
        paths.playpen_env_path(state_root, family.name, sandbox),
        state_root=state_root,
        family=family.name,
        sandbox=sandbox,
        pep_url=url(Port.PEP),
        litellm_base_url=url(Port.LITELLM),
        index_root=paths.index_root(family),
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
            source="caregiver",
            detail={"message": str(exc)},
        )
        return (fault,)

    return ()


def limits_for(family: FamilyFile) -> LimitsBlock:
    """Contract 05 §2.1: two come from `family.toml`; `max_queued_turns`
    is `attendance`'s own constant, which `LimitsBlock`'s default mirrors."""
    if family.kind == Kind.AUTONOMOUS:
        turns = (
            family.max_running_turns
            if family.max_running_turns is not None
            else DEFAULT_MAX_RUNNING_TURNS
        )
        return LimitsBlock(max_running_turns=turns)

    if family.kind == Kind.THIN:
        job = family.job
        timeout = job.timeout if job is not None else f"{DEFAULT_JOB_TIMEOUT_S}s"
        root = job.caller_root if job is not None else None
        return LimitsBlock(job_timeout_s=duration_s(timeout), caller_root=root)

    return LimitsBlock()


def _key_fault(exc: LiteLLMError) -> FaultEntry:
    return FaultEntry(
        code="key_mint_failed",
        blocks_turns=True,
        since=now_rfc3339(),
        source="caregiver",
        detail={"message": str(exc)},
    )
