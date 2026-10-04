"""The temporary root of one test, and every file a service reads there.

Each writer here follows a contract, never a module of a service. A service
in another language reads the same bytes, so no file below comes from a
product writer. `caregiver` is the writer of most of these files on the
host. A topology with no `caregiver` process uses the writers here. A
topology with one writes only the registry and the token files, and
`caregiver` writes the rest.

    <root>/
      registry/families/<family>/family.yaml       contract 01 §1
      registry/families/<family>/instructions.md   contract 01 §1
      sessions/<family>/                 contract 02 §9, the session store
      state/families/<family>/status.json          contract 05 §2
      state/families/<family>/validation.json      contract 05 §3.2
      state/families/<family>/supervisor-<id>.env  contract 03 §7.1
      state/families/<family>/creds/creds.json     contract 03 §12
      state/families/<family>/config/              contract 01 §6.1
      state/families/<family>/control/<id>/        contract 03 §7.1
      state/tokens/<principal>.token     contract 02 §3 rule 5
      state/door-owui.key                contract 02 §3 rule 7
      state/grants/<family>.json         contract 04 §1, the grant file
      state/faults/<writer>/<family>.json          contract 05 §3.3.1
      state/audit/<day>.jsonl            contract 04 §6, written by the chaperone
      sock/                              contract 02 §3 rule 1
      release/                           the root of the release executor, empty
      home/.config/systemd/user/         the unit directory of a user manager
      work/  log/  home/  bin/  standins/  proc-logs/
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

import yaml

FAMILY: Final = "chat"
SANDBOX: Final = "chat-s1"
MODEL: Final = f"agent:{FAMILY}"

#: The two family kinds of contract 02 §2 that a fixture publishes.
ATTENDED: Final = "attended"
THIN: Final = "thin"

#: What `caregiver` puts in `instructions.md` of a fixture family.
INSTRUCTIONS: Final = "Be helpful.\n"

#: Contract 01 §3.2: the budget of a fixture family, in dollars each day.
BUDGET_USD: Final = 15

#: Contract 05 §3.3.1: the directory of the fault files that `attendance`
#: writes.
FAULTS_OF_ATTENDANCE: Final = "sessiond"


class Validity(StrEnum):
    """What the newest family file was, for the status document (contract 05 §3)."""

    #: The newest file validated. The family is `in_sync`.
    VALID = "valid"
    #: The newest file failed, and the last good definition serves (§3.1).
    INVALID = "invalid"
    #: No revision ever validated, so nothing serves (§3.1).
    NEVER_VALID = "never_valid"


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
_TOKEN_FILL: Final = "x" * 32

#: Contract 01 §3.12: the default limit of one thin job, in seconds.
JOB_TIMEOUT_S: Final = 120

#: Contract 04 §1.2: the two defaults `caregiver` writes, and the default of
#: the family file for `max_inflight_delegations`.
GRANT_LIMITS: Final = {"pep_rpm": 60, "max_inflight_delegations": 2, "max_open_gates": 10}
GRANT_VERSION: Final = 2
GRANT_MODE: Final = 0o640

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
    def registry_root(self) -> Path:
        """The registry that `caregiver` watches (contract 01 §1)."""
        return self.root / "registry"

    @property
    def release_root(self) -> Path:
        """What plays the root of the release executor. The suite leaves it empty."""
        return self.root / "release"

    @property
    def unit_dir(self) -> Path:
        """Where a user manager reads a unit from: `$XDG_CONFIG_HOME/systemd/user`."""
        return self.home / ".config" / "systemd" / "user"

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

    def status(self, family: str = FAMILY) -> dict[str, Any] | None:
        """The status document of one family, or None while it has none.

        Its writer puts it in place by rename (contract 05 §2 rule 2), so a
        file that is there is a whole document.
        """
        try:
            document: dict[str, Any] = json.loads(
                self.status_file(family).read_text(encoding="utf-8")
            )
        except FileNotFoundError:
            return None

        return document

    def validation_file(self, family: str = FAMILY) -> Path:
        """The whole validation report (contract 05 §3.2)."""
        return self.family_dir(family) / "validation.json"

    def creds_file(self, family: str = FAMILY) -> Path:
        """The credential file of contract 03 §12."""
        return self.mounts(family).creds / "creds.json"

    def fault_file(self, writer: str, family: str = FAMILY) -> Path:
        """The open faults that one service reports for one family (contract 05 §3.3.1)."""
        return self.state_root / "faults" / writer / f"{family}.json"

    def registry_family_dir(self, family: str = FAMILY) -> Path:
        return self.registry_root / "families" / family

    def family_file(self, family: str = FAMILY) -> Path:
        return self.registry_family_dir(family) / "family.yaml"

    def playpen_env(self, family: str = FAMILY, sandbox: str | None = None) -> Path:
        return self.family_dir(family) / f"supervisor-{sandbox or first_sandbox(family)}.env"

    def mounts(self, family: str = FAMILY, sandbox: str | None = None) -> Mounts:
        return Mounts(
            sessions=self.sessions_root / family,
            creds=self.family_dir(family) / "creds",
            config=self.family_dir(family) / "config",
            control=self.family_dir(family) / "control" / (sandbox or first_sandbox(family)),
        )

    def playpen_lock(self, family: str = FAMILY, sandbox: str | None = None) -> Path:
        """The lease of contract 03 §11.1, in the control directory."""
        return self.mounts(family, sandbox).control / "supervisor.lock"

    def grant_file(self, family: str = FAMILY) -> Path:
        return self.state_root / "grants" / f"{family}.json"

    def sessions_of(self, family: str) -> list[str]:
        """Every session directory one family has on disk now."""
        directory = self.sessions_root / family

        return sorted(entry.name for entry in directory.iterdir() if entry.is_dir())

    def audit_lines(self) -> list[dict[str, Any]]:
        """Every audit record the chaperone wrote, in file order (contract 04 §6)."""
        lines: list[dict[str, Any]] = []

        for path in sorted((self.state_root / "audit").glob("*.jsonl")):
            complete = path.read_bytes().split(b"\n")[:-1]
            lines.extend(json.loads(line) for line in complete if line.strip())

        return lines

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


def first_sandbox(family: str) -> str:
    """The id of the one sandbox a fixture family has (contract 02 §2)."""
    return f"{family}-s1"


def pep_token_of(family: str) -> str:
    """The fixture family token (contract 04 §2). Never a credential."""
    return f"FIXTURE-PEP-TOKEN-{family}"


def build_tree(tree: Tree) -> None:
    """Write everything the first topology reads, for one attended family."""
    build_bare_tree(tree)
    add_family(tree, FAMILY, ATTENDED)


def build_bare_tree(tree: Tree) -> None:
    """Write what no family owns: the directories, the tokens and the door key.

    A topology with a `caregiver` process starts from this tree and a
    registry. `caregiver` publishes each family.
    """
    for directory in (tree.work_root, tree.log_dir, tree.home, tree.bin_dir, tree.standins):
        directory.mkdir(parents=True, exist_ok=True)

    write_tokens(tree)
    write_door_key(tree)


def family_body(family: str = FAMILY, kind: str = ATTENDED, **fields: object) -> dict[str, Any]:
    """One family file as a mapping (contract 01 §2).

    Only the required fields, with an empty `egress` list: contract 01 §3.7
    rule 5 makes that the normal case. `fields` adds a field or replaces one.
    """
    body: dict[str, Any] = {
        "name": family,
        "kind": kind,
        "description": f"The {family} family of the process suite.",
        "model": {"router": MODEL_ALIAS, "budget_usd_per_day": BUDGET_USD},
        "egress": [],
    }
    body.update(fields)

    return body


def write_family_file(tree: Tree, body: Mapping[str, Any]) -> None:
    """Put one `family.yaml` in the registry, whole, by rename.

    This is the one action of invariant 9: an edit to one registry file.
    """
    text = yaml.safe_dump(dict(body), sort_keys=False)

    write_registry_file(tree, tree.family_file(str(body["name"])), text)


def write_family_prose(tree: Tree, family: str = FAMILY, text: str = INSTRUCTIONS) -> None:
    """Put the `instructions.md` of one family in the registry (contract 01 §1)."""
    write_registry_file(tree, tree.registry_family_dir(family) / "instructions.md", text)


def publish_family(tree: Tree, body: Mapping[str, Any], prose: str = INSTRUCTIONS) -> None:
    """Add one family to the registry: its prose first, then its file."""
    write_family_prose(tree, str(body["name"]), prose)
    write_family_file(tree, body)


def write_skill(tree: Tree, skill: str, text: str) -> None:
    """Put one skill in the registry, as `skills/<name>/SKILL.md` (contract 01 §3.10)."""
    write_registry_file(tree, tree.registry_root / "skills" / skill / "SKILL.md", text)


def remove_family(tree: Tree, family: str) -> None:
    """Take one family out of the registry: its directory is gone in one rename."""
    moved = tree.registry_root / f".removed-{family}"
    tree.registry_family_dir(family).rename(moved)
    shutil.rmtree(moved)


def write_registry_file(tree: Tree, path: Path, text: str) -> None:
    """Replace one registry file by rename.

    The temporary file is in the root of the registry, outside `families/`.
    So a reader of a family directory finds the old file or the new one, and
    no third file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = tree.registry_root / f".{path.name}.tmp"
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def add_family(tree: Tree, family: str, kind: str) -> None:
    """Publish one family with one ready sandbox, as `caregiver` does.

    A thin family gets the default job limit of contract 01 §3.12.
    """
    mounts = tree.mounts(family)

    for directory in (mounts.sessions, mounts.creds, mounts.config, mounts.control):
        directory.mkdir(parents=True, exist_ok=True)

    write_playpen_env(tree, family)
    write_creds(tree, family)
    write_runtime(tree, family)
    write_instructions(tree, family)
    write_status(tree, family, kind, job_timeout_s=JOB_TIMEOUT_S if kind == THIN else None)


