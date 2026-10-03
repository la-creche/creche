"""The per-chat `code-sandbox` directory (contract 02 §12.1, contract 03 §7.2).

A mount cannot join a running sandbox, so the `code-sandbox` family mounts one
parent — the work root — and each calling chat gets a directory under it
(`docs/rework/spec.md` §7.2). This service creates that directory at the
chat's first delegate call and deletes it with the chat session, never with
the job.

The owner session id arrives from the PEP and becomes a path component, so it
is validated here and nowhere else. Every door into this module checks it
first, because a path built once without the check is the whole hole.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .atomic import DIR_MODE_PRIVATE, ensure_dir
from .errors import ApiError, ErrorCode
from .ids import is_session
from .paths import code_sandbox_dir

# The one thin family that works in the caller's directory
# (`docs/rework/spec.md` §7.2).
CODE_SANDBOX_FAMILY = "code-sandbox"

# Contract 03 §7.2's `workspace.kind`. One kind exists.
WORKSPACE_KIND = "code-sandbox"


def owner_dir(work_root: Path, owner_session: str) -> Path:
    """Where one calling chat's files live. The id is checked first."""
    _check_owner(owner_session)
    return code_sandbox_dir(work_root, owner_session)


def make_owner_dir(work_root: Path, owner_session: str) -> Path:
    """Contract 02 §12.1 rule 2: create it at the first delegate call.

    Mode 0700, so a job run for one chat cannot read another chat's files
    (`docs/rework/spec.md` §7.2). Creating it again is a no-op:
    the directory outlives every job that ever used it.
    """
    path = owner_dir(work_root, owner_session)
    ensure_dir(path, DIR_MODE_PRIVATE)
    return path


def remove_owner_dir(work_root: Path, owner_session: str) -> bool:
    """Contract 02 §12.1 rule 4. The CHAT session's delete calls this.

    True when a directory was there. A chat that never delegated has none,
    and deleting it is still a clean delete (contract 02 §5.8).
    """
    path = owner_dir(work_root, owner_session)

    if not path.is_dir():
        return False

    shutil.rmtree(path, ignore_errors=True)
    return True


def workspace_of(family: str, owner_session: str | None) -> dict[str, Any] | None:
    """`start_turn.workspace` for a job, or None (contract 03 §7.2).

    It carries no host path. The playpen derives the target from its own
    mount, so a compromised host message cannot aim the link elsewhere.
    """
    if family != CODE_SANDBOX_FAMILY or owner_session is None:
        return None

    _check_owner(owner_session)
    return {"kind": WORKSPACE_KIND, "owner_session": owner_session}


def _check_owner(owner_session: str) -> None:
    """The one gate. `is_session` refuses `/`, `.` and `..` by construction."""
    if is_session(owner_session):
        return

    raise ApiError(
        ErrorCode.BAD_REQUEST,
        "owner_session is not a session id",
        detail={"length": len(owner_session)},
    )
