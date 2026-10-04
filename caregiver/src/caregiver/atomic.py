"""Atomic writes (contract 04 §1.3, contract 05 §2 rule 2).

A temp file in the target's own directory, `fsync`, `rename`, then `fsync`
the directory so the rename survives a power loss. Every writer in this
package that publishes a file another process reads goes through here, so
"a reader never sees a half file" is one implementation, not one per
caller. `atomic_replace_dir` is the same idea for a whole directory (the
family config mount, contract 01 §6.1 rule 1).

`read_json` is the one reader of a JSON file. Every module of this package
that reads a JSON object from a file goes through it, so "content that does
not read is a refusal, not an exception" is one implementation too."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, cast


def atomic_write(path: Path, data: bytes, *, mode: int) -> None:
    """Write `data` to `path`. A reader sees the old bytes or the new
    bytes, never a mix, because `rename` inside one directory is atomic."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

    _fsync_dir(path.parent)


def atomic_replace_dir(staging: Path, target: Path) -> None:
    """Swap `staging` in for `target`. The caller builds `staging` fully
    first (contract 01 §6.1 rule 1: "a reader never sees half a revision").

    `rename()` on a directory requires the destination to be empty or
    missing (Linux `rename(2)`), so a `target` that already holds a
    previous revision cannot be replaced with one `rename` call. This
    moves it aside with a first `rename` (always legal: the destination of
    *that* rename does not exist yet), swaps `staging` in with a second,
    then deletes the displaced copy. Two renames bracket a brief window
    where the path does not exist at all — never one where it holds
    half-written content, which is the guarantee the contract wants."""
    target.parent.mkdir(parents=True, exist_ok=True)
    displaced = target.with_name(target.name + ".old")
    if target.exists():
        if displaced.exists():
            shutil.rmtree(displaced)

        os.replace(target, displaced)

    os.replace(staging, target)
    _fsync_dir(target.parent)
    shutil.rmtree(displaced, ignore_errors=True)


def read_json(path: Path) -> dict[str, Any] | None:
    """The JSON object in the file at `path`, or None.

    None is the refusal: the file is absent, it does not read, or its
    content is not one JSON object. The caller decides what a refusal
    means. This function does not raise on content, so one bad file
    cannot end the pass or the loop that reads it."""
    try:
        raw = path.read_bytes()
    except OSError:
        return None

    try:
        body: object = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        # ValueError covers bytes that are not UTF-8, text that is not
        # JSON and an integer past the digit limit of the interpreter.
        # Nesting past the limit of the parser raises RecursionError.
        return None

    if not isinstance(body, dict):
        return None

    return cast("dict[str, Any]", body)


def _fsync_dir(directory: Path) -> None:
    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
