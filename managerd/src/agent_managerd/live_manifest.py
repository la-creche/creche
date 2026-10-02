"""Publish what is live, from root's ledger (contract 06 §3.4).

Root writes the ledger and the paths it owns, and **never** a git
repository: it holds no push credential, it reads the registry as hostile
input, and a root push to `main` would bypass CI, the pull-request gate and
the PEP's branch fence. So the resolved lock lives in `done/<ULID>.json`,
which is root-owned and authoritative.

`managerd` is what would carry it off the host. It runs as the operator and
the registry checkout is the operator's. **It copies values out of the ledger. It
never invents one.**

**NOTHING CALLS `publish` TODAY, AND NOTHING COMMITS OR PUSHES THE FILE IT
WRITES.** No module under `agent_managerd` imports this one, so
`live-manifest.json` is not written on the host at all, and agent-registry
CI still reads whatever a human last put there. `managerd` runs no git
(`docs/rework/spec.md` §12.2).

Closing that gap is a credential decision, not a function. A push from the
host needs write on agent-registry, and that is a key this code does not
ask for.
`bin/rework-registry-sync.sh` is the half that exists: it pulls this
checkout every minute, it deliberately never pushes, and it survives a copy
of this file that `managerd` has rewritten (its `clear_derived`).

Two consumers, both named in contract 06 §3.4's table.

1. agent-registry CI cannot read a ledger on the LAN, so it needs the
   `managerd` version in a committed file to know which agent-control to
   check out when it validates family files. `live-manifest.json` is that
   file, and its human writer is what this replaces.
2. The one view reads the ledger directly and needs nothing from here.

Three rules this module keeps.

1. **Read only, and only `done/`.** Nothing here writes into the spool, and
   nothing here decides anything: the ledger informs (§2.6).
2. **The newest SUCCEEDED entry wins.** A `restored`, `failed` or `refused`
   release did not change what is live, so publishing from one would name a
   version that never ran.
3. **An entry is data, not truth about this process.** It is root-written
   and therefore trustworthy about the release, and it is still parsed
   defensively: a half-written entry must not raise inside the reconcile
   loop.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from .atomic import atomic_write
from .paths import RELEASE_ROOT

#: Where root's ledger lives (`stage7-releases.md` §2.2), under root's own
#: root.
LEDGER_DIR: Final = RELEASE_ROOT / "releases" / "done"

#: What contract 06 §3.4 calls the file, in the registry checkout.
LIVE_MANIFEST_FILE: Final = "live-manifest.json"

#: Group readable, like every other file the reconciler publishes.
FILE_MODE: Final = 0o644

#: One ledger entry carries a whole resolved manifest and a 200 line tail.
MAX_ENTRY_BYTES: Final = 1 << 20

#: How many entries one pass reads, newest first. `done/` grows by one per
#: release and is never pruned here, so the walk is bounded like every
#: other walk root's neighbours perform.
MAX_ENTRIES_SCANNED: Final = 500

SUCCEEDED: Final = "succeeded"

#: The contract whose version CI needs beside the `managerd` version, so a
#: validator can refuse a family file written against a newer one.
FAMILY_FILE_CONTRACT: Final = "family-file"


@dataclass(frozen=True)
class LiveManifest:
    """Contract 06 §3.4's four fields, and nothing else."""

    managerd: str
    family_file_contract: str
    released_at: float
    release_id: str

    def as_dict(self) -> dict[str, object]:
        return {
            "managerd": self.managerd,
            "family_file_contract": self.family_file_contract,
            "released_at": self.released_at,
            "release_id": self.release_id,
        }


def publish(checkout: Path, ledger_dir: Path = LEDGER_DIR) -> LiveManifest | None:
    """Write `live-manifest.json` into the registry checkout.

    None means there is nothing to publish yet: no succeeded release, or a
    succeeded release that moved no component `managerd` speaks for. This
    function only produces the file — and **no caller commits or pushes it,
    because there is no caller** (the module docstring says what that costs
    and what would close it).
    """
    found = newest_live(ledger_dir)
    if found is None:
        return None

    body = json.dumps(found.as_dict(), indent=2, sort_keys=True) + "\n"
    atomic_write(checkout / LIVE_MANIFEST_FILE, body.encode("utf-8"), mode=FILE_MODE)

    return found


def newest_live(ledger_dir: Path = LEDGER_DIR) -> LiveManifest | None:
    """The newest succeeded release's `managerd` version, or None.

    Entry file names are ULIDs, which sort by time, so newest first is a
    reverse sort of the names and needs no clock.
    """
    for path in _entries(ledger_dir):
        entry = _read(path)
        if entry is None or entry.get("status") != SUCCEEDED:
            continue

        found = _manifest_of(entry)
        if found is not None:
            return found

    return None


def _entries(ledger_dir: Path) -> list[Path]:
    try:
        names = sorted(
            (one for one in ledger_dir.iterdir() if one.name.endswith(".json")),
            key=lambda one: one.name,
            reverse=True,
        )
    except OSError:
        return []

    return names[:MAX_ENTRIES_SCANNED]


def _read(path: Path) -> dict[str, object] | None:
    try:
        if path.is_symlink() or path.stat().st_size > MAX_ENTRY_BYTES:
            return None

        loaded: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None

    if not isinstance(loaded, dict):
        return None

    return {str(key): value for key, value in cast("dict[object, object]", loaded).items()}


def _manifest_of(entry: dict[str, object]) -> LiveManifest | None:
    """§3.4's four values, copied out of one ledger entry.

    Every one is read from the resolved manifest root computed, never from
    a field a requester wrote: `to_version` off the `managerd` row, the
    contract version off the row that PROVIDES `family-file`, and the two
    identifiers off the document itself.
    """
    document = entry.get("manifest")
    if not isinstance(document, dict):
        return None

    fields = cast("dict[str, object]", document)
    version = _version_of(fields, "managerd")
    if version is None:
        return None

    stamp = fields.get("resolved_at")

    return LiveManifest(
        managerd=version,
        family_file_contract=_contract_version(fields) or "",
        released_at=float(stamp) if isinstance(stamp, int | float) else 0.0,
        release_id=str(entry.get("id", "")),
    )


def _version_of(document: dict[str, object], name: str) -> str | None:
    rows = document.get("components")
    if not isinstance(rows, list):
        return None

    for row in cast("list[object]", rows):
        if not isinstance(row, dict):
            continue

        fields = cast("dict[str, object]", row)
        if fields.get("name") == name and isinstance(fields.get("to_version"), str):
            return str(fields["to_version"])

    return None


def _contract_version(document: dict[str, object]) -> str | None:
    rows = document.get("contracts")
    if not isinstance(rows, list):
        return None

    for row in cast("list[object]", rows):
        if not isinstance(row, dict):
            continue

        fields = cast("dict[str, object]", row)
        if fields.get("contract") == FAMILY_FILE_CONTRACT:
            return str(fields.get("version", ""))

    return None