def write_status(
    tree: Tree,
    family: str = FAMILY,
    kind: str = ATTENDED,
    *,
    job_timeout_s: int | None = None,
    sandboxes: tuple[tuple[str, str], ...] | None = None,
    config_rev: str = CONFIG_REV,
    validity: Validity = Validity.VALID,
) -> None:
    """One whole status document (contract 05 §2.1, §4.1, §9).

    Every field of §2.1 is present, so a reader that refuses a document with
    a field missing still accepts this one. `pep.watch` is `off`: nothing in
    this suite runs the watch of `caregiver`, and `off` means nobody asked
    (§2.2 rule 1).

    `job_timeout_s` is the one limit a scenario moves. Only a thin family
    has one (§9). `sandboxes` is every row as an id and a state of §4.2, for
    a scenario that plays `caregiver` during a replacement. The default is
    the one ready sandbox of the family. `config_rev` moves when the config
    mount changes (contract 01 §6.1). `validity` sets `state` and the
    validation block together (§3.1, §3.2).
    """
    now = _rfc3339()
    rows = sandboxes if sandboxes is not None else ((first_sandbox(family), "ready"),)
    valid = validity is Validity.VALID
    document: dict[str, Any] = {
        "family": family,
        "kind": kind,
        "state": "in_sync" if valid else "invalid",
        "written_at": now,
        "registry_rev": CONFIG_REV,
        "applied_rev": CONFIG_REV,
        "config_rev": config_rev,
        "validation": {
            "rev": CONFIG_REV,
            "checked_at": now,
            "ok": valid,
            "never_valid": validity is Validity.NEVER_VALID,
            "error_count": 0 if valid else 1,
            "warning_count": 0,
            "report_path": str(tree.family_dir(family) / "validation.json"),
            "first_error": None if valid else "kind: not a family kind",
        },
        "faults": [],
        "reconcile": None,
        "sandboxes": [_sandbox_row(tree, family, box, state, now) for box, state in rows],
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
        "limits": {
            "max_running_turns": None,
            "max_queued_turns": None,
            "job_timeout_s": job_timeout_s,
        },
        "triggers": {"webhooks": [], "enqueue": False},
        "pep": {"watch": "off", "url": "", "checked_at": None, "unreachable_since": None},
    }

    _atomic_write(tree.status_file(family), json.dumps(document) + "\n", STATUS_MODE)


