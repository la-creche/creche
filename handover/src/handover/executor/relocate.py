"""The one-time move into root's own root (`layout.py`).

`bin/rework-release-visit.sh` runs this ONCE, as root, after it has made the
new directories and before it installs the units that point at them:

    /srv/agents/state/rework/releases/done/*   ->  /var/lib/agent-release/releases/done/
    /srv/agents/state/rework/secrets/*.enc     ->  /var/lib/agent-release/secrets/
    /srv/agents/state/rework/upstreams.yaml    ->  /var/lib/agent-release/upstreams.yaml

What it carries, and why each one:

- **The ledger.** The executor refuses a request id `done/` already holds,
  and `caregiver` reads its answers there. A fresh ledger would forget both.
- **The sealed secrets.** The operator pasted each one on the phone. Dropping them
  would re-open a gap per server and one phone push each.
- **The roster.** The PEP reads it at its next start. Without it every
  released MCP server disappears until the next `mcp-servers` release.

What it leaves: `requests/` and `running/` must be EMPTY (drain first, the
visit says how), `rejected/` is quarantine, and the old work root holds
per-request trees nobody reads again. `docs/rework/retire-old.md` removes
the old tree once nothing reads it.

Two more things, both for the minutes between this visit and `caregiver`'s
own release, which is the one writer still running the old constants:

- **It holds the OLD spool lock** while it looks and copies, so an old
  executor already past its tap finishes first.
- **It renames the old spool root to `releases.retired`.** An old
  `caregiver` finds no `requests/` there and takes the quiet branch it
  takes on a host with no release spool (`mcp_wire.mcp_pass`), instead of filing
  requests into a spool nothing drains any more.

WHY IT COPIES FILE BY FILE, and never `mv`s a directory. The old roots sit
under directories the operator owns, which is the whole finding: whatever is there
may have been swapped. So a file is carried only when it is a regular file
root wrote — owned by root, one link, not a symlink, opened `O_NOFOLLOW`
relative to a directory descriptor — and only under a name its own module
would write. The operator cannot create a root-owned file, so those checks leave
only files root wrote. A target that already exists is kept: this never
overwrites.

`.moved` records that it ran, so a later visit neither copies again nor
resurrects a file the operator removed since.

Stdlib only apart from sibling constants, every import at module top.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from ..intake.store import SECRET_FILE_MODE, SECRET_NAME_RE, SECRET_SUFFIX
from . import layout
from .request import ULID_RE
from .roster import FILE_MODE as ROSTER_FILE_MODE
from .spool import (
    DONE_DIR,
    LEDGER_FILE_MODE,
    LOCK_NAME,
    LOG_SUFFIX,
    REQUEST_SUFFIX,
    REQUESTS_DIR,
    RUNNING_DIR,
)

#: Where the four lived until ROOTS. Literal, and only here: nothing else in
#: the package may name them again.
OLD_STATE_ROOT: Final = "/srv/agents/state/rework"
OLD_SPOOL_ROOT: Final = f"{OLD_STATE_ROOT}/releases"
OLD_SECRETS_DIR: Final = f"{OLD_STATE_ROOT}/secrets"
OLD_ROSTER_NAME: Final = "upstreams.yaml"

#: What the old spool root is renamed to once its ledger is carried.
RETIRED_SUFFIX: Final = ".retired"

#: What says the move ran. In root's own root, so only root can fake it.
MARKER_NAME: Final = ".moved"
MARKER_MODE: Final = 0o644

#: The largest file carried. A ledger entry is capped at 1 MiB and a sealed
#: secret at 128 KiB, and a log is a 200-line tail: 16 MiB is far past all
#: three and still bounds one read.
MAX_MOVED_BYTES: Final = 16 << 20

_DIR_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_CREATE_FLAGS: Final = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
#: `spool.py`'s own lock flags: never create it, never follow a link to it.
_LOCK_FLAGS: Final = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC

#: `done/<ULID>.json` and `done/<ULID>.log`, the only names `spool.py` writes
#: there that are not its own dot-prefixed temporaries.
_LEDGER_NAME_RE: Final = re.compile(
    rf"(?:{ULID_RE.pattern})(?:{re.escape(REQUEST_SUFFIX)}|{re.escape(LOG_SUFFIX)})"
)
_SECRET_FILE_RE: Final = re.compile(rf"(?:{SECRET_NAME_RE.pattern}){re.escape(SECRET_SUFFIX)}")


class RelocateError(Exception):
    """The move cannot start. The message is for the operator, and names the fix."""


@dataclass
class Moved:
    """What one run did, by name relative to the old state root."""

    already: bool = False
    copied: list[str] = field(default_factory=list[str])
    kept: list[str] = field(default_factory=list[str])
    refused: list[str] = field(default_factory=list[str])
    retired: str = ""

    def lines(self) -> list[str]:
        """The visit prints these, one per line."""
        if self.already:
            return [f"already moved ({MARKER_NAME} is there); nothing copied"]

        found = [f"copied  {one}" for one in self.copied]
        found += [f"kept    {one} (already in the new root)" for one in self.kept]
        found += [f"REFUSED {one} (not a file root wrote)" for one in self.refused]
        if self.retired:
            found.append(self.retired)

        found.append(
            f"{len(self.copied)} copied, {len(self.kept)} kept, {len(self.refused)} refused"
        )

        return found


def move_old(prefix: Path, owner_uid: int) -> Moved:
    """Carry the three kinds of file, once. `prefix` is `/` on the host
    and a temp directory in a test; `owner_uid` is root's uid, 0.

    Raises `RelocateError` before it copies anything when the old spool
    still holds a request or a switch note, or when the new root is not
    there to copy into.
    """
    new_root = _under(prefix, layout.RELEASE_ROOT)
    new_fd = _open_dir(new_root)
    if new_fd is None:
        raise RelocateError(f"{layout.RELEASE_ROOT} is not a directory. The visit makes it first.")

    try:
        if _exists(new_fd, MARKER_NAME):
            return Moved(already=True)

        lock_fd = _hold_old_lock(prefix)
        try:
            moved = _carry_all(prefix, new_fd, owner_uid)
            moved.retired = _retire_old_spool(prefix)
        finally:
            if lock_fd is not None:
                os.close(lock_fd)

        _write_marker(new_fd, moved)

        return moved
    finally:
        os.close(new_fd)


def _carry_all(prefix: Path, new_fd: int, owner_uid: int) -> Moved:
    _refuse_undrained(prefix)
    moved = Moved()
    _carry_dir(
        _under(prefix, f"{OLD_SPOOL_ROOT}/{DONE_DIR}"),
        _under(prefix, f"{layout.SPOOL_ROOT}/{DONE_DIR}"),
        _LEDGER_NAME_RE,
        LEDGER_FILE_MODE,
        owner_uid,
        moved,
    )
    _carry_dir(
        _under(prefix, OLD_SECRETS_DIR),
        _under(prefix, layout.SECRETS_DIR),
        _SECRET_FILE_RE,
        SECRET_FILE_MODE,
        owner_uid,
        moved,
    )
    _carry_roster(prefix, new_fd, owner_uid, moved)

    return moved


def _hold_old_lock(prefix: Path) -> int | None:
    """The same `flock` the old executor takes at step 6, held until the
    old spool is renamed. None when there is no old lock: then no old
    executor can run either, because `Spool` will not create one."""
    shown = f"{OLD_SPOOL_ROOT}/{LOCK_NAME}"
    try:
        fd = os.open(_under(prefix, shown), _LOCK_FLAGS)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RelocateError(f"{shown} cannot be opened as a plain file ({exc.strerror}).") from exc

    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise RelocateError(f"{shown} is not a plain file. Look at it before running this again.")

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(fd)
        raise RelocateError(
            "a release holds the old spool lock. Let it finish, then run this again."
        ) from exc

    return fd


def _retire_old_spool(prefix: Path) -> str:
    """One `rename(2)`, inside the old state root. A failure is reported
    and does not undo the move: an old `caregiver` then files into a spool
    nothing drains, which it reports itself after an hour."""
    old = _under(prefix, OLD_SPOOL_ROOT)
    retired = f"{OLD_SPOOL_ROOT}{RETIRED_SUFFIX}"
    try:
        os.rename(old, _under(prefix, retired))
    except FileNotFoundError:
        return ""
    except OSError as exc:
        return f"NOT renamed {OLD_SPOOL_ROOT} to {retired} ({exc.strerror}); rename it by hand"

    return f"renamed {OLD_SPOOL_ROOT} to {retired}"


def _under(prefix: Path, absolute: str) -> Path:
    return prefix / absolute.lstrip("/")


def _refuse_undrained(prefix: Path) -> None:
    """A request left in the old `requests/` would never be drained: the
    new executor reads the new spool. A switch note left in `running/` is a
    repair only the executor that wrote it may run."""
    requests = _names(_under(prefix, f"{OLD_SPOOL_ROOT}/{REQUESTS_DIR}"))
    if requests is None or any(name.endswith(REQUEST_SUFFIX) for name in requests):
        raise RelocateError(
            f"{OLD_SPOOL_ROOT}/{REQUESTS_DIR} still holds a request. Drain the old spool first."
        )

    running = _names(_under(prefix, f"{OLD_SPOOL_ROOT}/{RUNNING_DIR}"))
    if running is None or running:
        raise RelocateError(
            f"{OLD_SPOOL_ROOT}/{RUNNING_DIR} holds an unfinished switch. Drain the old spool first."
        )


def _names(directory: Path) -> list[str] | None:
    """Entry names, none for a directory that is not there, and None for
    one this process may not list. The caller refuses on None: that is the
    direction a guess must fail in."""
    try:
        return os.listdir(directory)
    except FileNotFoundError:
        return []
    except OSError:
        return None


def _carry_dir(
    old: Path,
    new: Path,
    pattern: re.Pattern[str],
    mode: int,
    owner_uid: int,
    moved: Moved,
) -> None:
    """Every name `pattern` accepts, from `old` into `new`. An old
    directory that is absent carries nothing; one root does not own
    carries nothing and is named, because none of its files can be proved."""
    shown = old.name
    try:
        src_fd = os.open(old, _DIR_FLAGS)
    except FileNotFoundError:
        return
    except OSError:
        # A symlink or a file where the directory was: planted, not root's.
        moved.refused.append(f"{shown}/")
        return

    try:
        if os.fstat(src_fd).st_uid != owner_uid:
            moved.refused.append(f"{shown}/")
            return

        dst_fd = _open_dir(new)
        if dst_fd is None:
            raise RelocateError(f"{new} is not a directory. The visit makes it first.")

        try:
            for name in sorted(os.listdir(src_fd)):
                if not pattern.fullmatch(name):
                    continue

                _carry_one(src_fd, dst_fd, name, mode, owner_uid, f"{shown}/{name}", moved)
        finally:
            os.close(dst_fd)
    finally:
        os.close(src_fd)


def _carry_roster(prefix: Path, new_fd: int, owner_uid: int, moved: Moved) -> None:
    """The one file whose DIRECTORY is the operator's. What proves it is the
    file's own owner, which the operator cannot set."""
    try:
        src_fd = os.open(_under(prefix, OLD_STATE_ROOT), _DIR_FLAGS)
    except FileNotFoundError:
        return
    except OSError:
        # A link where the operator's state root was: whatever it holds is the operator's.
        moved.refused.append(OLD_ROSTER_NAME)
        return

    try:
        if not _exists(src_fd, OLD_ROSTER_NAME):
            return

        _carry_one(
            src_fd, new_fd, OLD_ROSTER_NAME, ROSTER_FILE_MODE, owner_uid, OLD_ROSTER_NAME, moved
        )
    finally:
        os.close(src_fd)


