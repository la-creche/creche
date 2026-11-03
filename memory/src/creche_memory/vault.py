"""Reads from the vault: one note, or the entries of one folder.

Each call opens the vault root, then each folder of the path relative to the
folder before it, with `O_NOFOLLOW`. The wire types refuse `.`, `..` and
hidden parts, so no part leaves the vault. A symlink anywhere on the path
fails the open."""

import errno
import hashlib
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from .wire import (
    MAX_LIST_ENTRIES,
    MAX_TEXT_BYTES,
    NOTE_SUFFIX,
    FolderPath,
    ListReply,
    Note,
    ReadReply,
    Refusal,
    RefusedReply,
    Sha256,
    VaultPath,
)

ROOT_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
DIR_FLAGS = ROOT_FLAGS | os.O_NOFOLLOW
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC

#: An open that fails with one of these finds no file at the path.
MISSING = frozenset({errno.ENOENT, errno.ENOTDIR})


def _refused(code: Refusal, reason: str) -> RefusedReply:
    return RefusedReply(ok=False, refused=code, reason=reason)


def _open_refusal(error: OSError) -> RefusedReply:
    if error.errno in MISSING:
        return _refused(Refusal.NOT_FOUND, "no such file or folder")

    if error.errno == errno.ELOOP:
        return _refused(Refusal.NOT_ALLOWED_HERE, "the path holds a symlink")

    return _refused(Refusal.INTERNAL, f"open failed: {errno.errorcode.get(error.errno or 0)}")


def _time(info: os.stat_result) -> datetime:
    return datetime.fromtimestamp(info.st_mtime, UTC)


def _child(folder: FolderPath, name: str) -> str:
    return f"{folder.root}/{name}" if folder.root else name


def _entry(folder: FolderPath, entry: os.DirEntry[str]) -> Note | FolderPath | None:
    """A note or a folder of the listing. A symlink, a hidden entry, a name the
    wire types refuse and a file that is not a note are none."""
    child = _child(folder, entry.name)
    try:
        if entry.is_dir(follow_symlinks=False):
            return FolderPath(child)

        if entry.is_file(follow_symlinks=False) and child.endswith(NOTE_SUFFIX):
            info = entry.stat(follow_symlinks=False)
            return Note(path=VaultPath(child), bytes=info.st_size, modified=_time(info))
    except ValidationError:
        return None

    return None


class Vault:
    """The vault, read as the user that runs the service."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def read_note(self, path: VaultPath) -> ReadReply | RefusedReply:
        *folders, name = path.root.split("/")
        try:
            fd = self._open_file(folders, name)
        except OSError as error:
            return _open_refusal(error)

        with os.fdopen(fd, "rb") as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode):
                return _refused(Refusal.NOT_FOUND, "not a regular file")

            data = file.read(MAX_TEXT_BYTES + 1)

        if len(data) > MAX_TEXT_BYTES:
            return _refused(Refusal.TOO_LARGE, f"the note is over {MAX_TEXT_BYTES} bytes")

        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return _refused(Refusal.BAD_REQUEST, "the note is not UTF-8")

        digest = Sha256(hashlib.sha256(data).hexdigest())

        return ReadReply(
            ok=True,
            op="read",
            path=path,
            text=text,
            sha256=digest,
            bytes=len(data),
            modified=_time(info),
        )

    def list_folder(self, folder: FolderPath) -> ListReply | RefusedReply:
        parts = folder.root.split("/") if folder.root else []
        try:
            fd = self._open_folder(parts)
        except OSError as error:
            return _open_refusal(error)

        notes: list[Note] = []
        folders: list[FolderPath] = []
        try:
            with os.scandir(fd) as entries:
                for entry in entries:
                    found = _entry(folder, entry)
                    if isinstance(found, Note):
                        notes.append(found)
                    elif isinstance(found, FolderPath):
                        folders.append(found)

                    if len(notes) + len(folders) > MAX_LIST_ENTRIES:
                        reason = f"the folder holds over {MAX_LIST_ENTRIES} entries"
                        return _refused(Refusal.TOO_LARGE, reason)
        finally:
            os.close(fd)

        notes.sort(key=lambda note: note.path.root)
        folders.sort(key=lambda path: path.root)

        return ListReply(ok=True, op="list", notes=notes, folders=folders)

    def _open_folder(self, parts: list[str]) -> int:
        fd = os.open(self._root, ROOT_FLAGS)
        for part in parts:
            try:
                child = os.open(part, DIR_FLAGS, dir_fd=fd)
            finally:
                os.close(fd)

            fd = child

        return fd

    def _open_file(self, folders: list[str], name: str) -> int:
        fd = self._open_folder(folders)
        try:
            return os.open(name, FILE_FLAGS, dir_fd=fd)
        finally:
            os.close(fd)