def write_playpen_env(tree: Tree, family: str = FAMILY, sandbox: str | None = None) -> None:
    """The file `sbx exec --env-file` reads (contract 03 §7.1), for one sandbox.

    Four lines, each a host path or an identifier, and no secret. The control
    directory of the sandbox is made too, as a create leaves it: empty.
    """
    box = sandbox or first_sandbox(family)
    mounts = tree.mounts(family, box)
    mounts.control.mkdir(parents=True, exist_ok=True)
    lines = [
        f"AGENT_CRED_DIR={mounts.creds}",
        f"AGENT_FAMILY_CONFIG_DIR={mounts.config}",
        f"AGENT_CONTROL_DIR={mounts.control}",
        f"AGENT_SANDBOX={box}",
    ]

    _atomic_write(tree.playpen_env(family, box), "\n".join(lines) + "\n", PLAYPEN_ENV_MODE)


def write_creds(tree: Tree, family: str = FAMILY) -> None:
    """The credential file the playpen reads at each pi start (contract 03 §12)."""
    document = {
        "epoch": CRED_EPOCH,
        "litellm_key": FIXTURE_LITELLM_KEY,
        "pep_token": pep_token_of(family),
        "written_at": _rfc3339(),
    }

    _atomic_write(
        tree.mounts(family).creds / "creds.json", json.dumps(document) + "\n", SECRET_MODE
    )


