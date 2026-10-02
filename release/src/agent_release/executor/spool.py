"""The release spool as ROOT reads it (`stage7-releases.md` §2.2, §3.2).

    /var/lib/agent-release/releases/   (`layout.SPOOL_ROOT`)
      requests/<ULID>.json   written by operator-side code   <- HOSTILE INPUT
      running/<ULID>.json    written by root: the validated request
      running/<ULID>-<component>.switch
                             written by root: what a crash must repair
      running/<ULID>-releasectl.self
                             written by root: the executor's own switch,
                             still to come after the ledger (contract 06
                             §1.1 rule 2)
      done/<ULID>.json       written by root: the ledger
      done/<ULID>.log        the whole log
      rejected/              what root would not parse, renamed here whole
      lock                   flock, root:root 0600; one release at a time

Everything under `requests/` is written outside root, so anything in it may
be a symlink, a FIFO, a directory, a hard link or a file that changes under
us. The rules, measured and proved on the host on 2026-09-17:

- the spool root and each subdirectory are opened ONCE, `O_NOFOLLOW`, and
  every later operation is relative to those descriptors;
- a request is opened `O_NOFOLLOW | O_NONBLOCK`, must be a regular file owned
  by one of the uids root accepts and at most `MAX_REQUEST_BYTES`;
- root never copies request bytes: `running/` and `done/` are re-serialized
  from the validated fields;
- `requests/` is ALWAYS drained. `agent-rework-release.path` is a
  `PathExistsGlob`: one `*.json` left behind re-fires the unit until
  systemd's start limit.

Two more come from §2.2.

1. **The lock is `root:root 0600`.** An operator-owned lock would let operator-side
   code park root. Root-owned, it cannot be taken by anything but the
   executor (§6 row 12).
2. **The switch journal.** A crash between switch and verify would otherwise
   leave a swapped tree nobody records. Root writes what it is about to swap
   BEFORE it swaps it, so the next run can put it back (§2.4 step 10).

Stdlib only, every import at module top: a release re-syncs the venv under
the running executor (contract 06 §1.1).
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, cast

from ..errors import Refusal, RefusalCode
from . import layout
from .request import MAX_REQUEST_BYTES, REQUEST_SUFFIX, Request, parse_request

#: Under root's own root, so no directory the operator owns sits above it
#: (`layout.py`).
SPOOL_ROOT: Final = layout.SPOOL_ROOT

REQUESTS_DIR: Final = "requests"
RUNNING_DIR: Final = "running"
DONE_DIR: Final = "done"
REJECTED_DIR: Final = "rejected"
LOCK_NAME: Final = "lock"

LOG_SUFFIX: Final = ".log"
SWITCH_SUFFIX: Final = ".switch"
SELF_SUFFIX: Final = ".self"
TMP_PREFIX: Final = "."
TMP_SUFFIX: Final = ".tmp"

#: A ledger entry carries the whole resolved manifest and a 200 line tail.
MAX_RECORD_BYTES: Final = 1 << 20

#: `done/` is readable by the group so the one view can read an outcome
#: (§2.6). It informs and never decides.
LEDGER_FILE_MODE: Final = 0o640

#: §3.2 rule 8. Extras are ledgered `refused`, check `rate`. The path unit
#: still drains them, so systemd's start limit is never reached.
MAX_PENDING_PER_REQUESTER: Final = 8

#: §3.1: the operator, the `agent-control` family and CI. "No other path exists."
MAX_REQUESTERS: Final = 3

#: The backstop §3.2 rule 8 needs. Its own cap counts per `requested_by`,
#: which is a field the REQUESTER writes, so one writer defeats it with one
#: new name per file. This caps the whole pass instead, at what the three
#: real requesters could legitimately have pending. Past it root neither
#: parses nor ledgers: it empties the glob with one rename per entry, so a
#: flood cannot grow `done/`.
MAX_REQUESTS_PER_PASS: Final = MAX_PENDING_PER_REQUESTER * MAX_REQUESTERS

#: The only part of a hostile file name a quarantined entry may keep.
_SAFE_NAME_RE_CHARS: Final = frozenset("abcdefghijklmnopqrstuvwxyz0123456789._-")
UNSAFE_NAME: Final = "unsafe-name"
MAX_KEPT_NAME_CHARS: Final = 64

#: How far `quarantine` looks for a free name before it stops looking. A
#: pass quarantines at most `MAX_REQUESTS_PER_PASS` entries, so this is
#: already an order of magnitude past what one run can need.
MAX_REJECTED_SEQ: Final = 9999

_DIR_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_CREATE_FLAGS: Final = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
_LOCK_FLAGS: Final = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


class SpoolError(Exception):
    """The spool itself is unusable: a missing directory, a planted file
    where root must write. The message is for the journal, not the ledger."""


class NotAFile(Exception):
    """Not a plain file the operator wrote: a symlink, a FIFO, a directory, another
    uid's file. Quarantined whole, never ledgered — there is nothing to
    read, so there is no id to ledger it under."""


@dataclass(frozen=True)
class VerifyHook:
    """Contract 06 §4's three fields, carried in the switch note.

    The repair has to run the PREVIOUS version's hook, and it holds no
    manifest: the run that resolved one crashed. Root wrote this note from
    its own resolved manifest before the swap, so it is root's fact and not
    a re-read of anything the operator owns.
    """

    command: tuple[str, ...]
    user: str
    timeout_s: int

    def as_dict(self) -> dict[str, object]:
        return {"command": list(self.command), "user": self.user, "timeout_s": self.timeout_s}


@dataclass(frozen=True)
class SwitchNote:
    """What one switch did, written before it happened (§2.4 step 10).

    A crash after the move and before the verify leaves this file behind.
    The next run reads it, puts `prev` back and ledgers the repair.
    """

    component: str
    to: str
    prev: str
    unit: str | None
    #: None for a note that recorded no hook. The repair then
    #: restores and restarts, and says under `manual` that it could not
    #: verify — it never guesses a command.
    verify: VerifyHook | None = None
    #: Where this switch kept the unit file it replaces (`install.py` rule
    #: 6), e.g. `/etc/systemd/system/agent-pep.service.prev`. None when it
    #: replaces none: the manifest names no unit, none is installed, or the
    #: release carries the file already there. The repair puts back exactly
    #: this, and no unit at all when it is None.
    unit_kept: str | None = None
    #: Where this switch kept each sibling unit it replaces (`install.py`
    #: rule 8). The repair puts back exactly these. A note an older
    #: executor wrote has no such key, and reads as none.
    siblings_kept: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "component": self.component,
            "to": self.to,
            "prev": self.prev,
            "unit": self.unit,
            "verify": self.verify.as_dict() if self.verify else None,
            "unit_kept": self.unit_kept,
            "siblings_kept": list(self.siblings_kept),
        }


@dataclass(frozen=True)
class SelfNote:
    """`releasectl`'s own switch, written at step 9 and acted on after the
    ledger entry and the lock (contract 06 §1.1 rule 2).

    It carries what the swap and rule 3's hook need, because the process
    that settles a crashed one holds no manifest. `to_version` is how that
    process tells whether the swap happened: the staged tree carries it as
    its stamp, and the live one does not until the swap.
    """

    component: str
    to: str
    prev: str
    to_version: str
    from_version: str | None
    requested_by: str
    verify: VerifyHook

    def as_dict(self) -> dict[str, object]:
        return {
            "component": self.component,
            "to": self.to,
            "prev": self.prev,
            "to_version": self.to_version,
            "from_version": self.from_version,
            "requested_by": self.requested_by,
            "verify": self.verify.as_dict(),
        }


def note_name(request_id: str, component: str) -> str:
    """What one switch note is called, built here and nowhere else.

    One run writes one note per component, so the name carries both. A
    request id is a ULID and holds no hyphen (§3.2), so the component can
    be taken off the end again — which is what `unfinished` does, because
    a repair must ask `ledgered` about the RUN and not about the note.
    """
    return f"{request_id}-{component}"


@dataclass(frozen=True)
class Unfinished:
    """One note `running/` still holds, with both ids it is keyed by.

    `name` is what clears it and what a repair's own ledger entry is filed
    under. `request_id` is the run that wrote it: the id whose entry, if
    it exists, means that run decided this switch itself.
    """

    name: str
    request_id: str
    note: SwitchNote


@dataclass(frozen=True)
class UnfinishedSelf:
    """One self note `running/` still holds. `name` is what clears it and
    what its entry is filed under, `request_id` the release that wrote it."""

    name: str
    request_id: str
    note: SelfNote


def _read_hook(value: object) -> VerifyHook | None:
    """The `verify` object out of a switch note root itself wrote.

    Root owns `running/`, so this is not hostile input. It is still read
    defensively, because a note an older executor wrote has no `verify` key
    at all and a half-written one must not raise inside a repair.
    """
    if not isinstance(value, dict):
        return None

    fields = cast("dict[str, object]", value)
    command, user, timeout = fields.get("command"), fields.get("user"), fields.get("timeout_s")
    if not isinstance(command, list) or not isinstance(user, str) or not isinstance(timeout, int):
        return None

    words = cast("list[object]", command)
    if not words or not all(isinstance(one, str) for one in words):
        return None

    return VerifyHook(tuple(cast("list[str]", words)), user, timeout)


def _read_strings(value: object) -> tuple[str, ...]:
    """A list of strings out of a note root wrote, read as `_read_hook`
    reads: a missing key or a non-list is none, and only strings are kept."""
    if not isinstance(value, list):
        return ()

    items = cast("list[object]", value)

    return tuple(one for one in items if isinstance(one, str))


def _safe_name(name: str) -> str:
    if len(name) > MAX_KEPT_NAME_CHARS or not name:
        return UNSAFE_NAME

    if not set(name) <= _SAFE_NAME_RE_CHARS:
        return UNSAFE_NAME

    return name


def open_dir(path: str, dir_fd: int | None = None) -> int:
    try:
        return os.open(path, _DIR_FLAGS, dir_fd=dir_fd)
    except OSError as exc:
        raise SpoolError(f"{path}: not a directory root can open ({exc.strerror})") from None


def read_capped(dir_fd: int, name: str, cap: int, owner_uids: frozenset[int] | None) -> bytes:
    """One file's bytes out of a hostile directory.

    `O_NOFOLLOW` refuses a symlink, `O_NONBLOCK` keeps a FIFO from hanging
    root forever, and `fstat` refuses everything that is not a plain file.

    `owner_uids` is a SET, not one uid: §3.1 names three requesters, and one
    of them is the PEP's `release` verb, which runs as `pep`. Root still
    decides nothing on what the file SAYS — the set is root's own
    configuration, and it is what keeps a fourth account from filing.
    """
    try:
        fd = os.open(name, _READ_FLAGS, dir_fd=dir_fd)
    except OSError:
        raise NotAFile("cannot be opened as a plain file") from None

    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise NotAFile("is not a regular file")

        if owner_uids is not None and info.st_uid not in owner_uids:
            raise NotAFile("is not owned by a requester root accepts")

        if info.st_size > cap:
            raise Refusal(RefusalCode.REQUEST, "request", f"is larger than {cap} bytes")

        # To EOF, but never past cap + 1: a file that grew after the fstat is
        # still caught, and one read() may return less than it was asked for.
        raw = b""
        while len(raw) <= cap and (chunk := os.read(fd, cap + 1 - len(raw))):
            raw += chunk
    except OSError:
        raise Refusal(RefusalCode.REQUEST, "request", "cannot be read") from None
    finally:
        os.close(fd)

    if len(raw) > cap:
        raise Refusal(RefusalCode.REQUEST, "request", f"is larger than {cap} bytes")

    return raw


def _encode(data: dict[str, object]) -> bytes:
    return (json.dumps(data, indent=2, sort_keys=False) + "\n").encode("utf-8")


def _write_new(dir_fd: int, name: str, payload: bytes) -> None:
    """Create `name`, never replace it.

    Written under a dot-name first so a reader sees a whole file or none, and
    `link` rather than `rename` because rename would silently replace a
    planted `name`.
    """
    tmp = f"{TMP_PREFIX}{name}{TMP_SUFFIX}"
    _unlink_quiet(dir_fd, tmp)
    try:
        fd = os.open(tmp, _CREATE_FLAGS, LEDGER_FILE_MODE, dir_fd=dir_fd)
    except OSError as exc:
        raise SpoolError(f"cannot create {tmp}: {exc.strerror}") from None

    try:
        # The unit's UMask must not narrow what the group has to read.
        os.fchmod(fd, LEDGER_FILE_MODE)
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)

    try:
        os.link(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except OSError as exc:
        raise SpoolError(f"cannot create {name}: {exc.strerror}") from None
    finally:
        _unlink_quiet(dir_fd, tmp)


def _unlink_quiet(dir_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno != errno.ENOENT:
            raise


def _exists(dir_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return False

    return True


class Spool:
    """The five descriptors and every operation root performs through them."""

    def __init__(
        self, root: str, owner_uid: int, *, also_owned_by: frozenset[int] = frozenset()
    ) -> None:
        #: §3.1's requesters that have a host account: the operator, who types
        #: `agent-releasectl request`, and `pep`, whose `release` verb files
        #: the same bytes. CI files through neither — it has no account here.
        #: Everything else is quarantined unread.
        self.owner_uids = frozenset({owner_uid}) | also_owned_by
        self._rejected_seq = 0
        self.root_fd = open_dir(root)
        self.requests_fd = open_dir(REQUESTS_DIR, self.root_fd)
        self.running_fd = open_dir(RUNNING_DIR, self.root_fd)
        self.done_fd = open_dir(DONE_DIR, self.root_fd)
        self.rejected_fd = open_dir(REJECTED_DIR, self.root_fd)

    def close(self) -> None:
        for fd in (
            self.rejected_fd,
            self.done_fd,
            self.running_fd,
            self.requests_fd,
            self.root_fd,
        ):
            os.close(fd)

    def pending(self) -> list[str]:
        """Every `*.json` name in `requests/`, oldest id first.

        A writer's in-flight `.<id>.tmp` does not end in `.json`, so it is
        left alone — and does not match the path unit's glob either.
        """
        return sorted(n for n in os.listdir(self.requests_fd) if n.endswith(REQUEST_SUFFIX))

    def seen(self, request_id: str) -> bool:
        """An id is spent once it reaches `running/` or `done/`: a replay
        must never overwrite a ledger entry (§6 row 5)."""
        name = request_id + REQUEST_SUFFIX

        return _exists(self.running_fd, name) or _exists(self.done_fd, name)

    def read_request(self, name: str, request_id: str) -> Request:
        raw = read_capped(self.requests_fd, name, MAX_REQUEST_BYTES, self.owner_uids)

        return parse_request(raw, request_id)

    def quarantine(self, name: str, stamp: int) -> str:
        """Move an entry root will not parse out of `requests/`.

        `rename` never follows a symlink and moves a directory whole, so
        nothing hostile is ever traversed. The new name is root's; the old
        one survives only when it is harmless.
        """
        target = self._free_rejected_name(stamp, _safe_name(name))
        try:
            os.rename(name, target, src_dir_fd=self.requests_fd, dst_dir_fd=self.rejected_fd)
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                return target

            raise SpoolError(f"cannot drain requests/: {exc.strerror}") from None

        return target

    def _free_rejected_name(self, stamp: int, safe: str) -> str:
        """A name nothing in `rejected/` already holds.

        `rename` replaces its target, `_rejected_seq` resets
        per run and `stamp` is a whole second, so two runs in one second
        would overwrite each other's evidence. `rejected/` is root-owned and root
        is single-threaded here, so looking before renaming is enough.
        """
        while self._rejected_seq < MAX_REJECTED_SEQ:
            self._rejected_seq += 1
            target = f"{stamp}-{self._rejected_seq:04d}-{safe}"
            if not _exists(self.rejected_fd, target):
                return target

        return f"{stamp}-{MAX_REJECTED_SEQ:04d}-{safe}"

    def start(self, request: Request) -> None:
        """`requests/` to `running/`: root writes what it validated, then
        removes the original. It never reads the operator-owned file twice."""
        name = request.id + REQUEST_SUFFIX
        _write_new(self.running_fd, name, _encode(request.as_dict()))
        _unlink_quiet(self.requests_fd, name)

    def drop_request(self, name: str) -> None:
        _unlink_quiet(self.requests_fd, name)

    def note_switch(self, request_id: str, note: SwitchNote) -> None:
        """Before the move, not after: a crash in between is repairable only
        if the record already exists."""
        name = note_name(request_id, note.component) + SWITCH_SUFFIX
        _unlink_quiet(self.running_fd, name)
        _write_new(self.running_fd, name, _encode(note.as_dict()))

    def clear_switch(self, name: str) -> None:
        """One note, by the name `note_name` gave it."""
        _unlink_quiet(self.running_fd, name + SWITCH_SUFFIX)

    def note_self(self, request_id: str, note: SelfNote) -> None:
        """At step 9, under the lock. A visit that takes the lock after this
        run releases it finds the note and refuses to rebuild the tree."""
        name = note_name(request_id, note.component) + SELF_SUFFIX
        _unlink_quiet(self.running_fd, name)
        _write_new(self.running_fd, name, _encode(note.as_dict()))

    def clear_self(self, name: str) -> None:
        _unlink_quiet(self.running_fd, name + SELF_SUFFIX)

    def unfinished_self(self) -> list[UnfinishedSelf]:
        """Every self note a run wrote and never settled, oldest id first."""
        found: list[UnfinishedSelf] = []
        for name in sorted(os.listdir(self.running_fd)):
            if not name.endswith(SELF_SUFFIX) or name.startswith(TMP_PREFIX):
                continue

            note = self._read_self(name)
            if note is None:
                continue

            stem = name.removesuffix(SELF_SUFFIX)
            found.append(UnfinishedSelf(stem, stem.removesuffix(f"-{note.component}"), note))

        return found

    def _read_self(self, name: str) -> SelfNote | None:
        """Root wrote it, so it is a fact. Read defensively all the same: a
        half-written note must not raise inside the run that settles it."""
        fields = self._read_note(name)
        if fields is None:
            return None

        texts = [fields.get(key) for key in ("component", "to", "prev", "to_version")]
        by, was = fields.get("requested_by"), fields.get("from_version")
        hook = _read_hook(fields.get("verify"))
        if not all(isinstance(one, str) for one in texts) or not isinstance(by, str):
            return None

        if hook is None:
            return None

        component, to, prev, to_version = cast("list[str]", texts)

        return SelfNote(
            component, to, prev, to_version, was if isinstance(was, str) else None, by, hook
        )

    def _read_note(self, name: str) -> dict[str, object] | None:
        try:
            raw = read_capped(self.running_fd, name, MAX_RECORD_BYTES, None)
        except (NotAFile, Refusal):
            return None

        try:
            body: object = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None

        if not isinstance(body, dict):
            return None

        return {str(key): value for key, value in cast("dict[object, object]", body).items()}

    def unfinished(self) -> list[Unfinished]:
        """Every switch a previous run recorded and never cleared, oldest
        id first. `running/` is root-owned, so this is a fact, not a hint."""
        found: list[Unfinished] = []
        for name in sorted(os.listdir(self.running_fd)):
            if not name.endswith(SWITCH_SUFFIX) or name.startswith(TMP_PREFIX):
                continue

            note = self._read_switch(name)
            if note is None:
                continue

            # `removesuffix` leaves a name that does not end in its own
            # component alone, so a note named `<id>.switch` keeps its
            # whole stem as the id.
            stem = name.removesuffix(SWITCH_SUFFIX)
            found.append(Unfinished(stem, stem.removesuffix(f"-{note.component}"), note))

        return found

    def _read_switch(self, name: str) -> SwitchNote | None:
        fields = self._read_note(name)
        if fields is None:
            return None

        component, to, prev = fields.get("component"), fields.get("to"), fields.get("prev")
        unit, kept = fields.get("unit"), fields.get("unit_kept")
        if not isinstance(component, str) or not isinstance(to, str) or not isinstance(prev, str):
            return None

        return SwitchNote(
            component,
            to,
            prev,
            unit if isinstance(unit, str) else None,
            _read_hook(fields.get("verify")),
            kept if isinstance(kept, str) else None,
            _read_strings(fields.get("siblings_kept")),
        )

    def ledgered(self, request_id: str) -> bool:
        """Whether `done/` already holds this id's ENTRY.

        `seen` answers a wider question — it counts `running/` too — and a
        crash leaves `running/<id>.json` behind, so a repair would read
        every unfinished request as already spent. This one is the ledger
        alone, which is what a repair must not overwrite.
        """
        return _exists(self.done_fd, request_id + REQUEST_SUFFIX)

    def finish(self, request_id: str, entry: dict[str, object], log_lines: list[str]) -> None:
        """`running/` to `done/`: the ledger entry, the whole log beside it."""
        name = request_id + REQUEST_SUFFIX
        log_text = "".join(f"{line}\n" for line in log_lines)
        # `finish` writes the log first. A crash between the
        # two calls left a log with no entry, and `_write_new` then refused
        # it on the next run, which exited 1 without draining and re-fired
        # the path unit into its start limit. Root's own half-written log is
        # root's to replace — the ENTRY is the record, and it is absent.
        if not self.ledgered(request_id):
            _unlink_quiet(self.done_fd, request_id + LOG_SUFFIX)

        _write_new(self.done_fd, request_id + LOG_SUFFIX, log_text.encode("utf-8"))
        _write_new(self.done_fd, name, _encode(entry))
        _unlink_quiet(self.running_fd, name)
        self._clear_notes_of(request_id)

    def _clear_notes_of(self, request_id: str) -> None:
        """Every note this id wrote, AFTER its entry is on disk.

        A note is there to survive a CRASH between the swap and the verify
        (§2.4 row 10). A run that recorded itself is not a crash, whatever
        its outcome, and one left behind is read by the next run as one:
        `running/` is root-only, so operator-side tooling cannot even see it,
        and `bin/rework-release-visit.sh --rebuild-executor` refuses with
        "an unfinished switch is in .../running" over a release root has
        already decided and ledgered.

        It runs after both `_write_new` calls on purpose. A `finish` that
        cannot write the entry has not recorded anything, and the notes
        must outlive it.
        """
        self.clear_switch(request_id)
        # The self switch's own entry is filed under the note's name, so
        # writing it spends the note. The RELEASE's entry does not: its
        # note is what the switch after it reads (contract 06 §1.1 rule 2).
        self.clear_self(request_id)
        for name in os.listdir(self.running_fd):
            if name.startswith(f"{request_id}-") and name.endswith(SWITCH_SUFFIX):
                _unlink_quiet(self.running_fd, name)

    def open_lock(self) -> int:
        try:
            fd = os.open(LOCK_NAME, _LOCK_FLAGS, dir_fd=self.root_fd)
        except OSError as exc:
            raise SpoolError(f"{LOCK_NAME}: {exc.strerror} (the setup script creates it)") from None

        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise SpoolError(f"{LOCK_NAME} is not a regular file")

        return fd


def acquire(
    fd: int,
    wait_s: float,
    poll_s: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> float | None:
    """Exclusive flock, polled. Seconds waited, or None when `wait_s` ran
    out. Two requests therefore run one after the other (§2.4 step 6)."""
    started = clock()
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

            return clock() - started
        except BlockingIOError:
            pass

        if clock() - started >= wait_s:
            return None

        sleep(poll_s)


def release_lock(fd: int) -> None:
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)
