"""Host paths (contracts 03 §7.1/§12, 04 §1.1/§1.6, 05 §2/§3.3.1/§4.1).

One function per path, spelled once. A second copy of a path template is a
second place for a typo to strand a family."""

from __future__ import annotations

from pathlib import Path
from typing import Final

from agent_family import FamilyFile
from agent_family.registry import FAMILY_FILE

#: Rework runtime state.
STATE_ROOT: Final = Path("/srv/agents/state/creche")

#: Root's own root: the release spool, every ancestor root's.
#: `handover.executor.layout`'s `RELEASE_ROOT` spelled a second time,
#: because the two packages share no module.
RELEASE_ROOT: Final = Path("/var/lib/creche-handover")

#: Contract 03 §7.1: outside
#: `STATE_ROOT`. The session store belongs to the runtime, not the family
#: (contract 01 §3.3 rule 6), so `caregiver` mounts it without ever reading
#: or writing its contents.
SESSIONS_ROOT: Final = Path("/srv/agents/sessions")

#: Contract 01 §5.4: the index store of `library/`, one of the allowed
#: roots. A family that mounts it reads it at this same path inside the VM.
INDEX_ROOT: Final = Path("/srv/agents/state/index")
#: The mode of a family mount that `index_root` takes.
_READ_ONLY: Final = "ro"

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
TOKENS_DIR: Final = "tokens"
#: Contract 05 §6.4. `door-trigger` reads the same two names from its own
#: `DOOR_TRIGGER_WEBHOOK_DIR` default, so a change here is a change there.
TRIGGERS_DIR: Final = "triggers"
WEBHOOKS_DIR: Final = "webhooks"
WEBHOOK_TOKEN_SUFFIX: Final = ".token"

#: Contract 02 §3.1's principal name for this service. It keeps the old
#: name, because the host minted the token file under it.
CAREGIVER_PRINCIPAL: Final = "caregiver"

STATUS_FILE: Final = "status.json"
VALIDATION_FILE: Final = "validation.json"
SANDBOX_SEQ_FILE: Final = "sandbox-seq"
CREDS_FILE: Final = "creds.json"
#: Contract 03 §7.1's file, one per sandbox: `supervisor-<family>-s<N>.env`.
PLAYPEN_ENV_PREFIX: Final = "supervisor-"
PLAYPEN_ENV_SUFFIX: Final = ".env"
SANDBOXES_FILE: Final = "sandboxes.json"
APPLIED_DIR: Final = "applied"
APPLIED_META_FILE: Final = "applied.json"
POLICY_FRAGMENT_SUFFIX: Final = ".toml"
POLICY_LOCK_FILE: Final = "policy.lock"


def family_dir(root: Path, family: str) -> Path:
    """`/srv/agents/state/creche/families/<family>/` — this family's own
    runtime state: status, validation report, credentials, config mount,
    control directory, sandbox counter."""
    return root / FAMILIES_DIR / family


def status_path(root: Path, family: str) -> Path:
    """Contract 05 §2: the status document."""
    return family_dir(root, family) / STATUS_FILE


def validation_path(root: Path, family: str) -> Path:
    """Contract 05 §3.2: the full validation report `status.json` points at."""
    return family_dir(root, family) / VALIDATION_FILE


def creds_dir(root: Path, family: str) -> Path:
    """Contract 03 §12.2, §7.1: mounted read-only, at this same path inside
    the VM."""
    return family_dir(root, family) / "creds"


def creds_path(root: Path, family: str) -> Path:
    return creds_dir(root, family) / CREDS_FILE


def config_dir(root: Path, family: str) -> Path:
    """Contract 01 §6.1, contract 03 §7.1: mounted read-only, at this same
    path inside the VM. Holds `instructions.md`, `skills/`,
    `runtime.json`."""
    return family_dir(root, family) / "config"


def control_root(root: Path, family: str) -> Path:
    """The parent of every sandbox's control directory. `caregiver` makes it
    and mounts nothing from it: only the per-sandbox directories below are
    mounted (contract 03 §7.1)."""
    return family_dir(root, family) / "control"


def control_dir(root: Path, family: str, sandbox: str) -> Path:
    """Contract 03 §7.1, §11.1: mounted rw, at this same path inside the
    VM. PER SANDBOX, because contract 05 §5 keeps two sandboxes of one
    family live at once and §11.1's lock, §7.4's turn files and §7.5's
    process records all belong to one VM."""
    return control_root(root, family) / sandbox


def playpen_env_path(root: Path, family: str, sandbox: str) -> Path:
    """Contract 03 §7.1: what `sbx exec --env-file` hands the playpen.
    One file per sandbox, because it names `AGENT_SANDBOX` and that
    sandbox's own control directory. BESIDE the control directory, never
    inside it — that one is emptied at every sandbox create and is mounted
    into the untrusted sandbox."""
    name = f"{PLAYPEN_ENV_PREFIX}{sandbox}{PLAYPEN_ENV_SUFFIX}"
    return family_dir(root, family) / name


def sandbox_seq_path(root: Path, family: str) -> Path:
    """Contract 05 §4.1: the monotonic, never-reused sandbox counter."""
    return family_dir(root, family) / SANDBOX_SEQ_FILE


def sandboxes_path(root: Path, family: str) -> Path:
    """The reconciler's sandbox ledger: every id this family was ever
    given, and what became of it. `sandbox-seq` says how far the counter
    went; this says which of those ids still exist (contract 05 §4.2)."""
    return family_dir(root, family) / SANDBOXES_FILE


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


def read_sandbox_sequence(root: Path, family: str) -> int:
    """The counter's current value, or 0 when it does not exist yet or
    holds something unreadable. Never raises: a missing or corrupt
    counter means "no sandbox created yet", not a
    crash — the same invariant-19 spirit applied to caregiver's own state,
    not only to a family file."""
    try:
        return int(sandbox_seq_path(root, family).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def session_dir(family: str) -> Path:
    """`/srv/agents/sessions/<family>/`, mounted rw at this same path inside
    the VM. It is the primary workspace (contract 03 §7.1). `SbxDriver`
    makes it when it is missing, so a new or renamed family gets one."""
    return SESSIONS_ROOT / family


def index_root(family: FamilyFile) -> Path | None:
    """`INDEX_ROOT` when the family file mounts that path read-only, else
    `None`. A mount of another path under it does not count."""
    for mount in family.files:
        if Path(mount.path) == INDEX_ROOT and mount.mode == _READ_ONLY:
            return INDEX_ROOT

    return None


def sandbox_id(family: str, sequence: int) -> str:
    """`<family>-s<N>` (contract 05 §4.1). The sbx sandbox name equals the
    id: one name, one meaning."""
    return f"{family}-s{sequence}"


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


def caregiver_token_path(root: Path) -> Path:
    """The bearer `attendance` expects on `/internal/switch-sandbox`
    (contract 02 §3 rule 5: one token file per caller). `attendance` owns
    this directory and mints the file; `caregiver` only reads it."""
    return root / TOKENS_DIR / f"{CAREGIVER_PRINCIPAL}.token"


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
    `source` is a wire name, `sessiond` or `pep` (attendance's and the PEP's)
    — never `caregiver`, which reads these, never writes them."""
    return root / FAULTS_DIR / source / f"{family}.json"
