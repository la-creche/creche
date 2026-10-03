"""The fault file this service writes (contract 05 §3.3.1).

Five of contract 05 §3.3's faults are detected here (§3.3.1):
`protocol_mismatch`, `protocol_violation`, `orphan_processes`,
`sandbox_start_failed` and `audit_unreadable`. `managerd` also reports
`sandbox_start_failed`, for a sandbox its own apply could not create.
`attendance` never asks `managerd` for anything. It
reports what it sees in a file, and `managerd` decides whether to replace a
sandbox.

The file holds this writer's current open faults for one family. It is not an
event log. An empty list clears every fault this writer raised.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from .atomic import DIR_MODE_GROUP, MODE_GROUP_READ, ensure_dir, write_json
from .clock import now, rfc3339
from .paths import fault_file, faults_dir

FAULT_SOURCE = "sessiond"


class FaultCode(StrEnum):
    """The codes this service detects (contract 05 §3.3)."""

    PROTOCOL_MISMATCH = "protocol_mismatch"
    PROTOCOL_VIOLATION = "protocol_violation"
    ORPHAN_PROCESSES = "orphan_processes"
    # Contract 05 §3.3.1: raised when the playpen answers contract 03
    # §5.7's `fatal` instead of `ready`. Contract 05 §4.2 defines `ready` as
    # "the channel handshake passed", which only this service runs.
    SANDBOX_START_FAILED = "sandbox_start_failed"
    #: The PEP's audit cannot be read, so `waiting-approval` is invisible
    #: (contract 05 §3.3). Turns still run: the tail is a
    #: label on a state, never a gate on one.
    AUDIT_UNREADABLE = "audit_unreadable"


# Contract 05 §3.3's table fixes this per code. It is not a caller's choice.
_BLOCKS_TURNS: dict[FaultCode, bool] = {
    FaultCode.PROTOCOL_MISMATCH: True,
    FaultCode.PROTOCOL_VIOLATION: True,
    FaultCode.ORPHAN_PROCESSES: False,
    FaultCode.SANDBOX_START_FAILED: True,
    FaultCode.AUDIT_UNREADABLE: False,
}


@dataclass(slots=True)
class Fault:
    """One open fault (contract 05 §3.3's fault object)."""

    code: FaultCode
    since: datetime
    message: str | None = None
    sandbox: str | None = None

    def to_file(self) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "code": self.code.value,
            "blocks_turns": _BLOCKS_TURNS[self.code],
            "since": rfc3339(self.since),
            "source": FAULT_SOURCE,
        }

        if self.message is not None:
            entry["message"] = self.message

        if self.sandbox is not None:
            entry["sandbox"] = self.sandbox

        return entry


class FaultReporter:
    """Keeps one file per family in step with this service's open faults."""

    def __init__(self, state_root: Path) -> None:
        self._state_root = state_root
        self._open: dict[str, dict[FaultCode, Fault]] = {}

    def raise_fault(
        self,
        family: str,
        code: FaultCode,
        message: str | None = None,
        sandbox: str | None = None,
    ) -> None:
        """Record a fault. `since` keeps the first sighting, not the latest."""
        family_faults = self._open.setdefault(family, {})
        existing = family_faults.get(code)
        since = existing.since if existing is not None else now()
        family_faults[code] = Fault(code=code, since=since, message=message, sandbox=sandbox)
        self._publish(family)

    def clear(self, family: str, code: FaultCode) -> None:
        """Drop one fault. The file is rewritten whole (contract 05 §3.3.1)."""
        family_faults = self._open.get(family)

        if family_faults is None or code not in family_faults:
            return

        del family_faults[code]
        self._publish(family)

    def clear_all(self, family: str) -> None:
        """Clear every fault this service raised for the family."""
        self._open[family] = {}
        self._publish(family)

    def open_faults(self, family: str) -> list[Fault]:
        return sorted(self._open.get(family, {}).values(), key=lambda fault: fault.code.value)

    def _publish(self, family: str) -> None:
        # The one reader is `managerd`, which gets the file by group. The
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
