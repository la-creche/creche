"""The applied snapshot: the revision that is live right now (contract
05 §2.1's `applied_rev`).

`classify()` answers "live or replace" from two parsed family files. The
registry holds only the new one. So the reconciler keeps the old one here,
verbatim, and re-parses it on the next pass:

    families/<family>/applied/family.toml   the text that was applied, TOML
    families/<family>/applied/family.yaml   the text that was applied, YAML
    families/<family>/applied/applied.json  its revision, image and time

The copy has the name of the markup of its text, as the registry file has.
A write keeps one copy: it writes the copy of its markup, then it removes
the other copy, then it writes the meta file. A read takes `family.toml`
first and `family.yaml` second, and it gives each text to the parser of its
markup. A stop between the write and the remove leaves two copies and the
old meta file, and a read takes the TOML copy:

- After the write of a TOML copy, that copy holds the new text.
- After the write of a YAML copy, that copy holds the old text. The snapshot
  then did not move.

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
from typing import Final

from agent_family import FamilyFile, Markup, parse_family

from . import paths
from .atomic import atomic_write, read_json
from .clock import now_rfc3339

#: The snapshot holds no secret: a family file names grants and mounts, and
#: the credential values live in `creds.json` at 0600 (contract 03 §12).
APPLIED_FILE_MODE: Final = 0o644

#: The sequence in which a read looks for the copy.
_READ_ORDER: Final = (Markup.TOML, Markup.YAML)


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
    markup: Markup,
    model_live_at: str | None = None,
) -> None:
    """Record this revision as the live one. The caller writes it only
    after every step of the reconcile succeeded. `markup` is the markup of
    `family_text`, and the copy gets the name of that markup."""
    atomic_write(
        paths.applied_family_path(state_root, family_name, markup),
        family_text.encode("utf-8"),
        mode=APPLIED_FILE_MODE,
    )
    for other in Markup:
        if other is not markup:
            paths.applied_family_path(state_root, family_name, other).unlink(missing_ok=True)

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
    meta = read_json(paths.applied_meta_path(state_root, family_name))
    if meta is None:
        return None

    copy = _read_copy(state_root, family_name)
    if copy is None:
        return None

    text, markup = copy
    family, _ = parse_family(text, markup)
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
    copy = _read_copy(state_root, family_name)
    return copy[0] if copy is not None else ""


def _read_copy(state_root: Path, family_name: str) -> tuple[str, Markup] | None:
    """The text of the copy and its markup. The read takes the first copy
    that is there, in `_READ_ORDER`. `None` when no copy is there, and when
    the first copy cannot be read."""
    for markup in _READ_ORDER:
        path = paths.applied_family_path(state_root, family_name, markup)
        try:
            return path.read_text(encoding="utf-8"), markup
        except FileNotFoundError:
            continue
        except (OSError, UnicodeDecodeError):
            return None

    return None


def forget_applied(state_root: Path, family_name: str) -> None:
    """Drop the snapshot. A deleted family has no live revision, and a
    family whose next apply must start from nothing has none either."""
    shutil.rmtree(paths.applied_dir(state_root, family_name), ignore_errors=True)