def _carry_one(
    src_fd: int,
    dst_fd: int,
    name: str,
    mode: int,
    owner_uid: int,
    shown: str,
    moved: Moved,
) -> None:
    if _exists(dst_fd, name):
        moved.kept.append(shown)
        return

    body = _read_root_file(src_fd, name, owner_uid)
    if body is None:
        moved.refused.append(shown)
        return

    _write_new(dst_fd, name, body, mode)
    moved.copied.append(shown)


def _read_root_file(dir_fd: int, name: str, owner_uid: int) -> bytes | None:
    """The bytes of a regular file `owner_uid` wrote, or None.

    One link, because a second name is a name root did not give it. The
    checks read the OPEN descriptor, so a swap after the open changes
    nothing that is read.
    """
    try:
        fd = os.open(name, _READ_FLAGS, dir_fd=dir_fd)
    except OSError:
        return None

    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None

        if info.st_uid != owner_uid or info.st_nlink != 1:
            return None

        if info.st_size > MAX_MOVED_BYTES:
            return None

        return _read_all(fd, info.st_size)
    finally:
        os.close(fd)


def _read_all(fd: int, size: int) -> bytes | None:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(fd, 1 << 16)
        if not chunk:
            break

        total += len(chunk)
        if total > MAX_MOVED_BYTES:
            return None

        chunks.append(chunk)

    body = b"".join(chunks)

    return body if len(body) == size else None


