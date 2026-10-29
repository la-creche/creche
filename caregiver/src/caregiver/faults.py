"""Fault files another service writes (contract 05 section 3.3, section
3.3.1). `attendance` and the PEP see faults `caregiver` cannot see for itself
and report them in a file, never a call.

`read_fault_file` is the reader. Every reconcile pass reads the files
through `steps.read_faults` and folds the result into the status
document."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast

from .atomic import read_json

log = logging.getLogger("caregiver.faults")

#: Contract 05 section 3.3.1: which service may raise which code. caregiver
#: itself raises the other six codes of section 3.3's table directly, from
#: its own reconcile loop, never through a fault file.
FAULT_CODES_BY_SOURCE: Final[dict[str, frozenset[str]]] = {
    "attendance": frozenset(
        {
            "protocol_mismatch",
            "protocol_violation",
            "orphan_processes",
            "audit_unreadable",
            # The fourth code section 3.3.1 names for this writer, and the one
            # code two services may report. `attendance` raises it for a sandbox
            # whose playpen answered `fatal` (contract 03 section 5.7), so
            # the handshake it runs can never pass.
            "sandbox_start_failed",
        }
    ),
    "pep": frozenset({"grants_stale"}),
}

#: Section 3.3 defines this one code FLEET-wide -- "no sandbox reached
#: `ready` after the retry budget" -- while `attendance` can only ever see one
#: sandbox at a time. `rescope_by_fleet` closes that gap.
FLEET_WIDE_CODE: Final = "sandbox_start_failed"

#: The smallest fleet in which one sandbox failing still leaves another to
#: serve. Below it, the fleet-wide code means what it says.
FLEET_WITH_A_SPARE: Final = 2

#: Contract 05 section 3.3's table, fixed per code. The reader sets this
#: from the code, never from the untrusted file's own claim (00-common-
#: rules "Everything that crosses a process boundary is untrusted input").
BLOCKS_TURNS_BY_CODE: Final[dict[str, bool]] = {
    "protocol_mismatch": True,
    "protocol_violation": True,
    "grants_stale": True,
    "sandbox_start_failed": True,
    "orphan_processes": False,
    "audit_unreadable": False,
}

#: Contract 05 section 3.3.1 rule 7.
FAULT_STALE_AFTER_S: Final = 90

_KNOWN_KEYS: Final = frozenset({"code", "blocks_turns", "since", "source", "stale"})


@dataclass(frozen=True)
class FaultEntry:
    """Section 3.3's fault object. `detail` carries whatever extra keys the
    writer sent (`attempts`, `message`, `sandbox`, `rev`, ...) -- "a reader
    ignores a key it does not know", not "a reader refuses one"."""

    code: str
    blocks_turns: bool
    since: str
    source: str
    stale: bool = False
    detail: dict[str, Any] = field(default_factory=dict[str, Any])

    def as_json(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "blocks_turns": self.blocks_turns,
            "since": self.since,
            "source": self.source,
            "stale": self.stale,
            **self.detail,
        }


@dataclass(frozen=True)
class FaultFile:
    family: str
    source: str
    written_at: str
    faults: tuple[FaultEntry, ...]


def read_fault_file(path: Path, source: str, *, now: datetime | None = None) -> FaultFile | None:
    """`None` when the file is absent -- "a missing file means that writer
    raised nothing" (rule 5) -- or unreadable or malformed. A single bad
    entry is dropped, never the whole file: this is input from another
    process, validated the way contract 03 section 13 validates a
    playpen line, not trusted the way this service's own state is."""
    body = read_json(path)
    if body is None:
        return None

    family = body.get("family")
    written_at = body.get("written_at")
    raw_faults = body.get("faults")
    if not isinstance(family, str) or not isinstance(written_at, str):
        return None

    if not isinstance(raw_faults, list):
        return None

    stale = _is_stale(written_at, now)
    allowed = FAULT_CODES_BY_SOURCE.get(source, frozenset())
    faults: list[FaultEntry] = []
    for raw in cast("list[Any]", raw_faults):
        entry = _one_fault(raw, source, allowed, stale=stale)
        if entry is not None:
            faults.append(entry)

    return FaultFile(family=family, source=source, written_at=written_at, faults=tuple(faults))


def rescope_by_fleet(
    faults: tuple[FaultEntry, ...], live_sandboxes: tuple[str, ...]
) -> tuple[FaultEntry, ...]:
    """Decide `blocks_turns` for the one code section 3.3 defines fleet-wide.

    `sandbox_start_failed` reads "no sandbox reached `ready` after the retry
    budget", but `attendance` raises it per SANDBOX. During a make-before-break
    replacement (section 5) the entry names the INCOMING sandbox while the
    outgoing one serves every turn, and section 5.3 promises the family keeps
    serving. Taking `blocks_turns` from the code alone would stop it.

    So: the fault stops turns only while it names the family's ONLY live
    sandbox. Nothing is hidden either way -- the entry, its message and its
    sandbox reach the status document unchanged, and the noticeboard still shows it.

    Every other code keeps the value section 3.3's table fixes.
    """
    if len(live_sandboxes) < FLEET_WITH_A_SPARE:
        return faults

    return tuple(_downgraded(one, live_sandboxes) for one in faults)


def _downgraded(entry: FaultEntry, live_sandboxes: tuple[str, ...]) -> FaultEntry:
    named = entry.detail.get("sandbox")
    if entry.code != FLEET_WIDE_CODE or named not in live_sandboxes:
        return entry

    return replace(entry, blocks_turns=False)


def _one_fault(raw: Any, source: str, allowed: frozenset[str], *, stale: bool) -> FaultEntry | None:
    if not isinstance(raw, dict):
        return None

    raw = cast("dict[str, Any]", raw)
    code = raw.get("code")
    since = raw.get("since")
    if not isinstance(code, str) or code not in allowed or not isinstance(since, str):
        # Rule 6: an unknown code, or a code belonging to a different
        # writer, is dropped. A code this source may not raise is the same
        # refusal as one section 3.3 never defined at all.
        return None

    detail = {key: value for key, value in raw.items() if key not in _KNOWN_KEYS}
    return FaultEntry(
        code=code,
        blocks_turns=BLOCKS_TURNS_BY_CODE[code],
        since=since,
        source=source,
        stale=stale,
        detail=detail,
    )


def _is_stale(written_at: str, now: datetime | None) -> bool:
    try:
        written = datetime.fromisoformat(written_at)
    except ValueError:
        return True  # unparseable is untrusted; treat it as already stale

    current = now if now is not None else datetime.now(UTC)
    if written.tzinfo is None:
        written = written.replace(tzinfo=UTC)

    return (current - written).total_seconds() > FAULT_STALE_AFTER_S
