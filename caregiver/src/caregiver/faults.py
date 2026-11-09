"""Fault files another service writes (contract 05 section 3.3, section
3.3.1). `attendance`, the PEP and the vmm see faults `caregiver` cannot see
for itself and report them in a file, never a call.

`read_fault_file` is the reader. Every reconcile pass reads the files
through `steps.read_faults` and folds the result into the status
document."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast

from .atomic import read_json

log = logging.getLogger("caregiver.faults")

#: Contract 05 section 3.3.1: which service may raise which code. The vmm
#: is the third writer since minor 18 (decision CH21-1). caregiver itself
#: raises the other codes of section 3.3's table directly, from its own
#: reconcile loop, never through a fault file.
FAULT_CODES_BY_SOURCE: Final[dict[str, frozenset[str]]] = {
    # Each of 3 tries of a vmm call got the mismatch reply (decisions CH25
    # rule 1, CH33 rule 10), or the PEP's audit does not read.
    "attendance": frozenset({"vmm_protocol_mismatch", "audit_unreadable"}),
    "pep": frozenset({"grants_stale"}),
    # The two start faults of decision CH6-3 and the serial log cap of
    # decisions CH79 rule 7 and CH82 rule 1.
    "vmm": frozenset({"sandbox_start_failed", "egress_assert_failed", "serial_log_full"}),
}

#: The writers of section 3.3.1, in the order a pass folds their files.
FAULT_SOURCES: Final = tuple(FAULT_CODES_BY_SOURCE)

#: Contract 05 section 3.3's table, fixed per code. The reader sets this
#: from the code, never from the untrusted file's own claim (00-common-
#: rules "Everything that crosses a process boundary is untrusted input").
#: No vmm fault and no `vmm_protocol_mismatch` stops turns: the next turn
#: tries a start, and each start reruns every check, which fails closed
#: (decision CH82 rules 2, 3 and 6).
BLOCKS_TURNS_BY_CODE: Final[dict[str, bool]] = {
    "vmm_protocol_mismatch": False,
    "audit_unreadable": False,
    "grants_stale": True,
    "sandbox_start_failed": False,
    "egress_assert_failed": False,
    "serial_log_full": False,
}

#: Contract 05 section 3.3.1 rule 7.
FAULT_STALE_AFTER_S: Final = 90

_KNOWN_KEYS: Final = frozenset({"code", "blocks_turns", "since", "source", "stale"})


@dataclass(frozen=True)
class FaultEntry:
    """Section 3.3's fault object. `detail` carries whatever extra keys the
    writer sent (`message`, `rev`, ...) -- "a reader
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
