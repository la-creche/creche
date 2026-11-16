"""The verify hook of the `ci-runner-image` row: `handover check-ci-image`.

The row is a disk-image tree: signed files that root installs unchanged
(contract 06 §8.4). The tree holds no program, so the hook is a command of
the `handover` tree, which the `depends_on` of the row names (§4 rule 6).
The hook answers one question: does each file of the tree give the bytes
that the build made?

```
digests.sha256   <sha256>  root.qcow2   <- root.qcow2.zst, decompressed
                 <sha256>  vmlinuz      <- vmlinuz
                 <sha256>  initrd.img   <- initrd.img
```

The build wrote `digests.sha256` before the compression
(`ci-runner-image/bin/ci-runner-image-release`). Root checked the signature
of each file and the SHA-256 of each file as it ships. So this check adds
the one fact that root does not read: the zstd stream decompresses to the
root disk of the build.

Pure: it reads the files of the tree that the caller names, and writes
nothing. It starts no child. The standard library of Python 3.14
decompresses zstd (`compression.zstd`).
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Generator
from compression import zstd
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Final

from .artifacts import disk_files
from .catalog import (
    CI_DIGESTS_FILE,
    CI_IMAGE,
    CI_INITRD_FILE,
    CI_KERNEL_FILE,
    CI_ROOT_DISK,
    CI_ROOT_FILE,
    CI_ROOT_MAX_BYTES,
)

#: The bytes of one read.
CHUNK_BYTES: Final = 1024 * 1024

#: One line of `CI_DIGESTS_FILE`, as `sha256sum` prints it in text mode.
_LINE_RE: Final = re.compile(r"([0-9a-f]{64})  ([a-z0-9.]+)")

#: The names that `CI_DIGESTS_FILE` holds, one line each, in this order:
#: the order of `names` of the build.
DIGEST_NAMES: Final = (CI_ROOT_DISK, CI_KERNEL_FILE, CI_INITRD_FILE)

#: The texts of a failed check. A text names no byte of a file.
NOT_A_FILE: Final = "is not a regular file of the tree"
TOO_LARGE: Final = "is larger than its limit"
NOT_DIGESTS: Final = f"is not three sha256sum lines of {', '.join(DIGEST_NAMES)}"
NOT_ZSTD: Final = "is not a whole zstd stream"
TOO_LARGE_ROOT: Final = f"decompresses to more than {CI_ROOT_MAX_BYTES} bytes"
OTHER_BYTES: Final = "has another SHA-256 than the build gave"
UNCHECKED: Final = f"is not checked: {CI_DIGESTS_FILE} gave no digest"
MATCHES: Final = "has the SHA-256 that the build gave"


class _Fault(Exception):
    """One file failed its check. The text is one of the texts above."""


@dataclass(frozen=True)
class ImageDigests:
    """The SHA-256 of each file of the image after the build, as 64 hex."""

    root: str
    kernel: str
    initrd: str


@dataclass(frozen=True)
class FileCheck:
    """The result of the check of one file of the tree."""

    file: str
    ok: bool
    detail: str

    def as_dict(self) -> dict[str, str | bool]:
        return {"file": self.file, "ok": self.ok, "detail": self.detail}


def parse_digests(data: bytes) -> ImageDigests:
    """The three lines of `CI_DIGESTS_FILE`, each `<64 hex>  <name>` and a
    line feed, with the names of `DIGEST_NAMES` in that order. Each other
    text raises `_Fault`."""
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError as error:
        raise _Fault(NOT_DIGESTS) from error

    lines = text.split("\n")
    if len(lines) != len(DIGEST_NAMES) + 1 or lines[-1] != "":
        raise _Fault(NOT_DIGESTS)

    found: list[str] = []
    for line, name in zip(lines, DIGEST_NAMES, strict=False):
        matched = _LINE_RE.fullmatch(line)
        if matched is None or matched.group(2) != name:
            raise _Fault(NOT_DIGESTS)

        found.append(matched.group(1))

    root, kernel, initrd = found

    return ImageDigests(root=root, kernel=kernel, initrd=initrd)


def check_image(tree: Path) -> tuple[FileCheck, ...]:
    """One check for each file of the row, in the order of
    `artifacts.disk_files`. A tree that cannot be read fails each check."""
    limits = {one.name: one.max_bytes for one in disk_files(CI_IMAGE)}
    try:
        with _open(tree, CI_DIGESTS_FILE, limits[CI_DIGESTS_FILE]) as file:
            digests = parse_digests(file.read(limits[CI_DIGESTS_FILE] + 1))
    except _Fault as fault:
        first = FileCheck(CI_DIGESTS_FILE, False, str(fault))
        rest = (FileCheck(name, False, UNCHECKED) for name in limits if name != CI_DIGESTS_FILE)

        return (first, *rest)

    wanted = {
        CI_KERNEL_FILE: digests.kernel,
        CI_INITRD_FILE: digests.initrd,
        CI_ROOT_FILE: digests.root,
    }
    checks = [FileCheck(CI_DIGESTS_FILE, True, f"names {', '.join(DIGEST_NAMES)}")]
    for name, sha256 in wanted.items():
        checks.append(_check_one(tree, name, limits[name], sha256))

    return tuple(checks)


def _check_one(tree: Path, name: str, limit: int, sha256: str) -> FileCheck:
    """The SHA-256 of one file, or of the root disk that its zstd stream
    gives, against the digest of the build."""
    try:
        with _open(tree, name, limit) as file:
            found = _root_sha256(file) if name == CI_ROOT_FILE else _sha256(file)
    except _Fault as fault:
        return FileCheck(name, False, str(fault))

    if found != sha256:
        return FileCheck(name, False, OTHER_BYTES)

    return FileCheck(name, True, MATCHES)


@contextmanager
def _open(tree: Path, name: str, limit: int) -> Generator[BinaryIO]:
    """One file of the tree: a regular file, no link, at most `limit`
    bytes. Each other entry, and an entry that cannot be read, raises
    `_Fault`."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        directory = os.open(tree, flags | os.O_DIRECTORY)
    except OSError as error:
        raise _Fault(NOT_A_FILE) from error

    # `O_NONBLOCK`: an open of a FIFO in place of a file returns at once.
    # It changes no read of a regular file.
    try:
        descriptor = os.open(name, flags | os.O_NONBLOCK, dir_fd=directory)
    except OSError as error:
        raise _Fault(NOT_A_FILE) from error
    finally:
        os.close(directory)

    # The checks read the descriptor before `fdopen`, which refuses a
    # directory with an error of its own.
    facts = os.fstat(descriptor)
    if not stat.S_ISREG(facts.st_mode):
        os.close(descriptor)
        raise _Fault(NOT_A_FILE)

    if facts.st_size > limit:
        os.close(descriptor)
        raise _Fault(TOO_LARGE)

    with os.fdopen(descriptor, "rb") as file:
        yield file


def _sha256(file: BinaryIO) -> str:
    """The SHA-256 of the bytes of `file`, as 64 hex."""
    digest = hashlib.sha256()
    while chunk := file.read(CHUNK_BYTES):
        digest.update(chunk)

    return digest.hexdigest()


def _root_sha256(file: BinaryIO) -> str:
    """The SHA-256 of the bytes that the zstd stream of `file` gives, as 64
    hex. A stream that is not whole zstd, or that gives more than
    `CI_ROOT_MAX_BYTES`, raises `_Fault`."""
    digest = hashlib.sha256()
    total = 0
    try:
        with zstd.ZstdFile(file, "rb") as stream:
            while chunk := stream.read(CHUNK_BYTES):
                total += len(chunk)
                if total > CI_ROOT_MAX_BYTES:
                    raise _Fault(TOO_LARGE_ROOT)

                digest.update(chunk)
    except (zstd.ZstdError, EOFError, OSError) as error:
        raise _Fault(NOT_ZSTD) from error

    return digest.hexdigest()
