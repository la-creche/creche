"""Host paths (contracts 03 §7.1/§12, 04 §1.1/§1.6, 05 §2/§3.3.1/§4.1).

One function per path, spelled once. A second copy of a path template is a
second place for a typo to strand a family."""

from __future__ import annotations

from pathlib import Path
from typing import Final

#: Rework runtime state.
STATE_ROOT: Final = Path("/srv/agents/state/rework")

#: Root's own root: the release spool, the sealed secrets and the roster,
#: every ancestor root's. `agent_release.executor.layout`'s `RELEASE_ROOT`
#: spelled a second time, because the two packages share no module.
#: `release/tests/test_release_r7h_roster.py` holds the two equal.
RELEASE_ROOT: Final = Path("/var/lib/agent-release")

#: Contract 03 §7.1: outside
#: `STATE_ROOT`. The session store belongs to the runtime, not the family
#: (contract 01 §3.3 rule 6), so `managerd` mounts it without ever reading
#: or writing its contents.
SESSIONS_ROOT: Final = Path("/srv/agents/sessions")

FAMILIES_DIR: Final = "families"
GRANTS_DIR: Final = "grants"
FAULTS_DIR: Final = "faults"
AUDIT_DIR: Final = "audit"
TOKENS_DIR: Final = "tokens"
#: Contract 05 §6.4. `door-trigger` reads the same two names from its own
#: `DOOR_TRIGGER_WEBHOOKS_DIR` default, so a change here is a change there.
TRIGGERS_DIR: Final = "triggers"
WEBHOOKS_DIR: Final = "webhooks"
WEBHOOK_TOKEN_SUFFIX: Final = ".token"

#: Contract 02 §3.1's principal name for this service.
MANAGERD_PRINCIPAL: Final = "managerd"

STATUS_FILE: Final = "status.json"
VALIDATION_FILE: Final = "validation.json"
SANDBOX_SEQ_FILE: Final = "sandbox-seq"
CREDS_FILE: Final = "creds.json"
#: Contract 03 §7.1's file, one per sandbox: `supervisor-<family>-s<N>.env`.
PLAYPEN_ENV_PREFIX: Final = "supervisor-"
PLAYPEN_ENV_SUFFIX: Final = ".env"
SANDBOXES_FILE: Final = "sandboxes.json"
APPLIED_DIR: Final = "applied"
APPLIED_FAMILY_FILE: Final = "family.yaml"
APPLIED_META_FILE: Final = "applied.json"


def family_dir(root: Path, family: str) -> Path:
    """`/srv/agents/state/rework/families/<family>/` — this family's own
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
    """The parent of every sandbox's control directory. `managerd` makes it
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
    """The last revision `managerd` applied, kept verbatim so the
    reconciler can diff the registry against it (contract 05 §3.5)."""
    return family_dir(root, family) / APPLIED_DIR


def applied_family_path(root: Path, family: str) -> Path:
    return applied_dir(root, family) / APPLIED_FAMILY_FILE


def applied_meta_path(root: Path, family: str) -> Path:
    return applied_dir(root, family) / APPLIED_META_FILE


def read_sandbox_sequence(root: Path, family: str) -> int:
    """The counter's current value, or 0 when it does not exist yet or
    holds something unreadable. Never raises: a missing or corrupt
    counter means "no sandbox created yet", not a
    crash — the same invariant-19 spirit applied to managerd's own state,
    not only to a family file."""
    try:
        return int(sandbox_seq_path(root, family).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def session_dir(family: str) -> Path:
    """`/srv/agents/sessions/<family>/`, mounted rw at this same path inside
    the VM. It is the primary workspace (contract 03 §7.1)."""
    return SESSIONS_ROOT / family


def sandbox_id(family: str, sequence: int) -> str:
    """`<family>-s<N>` (contract 05 §4.1). The sbx sandbox name equals the
    id: one name, one meaning."""
    return f"{family}-s{sequence}"


def grant_path(root: Path, family: str) -> Path:
    """Contract 04 §1.1: `/srv/agents/state/rework/grants/<family>.json`."""
    return root / GRANTS_DIR / f"{family}.json"


def managerd_token_path(root: Path) -> Path:
    """The bearer `sessiond` expects on `/internal/switch-sandbox`
    (contract 02 §3 rule 5: one token file per caller). `sessiond` owns
    this directory and mints the file; `managerd` only reads it."""
    return root / TOKENS_DIR / f"{MANAGERD_PRINCIPAL}.token"


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
    `source` is `sessiond` or `pep` — never `managerd`, which reads these,
    never writes them."""
    return root / FAULTS_DIR / source / f"{family}.json"
