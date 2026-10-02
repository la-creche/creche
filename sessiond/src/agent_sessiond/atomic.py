"""Atomic file writes and guarded reads.

Every document this service publishes is written to a temporary file in the
same directory, fsynced, then renamed into place. `rename` is atomic. Unlink
plus recreate is not, and a reader would see a partial document (contract 05
§2 rule 2, §3.3.1 rule 3).

Reads of a file another process writes are guarded by a size cap, because such
a file is untrusted input like any other (invariants 12 and 14).
"""

from __future__ import annotations

import itertools
import json
import os
from pathlib import Path
from typing import Any, cast

JsonObject = dict[str, Any]

MODE_PRIVATE = 0o600
MODE_GROUP_READ = 0o640
MODE_PUBLIC_READ = 0o644
DIR_MODE_GROUP = 0o750
DIR_MODE_PRIVATE = 0o700

# A status document, a lock file or a fault file is small. Anything larger is
# either a bug or an attempt to make this service read a file into memory.
MAX_JSON_BYTES = 1_048_576

_counter = itertools.count()


def write_json(path: Path, payload: dict[str, Any], mode: int = MODE_PUBLIC_READ) -> None:
    """Write one JSON document atomically, at the mode the contract names."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, separators=(",", ":"), sort_keys=False) + "\n"
    write_text(path, body, mode)


def write_text(path: Path, body: str, mode: int = MODE_PUBLIC_READ) -> None:
    """Same discipline for a document that is not JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{next(_counter)}.tmp")

    try:
        handle = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)

        try:
            os.write(handle, body.encode("utf-8"))
            os.fsync(handle)
        finally:
            os.close(handle)

        # os.open honours the umask, so the mode is set again explicitly.
        os.chmod(temp, mode)
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def read_json(path: Path, max_bytes: int = MAX_JSON_BYTES) -> dict[str, Any] | None:
    """Read a JSON object, or None when it is absent, oversized or malformed.

    None means "nothing usable here". A caller decides what that means; this
    function never raises on bad content, because bad content from another
    process is expected rather than exceptional.
    """
    try:
        stat = path.stat()
    except OSError:
        return None

    if stat.st_size > max_bytes:
        return None

    try:
        raw = path.read_bytes()
    except OSError:
        return None

    try:
        parsed: object = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None

    return as_object(parsed)


def as_object(value: object) -> JsonObject | None:
    """Narrow a decoded JSON value to an object, or None.

    Parsed JSON arrives as `object`. One helper does the narrowing so no
    caller has to repeat the cast, and so "not an object" has one meaning.
    """
    if not isinstance(value, dict):
        return None

    return cast(JsonObject, value)


def as_array(value: object) -> list[Any] | None:
    """Narrow a decoded JSON value to an array, or None."""
    if not isinstance(value, list):
        return None

    return cast("list[Any]", value)


def ensure_dir(path: Path, mode: int = DIR_MODE_GROUP) -> None:
    """Create a directory and pin its mode, whatever the umask says."""
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, mode)


def is_group_or_world_readable(path: Path) -> bool:
    """True when anyone but the owner can read the file."""
    try:
        stat = path.stat()
    except OSError:
        return False

    return bool(stat.st_mode & 0o077)
