"""The temporary root of one test, and every file a service reads there.

Each writer here follows a contract, never a module of a service. A service
in another language reads the same bytes, so no file below comes from a
product writer. `caregiver` is the writer of most of these files on the
host. A later topology starts it as a process and deletes the matching
writer here.

    <root>/
      sessions/<family>/                 contract 02 §9, the session store
      state/families/<family>/status.json          contract 05 §2
      state/families/<family>/supervisor-<id>.env  contract 03 §7.1
      state/families/<family>/creds/creds.json     contract 03 §12
      state/families/<family>/config/runtime.json  contract 01 §6.1
      state/families/<family>/control/<id>/        contract 03 §7.1
      state/tokens/<principal>.token     contract 02 §3 rule 5
      state/door-owui.key                contract 02 §3 rule 7
      sock/                              contract 02 §3 rule 1
      work/  log/  home/  bin/  standins/  proc-logs/
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

FAMILY: Final = "chat"
SANDBOX: Final = "chat-s1"
MODEL: Final = f"agent:{FAMILY}"
MODEL_ALIAS: Final = "agent-router"
CONFIG_REV: Final = "reg-c1-proc"
CRED_EPOCH: Final = 7

#: An obvious fixture, never resolved. A digest and never a tag
#: (contract 05 §4.1).
IMAGE: Final = "sha256:" + "0" * 63 + "1"

#: Obvious fixtures, never credentials. Each is long enough to pass the
#: 32-byte floor of contract 02 §3 rule 7.
DOOR_KEY: Final = "fixture-door-key-" + "d" * 32
FIXTURE_LITELLM_KEY: Final = "FIXTURE-LITELLM-KEY"
FIXTURE_PEP_TOKEN: Final = "FIXTURE-PEP-TOKEN"
_TOKEN_FILL: Final = "x" * 32

#: Contract 02 §3.1, one token file per row.
#:
#: CONTRACT-QUESTION: contract 02 §3.1 names the last token `caregiver`.
#: `attendance` reads `tokens/managerd.token`, and contract 05 §4.1 names the
#: principal `managerd`. Reading taken: the file name the host has, because a
#: service that reads another name cannot start there. A change costs this
#: one name.
PRINCIPALS: Final = (
    "door-owui",
    "door-tui",
    "door-delegate",
    "door-dispatch",
    "door-trigger",
    "view-ro",
    "managerd",
)

#: The two tokens the chaperone reads as another user (contract 02 §3 rule 5).
GROUP_READ_PRINCIPALS: Final = frozenset({"door-delegate", "door-dispatch"})

SECRET_MODE: Final = 0o600
GROUP_READ_MODE: Final = 0o640
PLAYPEN_ENV_MODE: Final = 0o640
STATUS_MODE: Final = 0o644

#: The site value a unit reads from the site file. TEST-NET-1: no host holds
#: it, and no test binds it.
LAN_ADDRESS: Final = "192.0.2.10"

#: macOS refuses a Unix socket path over 104 bytes.
MAX_SOCKET_PATH: Final = 100

_ROOT_PREFIX: Final = "cp"
_SOCKET_NAME: Final = "sessiond.sock"


def repo_root() -> Path:
    """The checkout this file is in. `integration/proc/proc_tree.py` is 3 deep."""
    return Path(__file__).resolve().parents[2]


def playpen_bundle() -> Path:
    """The built playpen. `pnpm run build` in `playpen/` writes it."""
    return repo_root() / "playpen" / "dist" / "playpen.js"


@dataclass(frozen=True, slots=True)
class Mounts:
    """The four directories of contract 03 §7.1, for one sandbox."""

    sessions: Path
    creds: Path
    config: Path
    control: Path


@dataclass(frozen=True, slots=True)
class Tree:
    """Every path of one test. One root, and nothing outside it."""

    root: Path

    @property
    def sessions_root(self) -> Path:
        return self.root / "sessions"

    @property
    def state_root(self) -> Path:
        return self.root / "state"

    @property
    def work_root(self) -> Path:
        return self.root / "work"

    @property
    def log_dir(self) -> Path:
        """Where `attendance` writes the stderr of each playpen."""
        return self.root / "log"

    @property
    def proc_logs(self) -> Path:
        """Where the harness writes the stdout and stderr of each child."""
        return self.root / "proc-logs"

    @property
    def home(self) -> Path:
        return self.root / "home"

    @property
    def bin_dir(self) -> Path:
        """First on the PATH of every service. It holds the stand-in programs."""
        return self.root / "bin"

    @property
    def standins(self) -> Path:
        """Where each stand-in program records its calls."""
        return self.root / "standins"

    @property
    def families_dir(self) -> Path:
        return self.state_root / "families"

    @property
    def tokens_dir(self) -> Path:
        return self.state_root / "tokens"

    @property
    def door_key_file(self) -> Path:
        return self.state_root / "door-owui.key"

    @property
    def attendance_socket(self) -> Path:
        return self.root / "sock" / _SOCKET_NAME

    def token_file(self, principal: str) -> Path:
        return self.tokens_dir / f"{principal}.token"

    def family_dir(self, family: str = FAMILY) -> Path:
        return self.families_dir / family

    def status_file(self, family: str = FAMILY) -> Path:
        return self.family_dir(family) / "status.json"

    def playpen_env(self, sandbox: str = SANDBOX, family: str = FAMILY) -> Path:
        return self.family_dir(family) / f"supervisor-{sandbox}.env"

    def mounts(self, sandbox: str = SANDBOX, family: str = FAMILY) -> Mounts:
        return Mounts(
            sessions=self.sessions_root / family,
            creds=self.family_dir(family) / "creds",
            config=self.family_dir(family) / "config",
            control=self.family_dir(family) / "control" / sandbox,
        )

    def playpen_lock(self, sandbox: str = SANDBOX, family: str = FAMILY) -> Path:
        """The lease of contract 03 §11.1, in the control directory."""
        return self.mounts(sandbox, family).control / "supervisor.lock"

    def session_dir(self, session: str, family: str = FAMILY) -> Path:
        return self.sessions_root / family / session

    def journal_lines(self, session: str, family: str = FAMILY) -> list[dict[str, Any]]:
        """The journal of one session, as contract 02 §8 puts it on disk.

        LF is the only delimiter. A last line with no LF is one that
        `attendance` is still writing, and it is left out.
        """
        path = self.session_dir(session, family) / "journal.ndjson"

        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return []

        complete = raw.split(b"\n")[:-1]

        return [json.loads(line) for line in complete if line.strip()]

    def journal_kinds(self, session: str, family: str = FAMILY) -> list[str]:
        return [str(line.get("kind", "")) for line in self.journal_lines(session, family)]


def make_root() -> Path:
    """A new directory, short enough to hold a Unix socket path."""
    return Path(tempfile.mkdtemp(prefix=_ROOT_PREFIX))


def remove_root(root: Path) -> None:
    shutil.rmtree(root, ignore_errors=True)


def socket_path_fits(tree: Tree) -> bool:
    """Whether this platform can bind the socket path of the tree."""
    return len(os.fsencode(tree.attendance_socket)) <= MAX_SOCKET_PATH


def build_tree(tree: Tree) -> None:
    """Write everything the first topology reads, for one attended family."""
    mounts = tree.mounts()

    for directory in (
        mounts.sessions,
        mounts.creds,
        mounts.config,
        mounts.control,
        tree.work_root,
        tree.log_dir,
        tree.home,
        tree.bin_dir,
        tree.standins,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    write_playpen_env(tree)
    write_status(tree)
    write_creds(tree)
    write_runtime(tree)
    write_tokens(tree)
    write_door_key(tree)


def write_status(
    tree: Tree,
    *,
    family: str = FAMILY,
    kind: str = "attended",
    state: str = "in_sync",
    sandboxes: tuple[tuple[str, str], ...] = ((SANDBOX, "ready"),),
) -> None:
    """One whole status document (contract 05 §2.1, §4.1, §9).

    Every field of §2.1 is present, so a reader that refuses a document with
    a field missing still accepts this one. `pep.watch` is `off`: no test of
    the first topology starts the chaperone, and `off` means nobody asked
    (§2.2 rule 1).
    """
    now = _rfc3339()
    document: dict[str, Any] = {
        "family": family,
        "kind": kind,
        "state": state,
        "written_at": now,
        "registry_rev": CONFIG_REV,
        "applied_rev": CONFIG_REV,
        "config_rev": CONFIG_REV,
        "validation": {
            "rev": CONFIG_REV,
            "checked_at": now,
            "ok": True,
            "never_valid": False,
            "error_count": 0,
            "warning_count": 0,
            "report_path": str(tree.family_dir(family) / "validation.json"),
            "first_error": None,
        },
        "faults": [],
        "reconcile": None,
        "sandboxes": [
            _sandbox_row(tree, family, box, box_state, now) for box, box_state in sandboxes
        ],
        "credentials": {
            "epoch": CRED_EPOCH,
            "key_id": f"fixture-key-{CRED_EPOCH}",
            "token_id": f"fixture-token-{CRED_EPOCH}",
            "rotated_at": now,
            "next_rotation_at": None,
            "rotation_state": "settled",
        },
        "spend": {
            "window": "day",
            "spend_usd": 0.0,
            "budget_usd": 15.0,
            "as_of": now,
            "source": "litellm",
        },
        "limits": {"max_running_turns": None, "max_queued_turns": None, "job_timeout_s": None},
        "triggers": {"webhooks": [], "enqueue": False},
        "pep": {"watch": "off", "url": "", "checked_at": None, "unreachable_since": None},
    }

    _atomic_write(tree.status_file(family), json.dumps(document) + "\n", STATUS_MODE)


def write_playpen_env(tree: Tree, *, sandbox: str = SANDBOX, family: str = FAMILY) -> None:
    """The file `sbx exec --env-file` reads (contract 03 §7.1).

    Four lines, each a host path or an identifier, and no secret.
    """
    mounts = tree.mounts(sandbox, family)
    mounts.control.mkdir(parents=True, exist_ok=True)
    lines = [
        f"AGENT_CRED_DIR={mounts.creds}",
        f"AGENT_FAMILY_CONFIG_DIR={mounts.config}",
        f"AGENT_CONTROL_DIR={mounts.control}",
        f"AGENT_SANDBOX={sandbox}",
    ]

    _atomic_write(tree.playpen_env(sandbox, family), "\n".join(lines) + "\n", PLAYPEN_ENV_MODE)


def write_creds(tree: Tree, *, family: str = FAMILY) -> None:
    """The credential file the playpen reads at each pi start (contract 03 §12)."""
    document = {
        "epoch": CRED_EPOCH,
        "litellm_key": FIXTURE_LITELLM_KEY,
        "pep_token": FIXTURE_PEP_TOKEN,
        "written_at": _rfc3339(),
    }

    _atomic_write(
        tree.mounts(family=family).creds / "creds.json", json.dumps(document) + "\n", SECRET_MODE
    )


def write_runtime(tree: Tree, *, family: str = FAMILY) -> None:
    """`runtime.json` of the family config mount (contract 01 §6.1)."""
    document: dict[str, object] = {"shell": False, "sandbox_tools": [], "model_alias": MODEL_ALIAS}

    _atomic_write(
        tree.mounts(family=family).config / "runtime.json", json.dumps(document) + "\n", STATUS_MODE
    )


def write_tokens(tree: Tree) -> None:
    """One token file per principal, at the mode contract 02 §3 rule 5 names."""
    tree.tokens_dir.mkdir(parents=True, exist_ok=True)

    for principal in PRINCIPALS:
        mode = GROUP_READ_MODE if principal in GROUP_READ_PRINCIPALS else SECRET_MODE
        _atomic_write(tree.token_file(principal), token_of(principal) + "\n", mode)


def token_of(principal: str) -> str:
    """The fixture bearer of one principal. Never a credential."""
    return f"{principal}-{_TOKEN_FILL}"


def write_door_key(tree: Tree) -> None:
    _atomic_write(tree.door_key_file, DOOR_KEY + "\n", SECRET_MODE)


def _sandbox_row(tree: Tree, family: str, sandbox: str, state: str, now: str) -> dict[str, Any]:
    return {
        "id": sandbox,
        "state": state,
        "power": "running",
        "image": IMAGE,
        "spec_hash": "00000001",
        "cpus": 2,
        "memory": "4g",
        "created_at": now,
        "ready_at": now if state == "ready" else None,
        "channel": "closed",
        "supervisor_env": str(tree.playpen_env(sandbox, family)),
    }


def _atomic_write(path: Path, text: str, mode: int) -> None:
    """Temp file, then `rename`, so a running reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(text, encoding="utf-8")
    temp.chmod(mode)
    temp.replace(path)


def _rfc3339() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