def write_runtime(tree: Tree, family: str = FAMILY) -> None:
    """`runtime.json` of the family config mount (contract 01 §6.1)."""
    document: dict[str, object] = {"shell": False, "sandbox_tools": [], "model_alias": MODEL_ALIAS}

    _atomic_write(
        tree.mounts(family).config / "runtime.json", json.dumps(document) + "\n", STATUS_MODE
    )


def write_instructions(tree: Tree, family: str = FAMILY, text: str = INSTRUCTIONS) -> None:
    """`instructions.md` of the family config mount (contract 01 §6.1).

    It reaches pi as a path on the command line, never as text
    (contract 03 §7.1).
    """
    _atomic_write(tree.mounts(family).config / "instructions.md", text, STATUS_MODE)


def write_grants(tree: Tree, family: str, *, rev: str, delegates: tuple[str, ...]) -> None:
    """The grant file of one family (contract 04 §1.2), by temp file and rename.

    It holds the digest of the family token and never the token (§2.2). No
    MCP server and no verb is granted: `delegates` is the one reach a
    scenario of this suite needs.
    """
    digest = hashlib.sha256(pep_token_of(family).encode("utf-8")).hexdigest()
    document: dict[str, Any] = {
        "version": GRANT_VERSION,
        "family": family,
        "rev": rev,
        "token_sha256": [digest],
        "model_alias": MODEL_ALIAS,
        "tools": {},
        "verbs": {},
        "delegates": list(delegates),
        "approval": [],
        "limits": dict(GRANT_LIMITS),
    }

    _atomic_write(tree.grant_file(family), json.dumps(document) + "\n", GRANT_MODE)


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
        "supervisor_env": str(tree.playpen_env(family, sandbox)),
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
