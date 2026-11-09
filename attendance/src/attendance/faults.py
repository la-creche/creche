"""The fault file this service writes (contract 05 §3.3.1).

Two of contract 05 §3.3's faults are detected here (§3.3.1):
`vmm_protocol_mismatch` and `audit_unreadable`. The vmm detects the start
faults of a VM and writes its own file (decision CH21-1). `attendance` never
asks `caregiver` for anything. It reports what it sees in a file.

The file holds this writer's current open faults for one family. It is not an
event log. An empty list clears every fault this writer raised.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from .atomic import DIR_MODE_GROUP, MODE_GROUP_READ, TEMP_SUFFIX, ensure_dir, write_json
from .clock import now, rfc3339
from .ids import is_family
from .paths import fault_file, faults_dir

FAULT_SOURCE = "attendance"

_FAULT_SUFFIX = ".json"


class FaultCode(StrEnum):
    """The codes this service detects (contract 05 §3.3)."""

    #: Each of the 3 tries of a vmm call got the mismatch reply (decisions
    #: CH25 rule 1, CH33 rule 10). A vmm reply with no mismatch clears it
    #: (decision CH82 rule 4).
    VMM_PROTOCOL_MISMATCH = "vmm_protocol_mismatch"
    #: The PEP's audit cannot be read, so `waiting-approval` is invisible
    #: (contract 05 §3.3). Turns still run: the tail is a
    #: label on a state, never a gate on one.
    AUDIT_UNREADABLE = "audit_unreadable"


# Contract 05 §3.3's table fixes this per code. It is not a caller's choice.
# No fault of this writer blocks turns (decision CH82 rule 3).
_BLOCKS_TURNS: dict[FaultCode, bool] = {
    FaultCode.VMM_PROTOCOL_MISMATCH: False,
    FaultCode.AUDIT_UNREADABLE: False,
}


@dataclass(slots=True)
class Fault:
    """One open fault (contract 05 §3.3's fault object)."""

    code: FaultCode
    since: datetime
    message: str | None = None

    def to_file(self) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "code": self.code.value,
            "blocks_turns": _BLOCKS_TURNS[self.code],
            "since": rfc3339(self.since),
            "source": FAULT_SOURCE,
        }

        if self.message is not None:
            entry["message"] = self.message

        return entry


class FaultReporter:
    """Keeps one file per family in step with this service's open faults."""

    def __init__(self, state_root: Path) -> None:
        self._state_root = state_root
        self._open: dict[str, dict[FaultCode, Fault]] = {}

    def start(self) -> None:
        """Clear every fault this writer raised (contract 05 §3.3.1 rule 8).

        A fresh process has observed no fault. Each fault file in this
        writer's directory gets an empty list, whether or not its family
        still exists. A temporary file that a crash left is removed. The
        vmm's `FaultBook::start` does the same in its own directory.
        """
        self._open = {}
        directory = faults_dir(self._state_root)
        ensure_dir(directory, DIR_MODE_GROUP)

        # The list is read whole first: each write below makes a temporary
        # file in the same directory, and a lazy read could list it.
        for entry in sorted(directory.iterdir()):
            name = entry.name

            if name.startswith(".") and name.endswith(TEMP_SUFFIX):
                entry.unlink()
            elif name.endswith(_FAULT_SUFFIX) and is_family(name.removesuffix(_FAULT_SUFFIX)):
                self._publish(name.removesuffix(_FAULT_SUFFIX))

    def raise_fault(
        self,
        family: str,
        code: FaultCode,
        message: str | None = None,
    ) -> None:
        """Record a fault. `since` keeps the first sighting, not the latest."""
        family_faults = self._open.setdefault(family, {})
        existing = family_faults.get(code)
        since = existing.since if existing is not None else now()
        family_faults[code] = Fault(code=code, since=since, message=message)
        self._publish(family)

    def clear(self, family: str, code: FaultCode) -> None:
        """Drop one fault. The file is rewritten whole (contract 05 §3.3.1)."""
        family_faults = self._open.get(family)

        if family_faults is None or code not in family_faults:
            return

        kept = family_faults.pop(code)

        try:
            self._publish(family)
        except BaseException:
            # The file still holds the fault. Without the fault in memory,
            # the next clear finds nothing to drop and writes nothing.
            family_faults[code] = kept
            raise

    def open_faults(self, family: str) -> list[Fault]:
        return sorted(self._open.get(family, {}).values(), key=lambda fault: fault.code.value)

    def _publish(self, family: str) -> None:
        # The one reader is `caregiver`, which gets the file by group. The
        # writing service owns it, the same pattern as the audit (contract 04 §6).
        ensure_dir(faults_dir(self._state_root), DIR_MODE_GROUP)
        payload: dict[str, Any] = {
            "family": family,
            "source": FAULT_SOURCE,
            "written_at": rfc3339(now()),
            "faults": [fault.to_file() for fault in self.open_faults(family)],
        }
        write_json(fault_file(self._state_root, family), payload, MODE_GROUP_READ)


def blocks_turns(code: FaultCode) -> bool:
    """Whether contract 05 §3.3 says this fault stops new turns."""
    return _BLOCKS_TURNS[code]
