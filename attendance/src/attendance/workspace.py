"""The per-chat directory of a thin family (contract 02 §12.1, contract 03 §7.2).

A mount cannot join a running sandbox. So a thin family that names
`job.caller_root` in its family file mounts that one root, and each calling
chat gets a directory under it (`docs/rework/spec.md` §7.2). `caregiver`
copies the root into the `limits` block of the status document. This service
creates the directory of a chat at its first delegate call. No job deletes
it. With no chat session here, nothing deletes it either.

The owner session id arrives from the PEP and becomes a path component, so it
is validated here and nowhere else. Every door into this module checks it
first, because a path built once without the check is the whole hole.
"""

from __future__ import annotations

from pathlib import Path

from .atomic import DIR_MODE_PRIVATE, ensure_dir
from .errors import ApiError, ErrorCode
from .ids import is_session


def owner_dir(caller_root: Path, owner_session: str) -> Path:
    """Where one calling chat's files live. The id is checked first."""
    _check_owner(owner_session)
    return caller_root / owner_session


def make_owner_dir(caller_root: Path, owner_session: str) -> Path:
    """Contract 02 §12.1 rule 2: create it at the first delegate call.

    Mode 0700, so a job run for one chat cannot read another chat's files
    (`docs/rework/spec.md` §7.2). Creating it again is a no-op:
    the directory outlives every job that ever used it.
    """
    path = owner_dir(caller_root, owner_session)
    ensure_dir(path, DIR_MODE_PRIVATE)
    return path


def _check_owner(owner_session: str) -> None:
    """The one gate. `is_session` refuses `/`, `.` and `..` by construction."""
    if is_session(owner_session):
        return

    raise ApiError(
        ErrorCode.BAD_REQUEST,
        "owner_session is not a session id",
        detail={"length": len(owner_session)},
    )
