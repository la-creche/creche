"""Delete a family entirely.

Credentials die before processes (invariant 13): the LiteLLM key and the
table of `policy.toml` are gone before the vmm stops any VM of the family.
This module does not decide *when* deleting a family is the right action --
the caller (the CLI or the loop) does -- it only performs the deletion in
the order of contract 05 §4.4, and removes caregiver's own state for that
family once nothing live depends on it."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from . import paths
from .litellm_keys import LiteLLMKeys
from .policy_fleet import FleetPolicy, withdraw_app
from .vmm import VmmClient
from .webhook_tokens import forget_webhooks

log = logging.getLogger("caregiver.delete")


def delete_family(
    family_name: str,
    *,
    state_root: Path,
    vmm: VmmClient,
    litellm: LiteLLMKeys,
    policy: FleetPolicy,
) -> str | None:
    """Contract 05 §4.4, in this order:

    1. The LiteLLM key.
    2. The fragment of the family, then a new assembly of `policy.toml`
       with no table for it. The table carries the digest of the token, so
       its removal revokes the token.
    3. The family files: `creds.json`, `egress.json`, `repos.json`,
       `config/` and the webhook bearers. A create of a session for a
       family with no `config/` is refused (decision CH33 rule 3).
    4. The drop of every session of the family on the vmm (decisions
       CH21-10, CH33 rule 3).
    5. This family's own state directory.

    Answers why `policy.toml` still holds a table of the family, or `None`.
    A refused assembly does not stop the delete: the VMs that hold the
    token go all the same.

    A drop that fails raises `VmmError`, and step 5 does not run. The state
    directory then stays, so the next look of the loop finds the family and
    runs the delete again. Steps 1 to 3 are idempotent."""
    litellm.delete_key(family_name)
    kept = withdraw_app(state_root, policy, family_name)
    if kept is not None:
        log.error("%s: policy.toml still holds the table of the family: %s", family_name, kept)

    paths.creds_path(state_root, family_name).unlink(missing_ok=True)
    # Reach narrows before processes stop, as credentials do (contract 01
    # §6.1.1 rule 7, decision CH69 rule 23).
    paths.egress_path(state_root, family_name).unlink(missing_ok=True)
    paths.repos_path(state_root, family_name).unlink(missing_ok=True)
    config = paths.config_dir(state_root, family_name)
    if config.exists():
        shutil.rmtree(config)
    # A webhook bearer lives outside `family_dir` (contract 05 §6.4), so
    # step 5 does not reach it. It is a credential, so it goes here, with
    # the others, before the vmm stops a VM.
    forget_webhooks(state_root, family_name)

    vmm.drop_family(family_name)

    family_dir = paths.family_dir(state_root, family_name)
    if family_dir.exists():
        shutil.rmtree(family_dir)

    return kept
