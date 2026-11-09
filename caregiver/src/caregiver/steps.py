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
from enum import StrEnum
from pathlib import Path
from typing import Final

from agent_family import FamilyFile, Index, Kind, duration_s, memory_mb
from creche_wire import FamilyShape

from . import paths
from .clock import ms_from_now
from .config_mount import (
    LiveModel,
    RuntimeConfig,
    config_mount_matches,
    read_model,
    write_config_mount,
)
from .credentials import Credentials, mint_token, read_creds, token_sha256, write_creds
from .faults import FAULT_SOURCES, FaultEntry, read_fault_file
from .litellm_keys import LiteLLMError, LiteLLMKeys
from .policy_file import build_app
from .policy_fleet import FleetPolicy, Standing, land_app, land_digests
from .status import CredentialsBlock, LimitsBlock, RotationState, now_rfc3339

#: Contract 01 §3.12, §3.14: what a thin/autonomous family gets when it
#: leaves the field unset. Not imported from `agent_family`: `grammar.py`'s
#: own defaults are that package's internal pydantic defaults, not
#: published for a caller to reuse.
DEFAULT_JOB_TIMEOUT_S: Final = 120
DEFAULT_MAX_RUNNING_TURNS: Final = 1

#: Contract 05 §7 rule 6: a `/key/update` can be served stale by another
#: LiteLLM worker for this long, so a model change is not live — and the
#: family is not `in_sync` — until it has passed. Probe 0b measured 7 s
#: across four workers on this host.
MODEL_CACHE_S: Final = 10

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

#: One digest, for a check of the shape of an app table before the key
#: step gives the family its token.
_ONE_DIGEST: Final = token_sha256("")


def read_faults(state_root: Path, family_name: str) -> tuple[FaultEntry, ...]:
    """Contract 05 §3.3.1: fold the open faults of the three writers,
    `attendance`, the PEP and the vmm (decision CH21-1). A missing file
    (no fault raised) or an unreadable one contributes nothing —
    `read_fault_file` already treats both as "no fault", never as a reason
    to stop. Each fault keeps the `blocks_turns` of its code."""
    collected: list[FaultEntry] = []
    for source in FAULT_SOURCES:
        fault_file = read_fault_file(paths.fault_path(state_root, source, family_name), source)
        if fault_file is not None:
            collected.extend(fault_file.faults)

    return tuple(collected)


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


def write_config(
    family: FamilyFile, state_root: Path, instructions: str, skills: dict[str, str]
) -> bool:
    """Contract 05 §3.4's `write_config`: `instructions.md`, `skills/`,
    `runtime.json` and `shape.json`, one revision for the directory
    (contract 01 §6.1, decisions CH23-1, CH36 G6, CH45 rule 2).

    A new alias gets a new `model_live_at`, and an alias that stays keeps
    the old one (decision CH23-1). The caller updates the key before this
    step, so the 10 seconds start after the `/key/update`.

    Answers whether anything changed. An unchanged mount is left alone, so
    a family whose file did not move keeps its directory — and its
    inode — across a reconcile that ran for another family's sake."""
    target = paths.config_dir(state_root, family.name)
    alias = family.model.router
    runtime = RuntimeConfig(
        shell=family.shell,
        sandbox_tools=tuple(family.sandbox_tools),
        model_alias=alias,
        system_prompt=family.system_prompt,
        model_live_at=_live_at(read_model(target), alias),
    )
    shape = _shape_of(family)
    if config_mount_matches(
        target, instructions=instructions, skills=skills, runtime=runtime, shape=shape
    ):
        return False

    write_config_mount(
        target, instructions=instructions, skills=skills, runtime=runtime, shape=shape
    )
    return True


def model_live_at(family_name: str, state_root: Path) -> int | None:
    """`model_live_at` of the `runtime.json` of the family, its one home
    (decision CH23-1). The sync check of a pass reads it here. `None` when
    no model change set it."""
    model = read_model(paths.config_dir(state_root, family_name))
    return model.live_at if model is not None else None


def _live_at(written: LiveModel | None, alias: str) -> int | None:
    """The `model_live_at` of a new `runtime.json` with `alias`. A first
    write sets none: the key of a new family is minted with its model, so
    no LiteLLM worker holds an old one. A `runtime.json` that does not read
    holds no alias, so its rewrite also sets none."""
    if written is None:
        return None

    if written.alias == alias:
        return written.live_at

    return ms_from_now(MODEL_CACHE_S)


def _shape_of(family: FamilyFile) -> FamilyShape:
    """The family VM shape of a valid family file (decision CH36 G6):
    `sandbox.cpus`, `sandbox.image` as the flavor, and `sandbox.memory` in
    MiB, with `1g` as 1024. The validator holds each in the range of the
    wire type, so the model check here refuses nothing that reaches it."""
    box = family.sandbox
    return FamilyShape.model_validate(
        {"cpus": box.cpus, "flavor": box.image, "memory_mib": memory_mb(box.memory)}
    )


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
        return LimitsBlock(job_timeout_s=duration_s(timeout))

    return LimitsBlock()


def _key_fault(exc: LiteLLMError) -> FaultEntry:
    return FaultEntry(
        code="key_mint_failed",
        blocks_turns=True,
        since=now_rfc3339(),
        source="caregiver",
        detail={"message": str(exc)},
    )
