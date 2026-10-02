"""Every path this service reads or writes (contract 02 §9, contract 05).

Path construction sits in one module so no caller builds a path by hand. Both
roots are configurable, so a test drives the whole service inside `tmp_path`.
"""

from __future__ import annotations

import os
from pathlib import Path

SESSION_FILE = "session.json"
JOURNAL_FILE = "journal.ndjson"
TURNS_DIR = "turns"
PI_DIR = "pi"
INBOX_DIR = "inbox"
OUTBOX_DIR = "outbox"
SCRATCH_DIR = "scratch"

STATUS_FILE = "status.json"
SUPERVISOR_LOCK = "supervisor.lock"

_FAMILIES = "families"
_CONTROL = "control"
_CREDS = "creds"
_CONFIG = "config"
_FAULTS = "faults"
_FAULT_SOURCE = "sessiond"
_LEASES = "leases"
_TOKENS = "tokens"
_OUTCOMES = "outcomes"
_DISPATCH = "dispatch"
_AUDIT = "audit"
_CODE_SANDBOX = "code-sandbox"

# What the sandbox sees. sbx mounts /srv/agents/sessions/<family>/ at that
# SAME path inside the VM (contract 03 §7.1), so a path sent on the channel is
# the host path. There is no second name for it and nothing rewrites one.
#
# The integration harness runs the supervisor as a plain child process on a
# Mac, under a temp directory no sbx mount put there, so it overrides the root
# below. Nothing else sets this, on the host or anywhere.
SANDBOX_MOUNT_ENV = "SESSIOND_SANDBOX_SESSIONS_MOUNT"


def family_sessions_dir(sessions_root: Path, family: str) -> Path:
    return sessions_root / family


def session_dir(sessions_root: Path, family: str, session: str) -> Path:
    return sessions_root / family / session


def session_file(sessions_root: Path, family: str, session: str) -> Path:
    return session_dir(sessions_root, family, session) / SESSION_FILE


def journal_file(sessions_root: Path, family: str, session: str) -> Path:
    return session_dir(sessions_root, family, session) / JOURNAL_FILE


def turns_dir(sessions_root: Path, family: str, session: str) -> Path:
    return session_dir(sessions_root, family, session) / TURNS_DIR


def turn_file(sessions_root: Path, family: str, session: str, turn: str) -> Path:
    return turns_dir(sessions_root, family, session) / f"{turn}.json"


def sandbox_sessions_mount(sessions_root: Path, family: str) -> str:
    """Where the sandbox sees this family's sessions (contract 03 §7.1).

    The default is identity: the host path IS the in-VM path, because that
    is the only thing `sbx create` can do with a mount.
    """
    override = os.environ.get(SANDBOX_MOUNT_ENV, "").strip()

    return override or str(family_sessions_dir(sessions_root, family))


def sandbox_cwd(sessions_root: Path, family: str, session: str) -> str:
    """The turn's working directory inside the sandbox (contract 03 §4.1)."""
    return f"{sandbox_sessions_mount(sessions_root, family)}/{session}"


def sandbox_session_dir(sessions_root: Path, family: str, session: str) -> str:
    """PI_CODING_AGENT_DIR inside the sandbox (contract 03 §4.1)."""
    return f"{sandbox_cwd(sessions_root, family, session)}/{PI_DIR}"


def family_state_dir(state_root: Path, family: str) -> Path:
    return state_root / _FAMILIES / family


def status_file(state_root: Path, family: str) -> Path:
    """The document `managerd` publishes (contract 05 §2)."""
    return family_state_dir(state_root, family) / STATUS_FILE


def control_dir(state_root: Path, family: str, sandbox: str) -> Path:
    """The supervisor's control mount (contract 03 §7.1). PER SANDBOX:
    contract 05 §5 keeps two sandboxes of one family live at once, in two
    microVMs, and each supervisor beats its own lock in its own
    directory."""
    return family_state_dir(state_root, family) / _CONTROL / sandbox


def supervisor_lock_file(state_root: Path, family: str, sandbox: str) -> Path:
    """Proof that an old supervisor may still live (contract 03 §11.4).

    One lock per sandbox. A family-wide lock made the incoming sandbox
    watch the OUTGOING supervisor's beat, so the handshake a switch asks
    for could never pass while the family was still being served."""
    return control_dir(state_root, family, sandbox) / SUPERVISOR_LOCK


def creds_dir(state_root: Path, family: str) -> Path:
    """Written by `managerd`, mounted read-only (contract 03 §12)."""
    return family_state_dir(state_root, family) / _CREDS


def config_dir(state_root: Path, family: str) -> Path:
    """The family config mount (contract 03 §7.1)."""
    return family_state_dir(state_root, family) / _CONFIG


def families_dir(state_root: Path) -> Path:
    """Listed to find every family. No index file exists (contract 05 §2)."""
    return state_root / _FAMILIES


def fault_file(state_root: Path, family: str) -> Path:
    """This service's own open faults (contract 05 §3.3.1)."""
    return state_root / _FAULTS / _FAULT_SOURCE / f"{family}.json"


def faults_dir(state_root: Path) -> Path:
    return state_root / _FAULTS / _FAULT_SOURCE


def lease_file(state_root: Path, family: str, session: str) -> Path:
    """The lease mirror. A report, not a lock (contract 02 §7.1)."""
    return state_root / _LEASES / family / f"{session}.json"


def token_file(state_root: Path, door: str) -> Path:
    """One token file per door (contract 02 §3 rule 5)."""
    return state_root / _TOKENS / f"{door}.token"


def outcome_file(state_root: Path, family: str, outcome_id: str) -> Path:
    """Autonomous outcome records (contract 02 §13.1)."""
    return state_root / _OUTCOMES / family / f"{outcome_id}.json"


def dispatch_dir(state_root: Path, caller_family: str) -> Path:
    """One caller family's dispatch ledger (contract 02 §13.4.3).

    Keyed by the family that ENQUEUED, not by the family that runs the job:
    the read is "what became of the jobs I started", and a directory listing
    is the whole of it.
    """
    return state_root / _DISPATCH / caller_family


def dispatch_file(state_root: Path, caller_family: str, session: str) -> Path:
    """One ledger entry. The session id is the file name (§13.4.3)."""
    return dispatch_dir(state_root, caller_family) / f"{session}.json"


def audit_file(state_root: Path, day: str) -> Path:
    """The PEP's daily audit (contract 04 §6, contract 05 §8).

    The PEP writes it and nothing else does. This service reads it for the
    one thing contract 04 publishes about an open approval gate (§6.4).
    """
    return state_root / _AUDIT / f"{day}.jsonl"


def code_sandbox_dir(work_root: Path, owner_session: str) -> Path:
    """The per-chat code-sandbox directory (contract 02 §12.1)."""
    return work_root / _CODE_SANDBOX / owner_session
