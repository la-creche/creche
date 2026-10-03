"""The per-family status document (contract 05 section 2).

`managerd` is its only writer. One document per family, at
`families/<family>/status.json`, rewritten atomically and at least every
30 seconds.

The process writes it from several threads: one family's pass runs beside
another's, and the loop restamps the document of a family
whose own pass is stuck inside a slow step. So "one writer" means one
writer per document, and `_lock_for` is that rule. Without it a restamp's
read-modify-write could lose the document the pass wrote between the two
halves, and publish `reconciling` over an `in_sync` that was already
true."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, cast

from agent_family import FamilyFile, FamilyState, Report

from .atomic import atomic_write
from .clock import now_rfc3339 as now_rfc3339
from .faults import FaultEntry
from .pep_watch import PepReport, unwatched
from .webhook_tokens import WebhookToken

STATUS_FILE_MODE: Final = 0o644
VALIDATION_FILE_MODE: Final = 0o644

#: Contract 05 section 2 rule 5: past this, a reader treats the family as
#: `unknown` rather than trusting a stale document as current.
STALE_AFTER_S: Final = 90

#: Contract 02 section 13 rule 4: `attendance`'s own constant, not a
#: per-family setting read from `family.yaml`. `managerd` only mirrors it
#: into the status view (contract 05 section 2.1).
ATTENDANCE_MAX_QUEUED_TURNS: Final = 100


class SandboxLifecycle(StrEnum):
    """Contract 05 section 4.2."""

    PLANNED = "planned"
    CREATING = "creating"
    READY = "ready"
    DRAINING = "draining"
    STOPPING = "stopping"
    GONE = "gone"
    FAILED = "failed"


class SandboxPower(StrEnum):
    RUNNING = "running"
    STOPPED = "stopped"


class ChannelState(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    LOST = "lost"


class RotationState(StrEnum):
    SETTLED = "settled"
    ROTATING = "rotating"
    FAILED = "failed"


@dataclass(frozen=True)
class ValidationBlock:
    """Contract 05 section 3.2. The report itself lives at `report_path`;
    this block is what a reader checks before opening a second file."""

    rev: str
    checked_at: str
    ok: bool
    never_valid: bool
    error_count: int
    warning_count: int
    report_path: str
    first_error: str | None

    def as_json(self) -> dict[str, Any]:
        return {
            "rev": self.rev,
            "checked_at": self.checked_at,
            "ok": self.ok,
            "never_valid": self.never_valid,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "report_path": self.report_path,
            "first_error": self.first_error,
        }


@dataclass(frozen=True)
class SandboxStatus:
    """Contract 05 section 4.1."""

    id: str
    state: SandboxLifecycle
    power: SandboxPower
    image: str
    spec_hash: str
    cpus: int
    memory: str
    created_at: str
    ready_at: str | None
    channel: ChannelState
    #: Contract 05 section 4.1: the host path of this sandbox's
    #: `supervisor.env`. `attendance` hands it to `sbx exec --env-file`, which
    #: is the only way the playpen learns where its mounts are
    #: (contract 03 section 7.1).
    playpen_env: str = ""

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "state": str(self.state),
            "power": str(self.power),
            "image": self.image,
            "spec_hash": self.spec_hash,
            "cpus": self.cpus,
            "memory": self.memory,
            "created_at": self.created_at,
            "ready_at": self.ready_at,
            "channel": str(self.channel),
            "supervisor_env": self.playpen_env,
        }


@dataclass(frozen=True)
class CredentialsBlock:
    """Contract 05 section 6.1. Ids and an epoch only -- never a value."""

    epoch: int
    key_id: str
    token_id: str
    rotated_at: str
    next_rotation_at: str | None
    rotation_state: RotationState

    def as_json(self) -> dict[str, Any]:
        return {
            "epoch": self.epoch,
            "key_id": self.key_id,
            "token_id": self.token_id,
            "rotated_at": self.rotated_at,
            "next_rotation_at": self.next_rotation_at,
            "rotation_state": str(self.rotation_state),
        }


@dataclass(frozen=True)
class TriggersBlock:
    """Contract 05 section 2.1 and section 6.4. One row per declared
    webhook, carrying where its bearer lives and never what it is: this
    document is mode 0644, and the operator needs the PATH to paste into Home
    Assistant."""

    webhooks: tuple[WebhookToken, ...] = ()
    #: Contract 01 §3.13's dispatch form. `attendance` never reads a family
    #: file, so this is how it learns that another family's `enqueue` verb
    #: may start a job here (contract 02 §13.4.1 rule 3). Written either way:
    #: `false` says no, and a missing key says this `managerd` is too old.
    enqueue: bool = False

    def as_json(self) -> dict[str, Any]:
        return {
            "webhooks": [one.as_json() for one in self.webhooks],
            "enqueue": self.enqueue,
        }


def accepts_dispatch(family: FamilyFile) -> bool:
    """True when the family file declares an `enqueue: true` trigger."""
    return any(trigger.enqueue is True for trigger in family.triggers or ())


@dataclass(frozen=True)
class LimitsBlock:
    """Contract 05 section 2.1. `max_running_turns` and `job_timeout_s`
    come from `family.yaml`; `max_queued_turns` mirrors `attendance`'s own
    constant instead (`ATTENDANCE_MAX_QUEUED_TURNS`)."""

    max_running_turns: int | None = None
    max_queued_turns: int | None = ATTENDANCE_MAX_QUEUED_TURNS
    job_timeout_s: int | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "max_running_turns": self.max_running_turns,
            "max_queued_turns": self.max_queued_turns,
            "job_timeout_s": self.job_timeout_s,
        }


@dataclass(frozen=True)
class StatusDocument:
    """Contract 05 section 2.1. `reconcile` and `spend` are optional:
    apply-once never sets a reconcile block (that is the watch loop's own
    state), and never reads live spend."""

    family: str
    kind: str
    state: FamilyState
    written_at: str
    registry_rev: str
    applied_rev: str
    config_rev: str
    validation: ValidationBlock
    sandboxes: tuple[SandboxStatus, ...] = ()
    credentials: CredentialsBlock | None = None
    limits: LimitsBlock = field(default_factory=LimitsBlock)
    triggers: TriggersBlock = field(default_factory=TriggersBlock)
    faults: tuple[FaultEntry, ...] = ()
    reconcile: dict[str, Any] | None = None
    spend: dict[str, Any] | None = None
    #: Contract 05 section 2.1's `pep` block. The default is `off`, which
    #: is the truth for every caller that runs one pass in one process
    #: and has no interval to probe over: `apply-once`, `reconcile-once`
    #: and the two refusals. A watch that is silently off lets a PEP outage
    #: go unnoticed, so there is no value here that means "nobody said".
    pep: PepReport = field(default_factory=unwatched)

    def as_json(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "kind": self.kind,
            "state": str(self.state),
            "written_at": self.written_at,
            "registry_rev": self.registry_rev,
            "applied_rev": self.applied_rev,
            "config_rev": self.config_rev,
            "validation": self.validation.as_json(),
            "faults": [one.as_json() for one in self.faults],
            "reconcile": self.reconcile,
            "sandboxes": [one.as_json() for one in self.sandboxes],
            "credentials": self.credentials.as_json() if self.credentials is not None else None,
            "spend": self.spend,
            "limits": self.limits.as_json(),
            "triggers": self.triggers.as_json(),
            "pep": self.pep.as_json(),
        }


#: One lock per document, made on first use. The set of families is
#: bounded by the registry, so this never grows past it.
_DOC_LOCKS: Final[dict[str, threading.Lock]] = {}
_DOC_LOCKS_MUTEX: Final = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    key = str(path)
    with _DOC_LOCKS_MUTEX:
        return _DOC_LOCKS.setdefault(key, threading.Lock())


#: The documents THIS process has published. `restamp_status` touches no
#: other. A set and not a clock: a file's mtime comes from the kernel's
#: coarse clock and can sit a few milliseconds behind `time.time()`, so "newer
#: than this process" would be wrong now and then, and only on Linux.
_PUBLISHED_HERE: set[Path] = set()
_PUBLISHED_GUARD = threading.Lock()


def write_status(path: Path, doc: StatusDocument) -> None:
    """Contract 05 section 2 rules 2-3: atomic, mode 0644. The document
    holds no secret -- credentials live in a 0600 file elsewhere
    (contract 03 section 12)."""
    with _lock_for(path):
        _write(path, doc)
        with _PUBLISHED_GUARD:
            _PUBLISHED_HERE.add(path)


def published_here(path: Path) -> bool:
    """Has THIS process published this document (`write_status`)?"""
    with _PUBLISHED_GUARD:
        return path in _PUBLISHED_HERE


def forget_published() -> None:
    """What a restart does to this module: a new process has published
    nothing. For a test that stands in for one inside a single process."""
    with _PUBLISHED_GUARD:
        _PUBLISHED_HERE.clear()


def restamp_status(path: Path, *, older_than_s: float) -> bool:
    """Rewrite the document with a new `written_at`, when nothing else has
    written it for `older_than_s` seconds. Answers whether it did.

    Contract 05 section 2 rule 4 asks for a rewrite at least every 30
    seconds, and section 2 rule 5 makes a reader call a family older than
    90 seconds `unknown`. One step of a pass -- `sbx create` -- takes
    longer than both and publishes nothing of its own while it runs. The
    content is carried over unchanged on purpose: the family IS still
    where the document says it is, and the only stale field is the time.

    It rewrites the bytes on disk rather than a parsed document, so the
    shape is whatever the pass published and no field can be lost in a
    round trip. The read and the write are one critical section, so a
    document the pass published in between is restamped, not overwritten.

    ONLY a document this process published. "Still true" is a claim this
    process can make about its own verdict and about nobody else's: a
    document from before a restart is a verdict about another commit, another
    image, or both. Restamped, the OLD reconciler's document would say
    `in_sync` and `ready` about an image nobody wants any more, under a fresh
    `written_at`, and `rework-cutover.sh up` reads exactly those three things
    to call the fleet converged. Such a document stays as old as it
    is, a reader calls it `unknown` after 90 seconds, and that is the truth:
    nobody who is running has looked."""
    if not published_here(path):
        return False

    with _lock_for(path):
        body = _read_json(path)
        if body is None or _age_s(path) < older_than_s:
            return False

        body["written_at"] = now_rfc3339()
        _write_json(path, body)
        return True


def _write(path: Path, doc: StatusDocument) -> None:
    _write_json(path, doc.as_json())


def _write_json(path: Path, body: dict[str, Any]) -> None:
    atomic_write(path, json.dumps(body, indent=2).encode("utf-8") + b"\n", mode=STATUS_FILE_MODE)


def _age_s(path: Path) -> float:
    """How long since the document was last written, by the filesystem's
    own clock. `written_at` counts whole seconds, which is too coarse to
    order two writes made inside one."""
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return 0.0


def _read_json(path: Path) -> dict[str, Any] | None:
    """The document as it stands. A document this process cannot read is
    one it must not rewrite: the next pass publishes a whole new one."""
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    if not isinstance(body, dict):
        return None

    return cast("dict[str, Any]", body)


def write_validation_report(path: Path, report: Report) -> None:
    """The full report `validation.report_path` points at (contract 05
    section 3.2). Contract 01 section 7 defines its shape; `agent_family`
    already builds it, this only publishes it at the path the status
    document names."""
    body = json.dumps(report.as_json(), indent=2).encode("utf-8") + b"\n"
    atomic_write(path, body, mode=VALIDATION_FILE_MODE)
