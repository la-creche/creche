"""Delete a family entirely.

Credentials die before processes (invariant 13): the LiteLLM key and the
PEP grant file are gone before any sandbox is touched. This module does
not decide *when* deleting a family is the right action -- the caller (the
CLI) does -- it only performs the deletion in the order invariant 13
requires, and removes caregiver's own state for that family once nothing
live depends on it."""

from __future__ import annotations

import shutil
from pathlib import Path

from . import paths
from .driver import SandboxDriver
from .egress import EgressConfig
from .grants import delete_grant_file
from .litellm_keys import LiteLLMKeys
from .webhook_tokens import forget_webhooks


def delete_family(
    family_name: str,
    *,
    state_root: Path,
    driver: SandboxDriver,
    litellm: LiteLLMKeys,
    egress: tuple[str, ...] = (),
    egress_config: EgressConfig | None = None,
) -> None:
    """1. The LiteLLM key. 2. The grant file (it carries the token's
    digest; deleting it revokes the token, contract 04 section 1.3 rule
    5). 3. `creds.json`. 4. Every sandbox this family ever had. 5. This
    family's own state directory.

    `egress` is the family's last-known egress list. Every destroy also
    removes the two plane endpoints `egress_config` names, defaulted the
    same way `apply_once` defaults them: a caller that passed only the
    family's own list would otherwise leak two policy rows per sandbox
    (contract 05 section 4.4 step 3). `sbx policy rm` runs for each of
    these, for each sandbox (README §6: `sbx rm -f` alone does not
    remove policy rows). An empty `egress` default leaves inert rules for
    a sandbox name nothing will reuse -- a known, accepted leak, not a
    reach a deleted family still has."""
    litellm.delete_key(family_name)
    delete_grant_file(paths.grant_path(state_root, family_name))
    paths.creds_path(state_root, family_name).unlink(missing_ok=True)
    # A webhook bearer lives outside `family_dir` (contract 05 §6.4), so
    # step 5's rmtree does not reach it. It is a credential, so it goes
    # here, with the others, before any sandbox is touched.
    forget_webhooks(state_root, family_name)

    planes = egress_config if egress_config is not None else EgressConfig()
    allow = planes.allowed(egress)

    sequence = paths.read_sandbox_sequence(state_root, family_name)
    for one in range(1, sequence + 1):
        driver.destroy(paths.sandbox_id(family_name, one), allow)

    family_dir = paths.family_dir(state_root, family_name)
    if family_dir.exists():
        shutil.rmtree(family_dir)
