"""Host paths (contracts 03 §12, 04 §1.1/§1.6, 05 §2/§3.3.1).

One function per path, spelled once. A second copy of a path template is a
second place for a typo to strand a family."""

from __future__ import annotations

from pathlib import Path
from typing import Final

from agent_family.registry import FAMILY_FILE

#: Rework runtime state.
STATE_ROOT: Final = Path("/srv/agents/state/creche")

#: Root's own root: the release spool, every ancestor root's.
#: `handover.executor.layout`'s `RELEASE_ROOT` spelled a second time,
#: because the two packages share no module.
RELEASE_ROOT: Final = Path("/var/lib/creche-handover")

#: The configuration directory of the Rust chaperone: `install.toml` and
#: `plugins/` of root, and the `policy.toml` that this service writes. It
#: is `root:agents 1775`, so this service replaces its own file and no
#: file of root.
CHAPERONE_CONFIG_DIR: Final = Path("/etc/chaperone")

#: The binary that the `chaperone` component installs. This service runs
#: its `check` verb only.
CHAPERONE_BIN: Final = Path("/opt/components/chaperone/bin/chaperone")

#: The plugin tree inside the configuration directory of chaperone.
CHAPERONE_PLUGINS_DIR: Final = "plugins"

#: The configuration directory of chaperone for a run with a
#: `--state-root` of its own, inside that root.
CHAPERONE_SCRATCH_DIR: Final = "chaperone"

FAMILIES_DIR: Final = "families"
GRANTS_DIR: Final = "grants"
FAULTS_DIR: Final = "faults"
#: Contract 05 §6.4. `door-trigger` reads the same two names from its own
#: `DOOR_TRIGGER_WEBHOOK_DIR` default, so a change here is a change there.
TRIGGERS_DIR: Final = "triggers"
WEBHOOKS_DIR: Final = "webhooks"
WEBHOOK_TOKEN_SUFFIX: Final = ".token"

STATUS_FILE: Final = "status.json"
VALIDATION_FILE: Final = "validation.json"
CREDS_FILE: Final = "creds.json"
#: Contract 01 §6.1.1: the egress allowlist of the vmm, beside `config/`.
#: `FAMILY_EGRESS_FILE` of `creche-wire` in Rust spells the same name, and
#: the vmm reads it (decision CH69 rule 3).
EGRESS_FILE: Final = "egress.json"
#: Contract 01 §6.1.1: the clone list of the vmm mirror, beside
#: `egress.json`. `FAMILY_REPOS_FILE` of `creche-wire` in Rust spells the
#: same name, and the vmm reads it (decision CH69 rule 4).
REPOS_FILE: Final = "repos.json"
APPLIED_DIR: Final = "applied"
APPLIED_META_FILE: Final = "applied.json"
POLICY_FRAGMENT_SUFFIX: Final = ".toml"
POLICY_LOCK_FILE: Final = "policy.lock"


def family_dir(root: Path, family: str) -> Path:
    """`/srv/agents/state/creche/families/<family>/` — this family's own
    runtime state: status, validation report, credentials, the config
    directory, the reach files and the applied snapshot."""
    return root / FAMILIES_DIR / family


def status_path(root: Path, family: str) -> Path:
    """Contract 05 §2: the status document."""
    return family_dir(root, family) / STATUS_FILE


def validation_path(root: Path, family: str) -> Path:
    """Contract 05 §3.2: the full validation report `status.json` points at."""
    return family_dir(root, family) / VALIDATION_FILE


def creds_dir(root: Path, family: str) -> Path:
    """Contract 03 §12.2: the directory of `creds.json`. The vmm reads the
    file as the operator and serves it to the guest (decision CH5b)."""
    return family_dir(root, family) / "creds"


def creds_path(root: Path, family: str) -> Path:
    return creds_dir(root, family) / CREDS_FILE


def config_dir(root: Path, family: str) -> Path:
    """Contract 01 §6.1: `instructions.md`, `skills/` and `runtime.json`.
    The vmm reads the directory as the operator and serves it to the guest
    (decision CH5b)."""
    return family_dir(root, family) / "config"


def egress_path(root: Path, family: str) -> Path:
    """Contract 01 §6.1.1: `egress.json` in the family dir, beside
    `config/` and never inside it (decision CH69 rule 3)."""
    return family_dir(root, family) / EGRESS_FILE


def repos_path(root: Path, family: str) -> Path:
    """Contract 01 §6.1.1: `repos.json` in the family dir, beside
    `egress.json` and outside `config/` (decision CH69 rules 4 and 6)."""
    return family_dir(root, family) / REPOS_FILE


def applied_dir(root: Path, family: str) -> Path:
    """The last revision `caregiver` applied, kept verbatim so the
    reconciler can diff the registry against it (contract 05 §3.5)."""
    return family_dir(root, family) / APPLIED_DIR


def applied_family_path(root: Path, family: str) -> Path:
    """The applied copy of a family file. It has the name of the registry
    file, `family.toml`."""
    return applied_dir(root, family) / FAMILY_FILE


def applied_meta_path(root: Path, family: str) -> Path:
    return applied_dir(root, family) / APPLIED_META_FILE


def policy_fragment_path(root: Path, family: str) -> Path:
    """The app table of one family for the Rust chaperone:
    `/srv/agents/state/creche/grants/<family>.toml`. `policy_fleet.py`
    joins every fragment into one `policy.toml`."""
    return policy_fragments_dir(root) / f"{family}{POLICY_FRAGMENT_SUFFIX}"


def policy_fragments_dir(root: Path) -> Path:
    """The directory of the fragments."""
    return root / GRANTS_DIR


def policy_lock_path(root: Path) -> Path:
    """The `flock` file of the assembly of `policy.toml`. It is in this
    service's state, because the configuration directory of chaperone
    takes `policy.toml` and nothing else from this service."""
    return root / GRANTS_DIR / POLICY_LOCK_FILE


def webhooks_family_dir(root: Path, family: str) -> Path:
    """Contract 05 §6.4: every bearer of one family's declared webhooks.

    OUTSIDE `family_dir`, because `door-trigger` reads this tree and has no
    business in a family's runtime state: it gets one directory, not the
    directory that also holds `creds/` and the config mount."""
    return root / TRIGGERS_DIR / WEBHOOKS_DIR / family


def webhook_token_path(root: Path, family: str, name: str) -> Path:
    """Contract 05 §6.4: `<state>/triggers/webhooks/<family>/<name>.token`."""
    return webhooks_family_dir(root, family) / f"{name}{WEBHOOK_TOKEN_SUFFIX}"


def fault_path(root: Path, source: str, family: str) -> Path:
    """Contract 05 §3.3.1, contract 04 §1.6: one file per writer per family.
    `source` is the writer: `attendance`, `pep` or `vmm` (decision CH21-1),
    never `caregiver`, which reads these and writes none of them."""
    return root / FAULTS_DIR / source / f"{family}.json"