def _write_new(dir_fd: int, name: str, body: bytes, mode: int) -> None:
    """`O_EXCL`, so a name that appeared since `_exists` looked is left as
    it is. A torn copy is removed rather than kept."""
    try:
        fd = os.open(name, _CREATE_FLAGS, mode, dir_fd=dir_fd)
    except FileExistsError:
        return

    try:
        written = 0
        while written < len(body):
            written += os.write(fd, body[written:])

        os.fchmod(fd, mode)
        os.fsync(fd)
    except OSError:
        os.unlink(name, dir_fd=dir_fd)
        raise
    finally:
        os.close(fd)


def _write_marker(dir_fd: int, moved: Moved) -> None:
    record = {
        "from": OLD_STATE_ROOT,
        "copied": len(moved.copied),
        "kept": len(moved.kept),
        "refused": moved.refused,
        "retired": moved.retired,
    }
    body = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
    _write_new(dir_fd, MARKER_NAME, body, MARKER_MODE)


def _open_dir(path: Path) -> int | None:
    """A directory descriptor, `O_NOFOLLOW`, or None when there is none or
    a symlink stands where it should be."""
    try:
        return os.open(path, _DIR_FLAGS)
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP):
            return None

        raise


def _exists(dir_fd: int, name: str) -> bool:
    """`follow_symlinks=False`: a planted link is a taken name."""
    try:
        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False

    return True
