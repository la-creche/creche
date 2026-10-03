"""The applied snapshot: the revision that is live right now (contract
05 §2.1's `applied_rev`).

`classify()` answers "live or replace" from two parsed family files. The
registry holds only the new one. So the reconciler keeps the old one here,
verbatim, and re-parses it on the next pass:

    families/<family>/applied/family.yaml   the text that was applied
    families/<family>/applied/applied.json  its revision, image and time

The snapshot moves at the END of a successful reconcile and at no other
moment. That is what makes a crash converge: an interrupted pass leaves the
old snapshot, so the next pass computes the same diff and repeats the same
steps. Every step is written to be safe to repeat.

A snapshot that will not parse reads as "nothing applied", which makes the
next pass a fresh apply. Never a crash (invariant 19's spirit, applied to
`caregiver`'s own state)."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

from agent_family import FamilyFile, parse_family

from . import paths
from .atomic import atomic_write
from .clock import now_rfc3339

#: The snapshot holds no secret: a family file names grants and mounts, and
#: the credential values live in `creds.json` at 0600 (contract 03 §12).
APPLIED_FILE_MODE: Final = 0o644


@dataclass(frozen=True)
class AppliedState:
    """One family's live definition, as `caregiver` last applied it."""

    rev: str
    image: str
    applied_at: str
    family: FamilyFile
    #: When a `/key/update` may be treated as live on every LiteLLM worker
    #: (contract 05 §7 rule 6). `None` when no model change is outstanding.
    #: It lives in the snapshot because the 10 seconds outlive the pass that
    #: started them, and a restart in between must not forget them.
    model_live_at: str | None = None


def write_applied(
    state_root: Path,
    family_name: str,
    *,
    rev: str,
    image: str,
    family_text: str,
    model_live_at: str | None = None,
) -> None:
    """Record this revision as the live one. The caller writes it only
    after every step of the reconcile succeeded."""
    atomic_write(
        paths.applied_family_path(state_root, family_name),
        family_text.encode("utf-8"),
        mode=APPLIED_FILE_MODE,
    )
    meta = {
        "rev": rev,
        "image": image,
        "applied_at": now_rfc3339(),
        "model_live_at": model_live_at,
    }
    atomic_write(
        paths.applied_meta_path(state_root, family_name),
        json.dumps(meta, indent=2).encode("utf-8") + b"\n",
        mode=APPLIED_FILE_MODE,
    )


def read_applied(state_root: Path, family_name: str) -> AppliedState | None:
    """`None` when this family has never been applied, or when the
    snapshot cannot be read or parsed."""
    meta = _read_meta(paths.applied_meta_path(state_root, family_name))
    if meta is None:
        return None

    try:
        text = paths.applied_family_path(state_root, family_name).read_text(encoding="utf-8")
    except OSError:
        return None

    family, _ = parse_family(text)
    if family is None:
        return None

    live_at = meta.get("model_live_at")
    return AppliedState(
        rev=str(meta.get("rev", "")),
        image=str(meta.get("image", "")),
        applied_at=str(meta.get("applied_at", "")),
        family=family,
        model_live_at=live_at if isinstance(live_at, str) else None,
    )


def read_applied_text(state_root: Path, family_name: str) -> str:
    """The snapshot's own bytes, or an empty string. The reconciler needs
    them to rewrite an unchanged snapshot without re-serialising a parsed
    model, which would lose comments the author wrote."""
    try:
        return paths.applied_family_path(state_root, family_name).read_text(encoding="utf-8")
    except OSError:
        return ""


def forget_applied(state_root: Path, family_name: str) -> None:
    """Drop the snapshot. A deleted family has no live revision, and a
    family whose next apply must start from nothing has none either."""
    shutil.rmtree(paths.applied_dir(state_root, family_name), ignore_errors=True)


def _read_meta(path: Path) -> dict[str, Any] | None:
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    return cast("dict[str, Any]", body) if isinstance(body, dict) else None
