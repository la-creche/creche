"""Writes to the vault: a new inbox note, or a whole-file rewrite.

An agent creates notes only in `inbox/`. It rewrites whole files only in the
places that agents own: `inbox/`, `house/ha/` and `house/scrum/`. A rewrite
carries the SHA-256 of the file as the agent read it, or `new` for a file
that must not exist yet. One lock holds each write, so no two writes race.

Each write goes to a temp file in the same folder first. A new file is
linked into place, so an existing file stops it. A rewrite renames the temp
file over the old one. The service user owns each file, with mode 0644."""

import contextlib
import hashlib
import os
import secrets
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from .vault import DIR_FLAGS, FILE_FLAGS, ROOT_FLAGS, open_refusal
from .wire import (
    NOTE_SUFFIX,
    CreateRequest,
    Refusal,
    RefusedReply,
    RewriteRequest,
    Sha256,
    VaultPath,
    WriteReply,
)

INBOX = "inbox"

#: The folders under which an agent may rewrite a whole file (decision MM5).
REWRITE_AREAS: tuple[tuple[str, ...], ...] = (("inbox",), ("house", "ha"), ("house", "scrum"))

FILE_MODE = 0o644
FOLDER_MODE = 0o755
TEMP_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
HASH_CHUNK = 64 * 1024

type Clock = Callable[[], datetime]
type Expect = Sha256 | Literal["new"]
type WriteOp = Literal["create", "rewrite"]


def _refused(code: Refusal, reason: str) -> RefusedReply:
    return RefusedReply(ok=False, refused=code, reason=reason)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _in_area(parts: list[str]) -> bool:
    return any(
        len(parts) > len(area) and tuple(parts[: len(area)]) == area for area in REWRITE_AREAS
    )


def _note_text(request: CreateRequest, created: datetime) -> str:
    stamp = created.strftime("%Y-%m-%dT%H:%M:%SZ")
    body = request.body if request.body.endswith("\n") else request.body + "\n"

    return (
        f"---\nfamily: {request.caller.root}\ncreated: {stamp}\n---\n\n# {request.title}\n\n{body}"
    )


def _digest_of(dir_fd: int, name: str) -> str:
    fd = os.open(name, FILE_FLAGS, dir_fd=dir_fd)
    digest = hashlib.sha256()
    with os.fdopen(fd, "rb") as file:
        while chunk := file.read(HASH_CHUNK):
            digest.update(chunk)

    return digest.hexdigest()


class Writer:
    """Applies each write to the vault, as the user that runs the service."""

    def __init__(self, root: Path, clock: Clock = _utc_now) -> None:
        self._root = root
        self._clock = clock
        self._lock = threading.Lock()

    def create(self, request: CreateRequest) -> WriteReply | RefusedReply:
        created = self._clock()
        name = f"{created:%Y-%m-%d}-{request.slug.root}{NOTE_SUFFIX}"
        path = VaultPath(f"{INBOX}/{name}")

        return self._write([INBOX], name, path, _note_text(request, created), "new", "create")

    def rewrite(self, request: RewriteRequest) -> WriteReply | RefusedReply:
        *folders, name = request.path.root.split("/")
        if not _in_area([*folders, name]):
            return _refused(Refusal.NOT_ALLOWED_HERE, "not a place that agents rewrite")

        return self._write(
            folders, name, request.path, request.text, request.if_match.root, "rewrite"
        )

    def _write(
        self,
        folders: list[str],
        name: str,
        path: VaultPath,
        text: str,
        if_match: Expect,
        op: WriteOp,
    ) -> WriteReply | RefusedReply:
        data = text.encode()
        with self._lock:
            try:
                dir_fd = self._open_folder(folders, if_match)
            except OSError as error:
                return open_refusal(error)

            try:
                return self._apply(dir_fd, name, path, data, if_match, op)
            except OSError as error:
                return open_refusal(error)
            finally:
                os.close(dir_fd)

    def _apply(
        self, dir_fd: int, name: str, path: VaultPath, data: bytes, if_match: Expect, op: WriteOp
    ) -> WriteReply | RefusedReply:
        if isinstance(if_match, Sha256):
            try:
                found = _digest_of(dir_fd, name)
            except FileNotFoundError:
                return _refused(Refusal.NOT_FOUND, "no such file to rewrite")

            if found != if_match.root:
                return _refused(Refusal.CHANGED, "the file changed since it was read")

        temp = self._temp(dir_fd, data)
        try:
            if isinstance(if_match, Sha256):
                os.rename(temp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            else:
                os.link(temp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)
        except FileExistsError:
            return _refused(Refusal.EXISTS, "the file exists")
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temp, dir_fd=dir_fd)

        os.fsync(dir_fd)
        digest = Sha256(hashlib.sha256(data).hexdigest())

        return WriteReply(ok=True, op=op, path=path, sha256=digest)

    def _temp(self, dir_fd: int, data: bytes) -> str:
        temp = f".memory-{secrets.token_hex(8)}.tmp"
        fd = os.open(temp, TEMP_FLAGS, FILE_MODE, dir_fd=dir_fd)
        with os.fdopen(fd, "wb") as file:
            file.write(data)
            file.flush()
            os.fchmod(file.fileno(), FILE_MODE)
            os.fsync(file.fileno())

        return temp

    def _open_folder(self, parts: list[str], if_match: Expect) -> int:
        """Open each folder relative to the one before it. For a new file,
        make a folder that is missing. A symlink on the path fails the open."""
        fd = os.open(self._root, ROOT_FLAGS)
        for part in parts:
            try:
                if isinstance(if_match, Sha256):
                    child = os.open(part, DIR_FLAGS, dir_fd=fd)
                else:
                    child = self._open_or_make(fd, part)
            finally:
                os.close(fd)

            fd = child

        return fd

    def _open_or_make(self, fd: int, part: str) -> int:
        try:
            return os.open(part, DIR_FLAGS, dir_fd=fd)
        except FileNotFoundError:
            pass

        try:
            os.mkdir(part, FOLDER_MODE, dir_fd=fd)
        except FileExistsError:
            return os.open(part, DIR_FLAGS, dir_fd=fd)

        child = os.open(part, DIR_FLAGS, dir_fd=fd)
        os.fchmod(child, FOLDER_MODE)

        return child
