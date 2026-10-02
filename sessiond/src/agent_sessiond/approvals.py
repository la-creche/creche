"""Seeing that a tool call is held at the PEP (contract 04 §8.6).

Contract 04 §8.6 says the session shows `waiting-approval` while a gated
call waits for a phone tap, and contract 02 §8.1 already has the two journal
lines for it. Nothing in either contract sends `sessiond` a message when a
gate opens: the bridge's HTTP call simply blocks inside the sandbox, the
supervisor sees an agent that is still working, and the PEP holds the gate
in its own memory.

One artefact of an open gate does exist outside the PEP. §6.4 makes a gated
call write TWO audit records: `decision: pending` when it opens and a second
one with the final decision when it resolves. That file is append-only, one
per UTC day, and contract 05 §8 already names it as a reader's source. This
module reads it.

```
  PEP  --append--> audit/<day>.jsonl  --tail--> this module --> turn state
         pending                                            \
         allow | deny                                         `-> journal
```

Three rules, each with its reason:

1. **A record names a turn or it is dropped.** `family` is trusted (it comes
   from the token), `claimed.session_id` and `claimed.turn_id` are not
   (contract 04 §3.1). A record is used only when the host itself started
   that exact turn in that exact family, so a forged pair can name nothing
   the host does not already own.
2. **The file is read forward, never re-read.** It only grows, so each poll
   reads from where the last one stopped. A truncated or rotated file
   restarts at zero rather than replaying decisions.
3. **An unreadable file is a fault, never silence.** The PEP owns the file
   at mode 0640 (§6), so a host that has not put this service in the reading
   group gets `running` where it would have got `waiting-approval`. Turns
   still run — the tail is a label on a state, never a gate on one — but
   nothing said why the label never appeared. `unreadable()` answers what
   the last poll could not read, and `SessionService` raises contract 05
   §3.3's `audit_unreadable` from it. A file that is simply ABSENT is not a
   fault: the PEP may not have written one today.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from pathlib import Path

from .atomic import as_object
from .clock import now
from .models import GateOutcome
from .paths import audit_file

# Contract 04 §6.1's `decision`, the three values a record can carry.
PENDING = "pending"

# Contract 04 §8.5's limit is 15 minutes, so a gate opened just before
# midnight resolves in the next day's file. Two days always covers it.
_DAYS_READ = 2

_DAY_FORMAT = "%Y-%m-%d"

# Contract 02 §13.1.1: which counter each of §6.1's resolving reasons raises.
# `approval_undeliverable` is a denial of the call, because §8.4 makes the
# PEP "deny at once" when no phone can see the gate. `approval_revoked` and
# `approval_abandoned` decided nothing, so they are absent and read as
# `UNCOUNTED`.
_OUTCOME_OF: dict[str, GateOutcome] = {
    "approved": GateOutcome.APPROVED,
    "approval_denied": GateOutcome.DENIED,
    "approval_undeliverable": GateOutcome.DENIED,
    "approval_timeout": GateOutcome.TIMED_OUT,
}


def outcome_of(reason: str) -> GateOutcome:
    """Contract 02 §13.1.1, read from the PEP's own resolving record."""
    return _OUTCOME_OF.get(reason, GateOutcome.UNCOUNTED)


class GateState(StrEnum):
    """What one audit record says happened to a gate."""

    OPENED = "opened"
    RESOLVED = "resolved"


@dataclass(frozen=True, slots=True)
class Gate:
    """One audit record about a gated call (contract 04 §6.1, §6.4)."""

    gate: str
    family: str
    session: str
    turn: str
    tool: str
    state: GateState
    reason: str
    waited_s: int


class AuditTail:
    """Reads the PEP's audit forward and answers with the gates it named."""

    def __init__(self, state_root: Path) -> None:
        self._state_root = state_root
        self._offsets: dict[Path, int] = {}
        self._denied: str | None = None

    def poll(self) -> list[Gate]:
        """Every gate record written since the last poll, in file order."""
        found: list[Gate] = []
        self._denied = None

        for path in self._paths():
            found.extend(self._read(path))

        return found

    def unreadable(self) -> str | None:
        """Why the last poll could not read the audit, or None.

        Rule 3. A permission error is the one failure worth a fault: it is
        the host's own misconfiguration, it hides every gate, and it never
        clears on its own. A missing file answers None.
        """
        return self._denied

    def _paths(self) -> list[Path]:
        moment = now()
        days = [moment - timedelta(days=offset) for offset in reversed(range(_DAYS_READ))]
        return [audit_file(self._state_root, day.strftime(_DAY_FORMAT)) for day in days]

    def _read(self, path: Path) -> list[Gate]:
        start = self._offsets.get(path, 0)

        try:
            size = path.stat().st_size
        except PermissionError:
            # The directory denies this user, so whether the file exists is
            # not even knowable from here (contract 04 §6's 0750 directory).
            self._deny(path)
            return []
        except OSError:
            return []

        # A file that shrank was rotated or truncated. Starting over would
        # replay decisions, so this reads the new tail and nothing else.
        if size < start:
            self._offsets[path] = size
            return []

        if size == start:
            return []

        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(start)
                text = handle.read()
        except PermissionError:
            # Rule 3: the PEP owns the file at 0640, and this user is not in
            # its group. Loud, because it hides every gate for every family.
            self._deny(path)
            return []
        except OSError:
            return []

        # A torn last line is left for the next poll, which reads it whole.
        body, _, tail = text.rpartition("\n")
        self._offsets[path] = size - len(tail.encode("utf-8"))
        return _gates_in(body)

    def _deny(self, path: Path) -> None:
        """Keep the first path that refused. One message names the fix."""
        if self._denied is not None:
            return

        self._denied = f"cannot read the PEP's audit at {path}"


def _gates_in(text: str) -> list[Gate]:
    found: list[Gate] = []

    for line in text.splitlines():
        gate = _gate_of(line)

        if gate is not None:
            found.append(gate)

    return found


def _gate_of(line: str) -> Gate | None:
    """One audit line, or None when it is not about a gate this can use."""
    if not line.strip():
        return None

    try:
        parsed: object = json.loads(line)
    except ValueError:
        return None

    record = as_object(parsed)

    if record is None:
        return None

    gate = _text(record.get("gate"))
    family = _text(record.get("family"))

    if gate is None or family is None:
        return None

    claimed = as_object(record.get("claimed")) or {}
    session = _text(claimed.get("session_id"))
    turn = _text(claimed.get("turn_id"))

    if session is None or turn is None:
        return None

    decision = _text(record.get("decision")) or ""

    return Gate(
        gate=gate,
        family=family,
        session=session,
        turn=turn,
        tool=_text(record.get("tool")) or "",
        state=GateState.OPENED if decision == PENDING else GateState.RESOLVED,
        reason=_text(record.get("reason")) or "",
        waited_s=_seconds(record.get("waited_ms")),
    )


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _seconds(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0

    return value // 1000
