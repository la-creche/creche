"""One file per secret, encrypt only (`stage7-releases.md` §4.3).

```
/var/lib/creche-handover/secrets/<name>.enc     root:root 0600
```

Each file is encrypted to the recipient set `.sops.yaml` already names, so
adding a secret needs public keys and nothing else. The PEP, which holds
the runtime private key, decrypts each file at load. The exclusion of
the host's operator account stays exactly as strict, and the authoring problem
disappears.

Five rules hold invariant 13 here.

1. **The plaintext reaches one place: the encryptor's stdin.** Never a
   file, never argv, never a log line, never the ledger. `SealFn` is that
   one door, and `host.make_sealer` is the only implementation that starts
   a child.
2. **A write fills a GAP.** `O_EXCL` refuses a name that already has a
   value, in the kernel rather than after a check (§4.3 rule 2).
   Overwriting is `rotate`, which is approval gated and is not here.
3. **No read verb exists** (§4.3 rule 4). This module can write a secret
   and list which names have one. It cannot answer what one is.
4. **A failed seal writes nothing.** The sealed bytes are produced first
   and the file is created second, so a refusal leaves the directory as it
   was (invariant 19).
5. **The directory is proved before it is used.**
   Sealing needs PUBLIC keys only — that is §4.3's whole point — so any
   writer of this directory can author a `<name>.enc` of its own and
   pre-empt the name the operator is about to fill. Ownership and the write bits
   are therefore the control, and the code checks both: the directory is
   opened once per call, `O_DIRECTORY | O_NOFOLLOW`, refused unless it is
   owned by `owner_uid` and closed to group and other, and every open,
   stat and listing after that runs `dir_fd=` against that one descriptor.
   This is `spool.py`'s rule 1, which this module had skipped.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final, cast

import yaml

from ..executor import layout

#: The one door a plaintext secret goes through: hand it bytes, get sealed
#: bytes or None. The production implementation is `host.make_sealer`,
#: which hands the plaintext to a child's STDIN. Declaring the seam here
#: rather than importing it keeps this module free of any child process.
SealFn = Callable[[bytes], bytes | None]

#: Contract 01b §4.1: a `secret:<key>` name. It becomes a file name, so the
#: pattern is checked before the name is used and it is `fullmatch`.
SECRET_NAME_RE: Final = re.compile(r"[a-z][a-z0-9_]{1,62}")

SECRET_SUFFIX: Final = ".enc"

#: Under root's own root, so no directory the operator owns sits above it
#: (`layout.py`).
DEFAULT_SECRETS_DIR: Final = layout.SECRETS_DIR

#: root:root, owner only. The PEP reads these as its own user through a
#: group the host grants, never through a world bit.
SECRET_FILE_MODE: Final = 0o600

#: A pasted credential. The longest real one is a PEM block; 8 KiB is well
#: past every upstream token in the fleet and still bounds a write.
MAX_SECRET_CHARS: Final = 8192

#: `O_EXCL` is the gap rule (rule 2). `O_NOFOLLOW` refuses a symlink
#: planted where the file would go.
_CREATE_FLAGS: Final = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC

#: Rule 5. `O_NOFOLLOW` here refuses a symlink planted where the DIRECTORY
#: goes, which `_CREATE_FLAGS` cannot see: it guards the last component
#: only, and the last component is the file.
_DIR_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC

#: Rule 5. Root owns the secrets directory, and nobody else may write it.
DEFAULT_OWNER_UID: Final = 0

#: What `recipients_of` will read. `.sops.yaml` is a short file.
MAX_SOPS_BYTES: Final = 64 * 1024

#: An age public key: `age1` and 58 characters of bech32. Every recipient
#: becomes part of one `--age` argument, and a value that is not a key is
#: a value root does not understand.
AGE_RECIPIENT_RE: Final = re.compile(r"age1[0-9a-z]{58}")


def recipients_of(
    sops_file: Path,
    for_path: str,
    *,
    owner_uid: int = DEFAULT_OWNER_UID,
) -> tuple[str, ...]:
    """The age recipients `.sops.yaml` names for one path.

    PUBLIC keys, which is the whole point: the host encrypts to them and
    holds none of the matching private keys. An empty answer means root
    found no rule it would use, and the caller must refuse rather than
    invent one. `host.make_sealer` refuses an empty set already.

    **This file is a trust root.** Because sealing
    needs public keys only, ADDING one recipient is enough to read every
    secret written afterwards. So the file must be owned by `owner_uid`
    and closed to group and other, and every recipient must be a
    well-formed age public key. A checkout under
    `/srv/agents/work/platform/` is operator-writable and is therefore the
    wrong place to read this from.
    """
    try:
        fd = os.open(sops_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return ()

    try:
        info = os.fstat(fd)
        if info.st_uid != owner_uid or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            return ()

        raw = os.read(fd, MAX_SOPS_BYTES + 1)
    except OSError:
        return ()
    finally:
        os.close(fd)

    if len(raw) > MAX_SOPS_BYTES:
        return ()

    try:
        loaded: Any = yaml.safe_load(raw.decode("utf-8", "replace"))
    except yaml.YAMLError:
        # This file is root's, so a malformed one is a foot-gun and not an
        # attack — and a raise here would escape `build_wiring`, so `main`
        # would die before the loop starts and `Restart=always` would turn
        # one typo into a unit that burns its start limit and stops.
        return ()

    if not isinstance(loaded, dict):
        return ()

    rules = cast("dict[str, object]", loaded).get("creation_rules")
    if not isinstance(rules, list):
        return ()

    for rule in cast("list[object]", rules):
        found = _recipients_in(rule, for_path)
        if found:
            return found

    return ()


def _matches(pattern: str, for_path: str) -> bool:
    """A `path_regex` that is not a valid regular expression is a rule
    that matches nothing, never a process that dies before its loop starts."""
    try:
        return re.search(pattern, for_path) is not None
    except re.error:
        return False


def _recipients_in(rule: object, for_path: str) -> tuple[str, ...]:
    if not isinstance(rule, dict):
        return ()

    fields = cast("dict[str, object]", rule)
    pattern = fields.get("path_regex")
    if not isinstance(pattern, str) or not _matches(pattern, for_path):
        return ()

    age = fields.get("age")
    if not isinstance(age, str):
        return ()

    found = tuple(one.strip() for one in age.split(",") if one.strip())
    if not all(AGE_RECIPIENT_RE.fullmatch(one) for one in found):
        # Fail closed on the SET, not on the one entry: a rule root cannot
        # read cleanly is a rule root does not seal to.
        return ()

    return found


class SecretStore:
    """Where a pasted value lands. It can fill a gap and list the names it
    has filled. **It cannot read one** (§4.3 rule 4)."""

    def __init__(
        self,
        directory: Path,
        seal: SealFn,
        *,
        owner_uid: int = DEFAULT_OWNER_UID,
    ) -> None:
        self._directory = directory
        self._seal = seal
        self._owner_uid = owner_uid

    def has(self, name: str) -> bool:
        """Whether a name already holds a value. The NAME, never a value —
        this is what the reconciler's gap check asks.

        Any entry at that name counts, including a dangling symlink. The
        old `Path.exists()` followed one and answered False, so `fill`
        sealed a value it could never write.
        """
        if SECRET_NAME_RE.fullmatch(name) is None:
            return False

        fd = self._open_directory()
        if fd is None:
            return False

        try:
            return _taken(fd, f"{name}{SECRET_SUFFIX}")
        finally:
            os.close(fd)

    def filled(self) -> tuple[str, ...]:
        """Every name that has a value. For the gap report and the view."""
        fd = self._open_directory()
        if fd is None:
            return ()

        try:
            entries = sorted(os.listdir(fd))
        except OSError:
            return ()
        finally:
            os.close(fd)

        return tuple(
            entry.removesuffix(SECRET_SUFFIX) for entry in entries if entry.endswith(SECRET_SUFFIX)
        )

    def fill(self, name: str, value: str) -> bool:
        """Seal one value into one gap. True when it landed.

        False is every refusal: a bad name, an oversized value, a directory
        this store will not write into, a name that already has a value, an
        encryptor that would not answer, or a write that failed. None of
        them says which, and none of them writes a partial file.

        Every step after the directory opens runs against that one
        descriptor, so a rename of the directory mid-call cannot move the
        write somewhere else.
        """
        if SECRET_NAME_RE.fullmatch(name) is None:
            return False

        if not value or len(value) > MAX_SECRET_CHARS:
            return False

        fd = self._open_directory()
        if fd is None:
            return False

        try:
            file_name = f"{name}{SECRET_SUFFIX}"
            if _taken(fd, file_name):
                return False

            sealed = self._seal(value.encode("utf-8"))
            if not sealed:
                return False

            return _write_new(fd, file_name, sealed)
        finally:
            os.close(fd)

    def _open_directory(self) -> int | None:
        """Rule 5. The directory, proved: not a symlink, owned by
        `owner_uid`, and closed to group and other. None is every refusal,
        and a caller that gets None writes nothing."""
        try:
            fd = os.open(self._directory, _DIR_FLAGS)
        except OSError:
            return None

        info = os.fstat(fd)
        if info.st_uid != self._owner_uid or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            os.close(fd)

            return None

        return fd


def _taken(dir_fd: int, file_name: str) -> bool:
    """Whether anything at all sits at that name. `follow_symlinks=False`,
    because a planted link is a taken name, not an empty one."""
    try:
        os.stat(file_name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return False

    return True


def _write_new(dir_fd: int, file_name: str, sealed: bytes) -> bool:
    """`O_EXCL`, so the kernel enforces the gap rule under a race.

    The write loops and syncs. A torn `.enc` could never be repaired:
    `O_EXCL` refuses a rewrite and there is no `rotate` verb here, so a
    half-written file is a permanent gap.
    """
    try:
        fd = os.open(file_name, _CREATE_FLAGS, SECRET_FILE_MODE, dir_fd=dir_fd)
    except OSError:
        return False

    try:
        written = 0
        while written < len(sealed):
            written += os.write(fd, sealed[written:])

        os.fsync(fd)
    except OSError:
        _unlink_quiet(dir_fd, file_name)

        return False
    finally:
        os.close(fd)

    return True


def _unlink_quiet(dir_fd: int, file_name: str) -> None:
    try:
        os.unlink(file_name, dir_fd=dir_fd)
    except OSError:
        # Nothing to report: the caller already answers False, and the
        # name is the only thing that could be said (`safe_token`).
        del file_name
