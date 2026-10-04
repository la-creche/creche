"""Audit record v2, the family path's own log (contract 04 §6).

```
/srv/agents/state/rework/audit/YYYY-MM-DD.jsonl
```

One file per UTC day, appended with `O_APPEND`, 0640 inside a 0750 directory.
Every decision is recorded, allows and denials alike (invariant 15). The PEP
serves no read endpoint for it: the noticeboard reads the files, and an endpoint
would be new surface on the process that holds every upstream credential.

A request that names no family goes to the unidentified log instead, at its
own path and in its own shape (`app.py`). Nothing here touches it, and a
reader never has to work out which schema a line follows, because the paths
differ.

The trusted fields (§6.1) and the claimed ones (§6.2) are separate objects in
the line. The separation is structural, so a reader cannot mix them up by
accident (§3.1 rule 3).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Final, cast

from .audit import AuditError, AuditLog
from .family_ids import rfc3339_ms
from .headers import NO_CLAIMS, Claimed

#: Contract 04 §6's table. The directory is set explicitly so the process
#: umask cannot widen it; `AuditLog` already pins 0640 on the files.
AUDIT_DIR_MODE: Final = 0o750

#: Per-string cap on an audited `args` value. §6.1 says `args` holds the full
#: arguments, and it does: structure and keys are untouched and nothing is
#: dropped. An oversized string is truncated instead, because 60 calls a
#: minute of 256 KiB bodies is 15 MB of audit a minute, kept 180 days.
AUDIT_ARG_STRING_MAX_CHARS: Final = 8 * 1024
AUDIT_TRUNCATION_MARKER: Final = "...<truncated>"

#: What a record holds in place of arguments that no line can hold. A
#: string that is not Unicode text has no UTF-8 form, and this writer walks
#: only as deep as the interpreter recurses. The record itself stays: each
#: other field is a value of the PEP.
#:
#: CONTRACT-QUESTION: §6.1 says that `args` holds the full arguments, and
#: it is silent on arguments that no JSON line in UTF-8 can hold. The
#: reading here: the record holds this marker, and `family_app` refuses the
#: call before any effect. A change costs each reader of the audit.
UNRECORDABLE_ARGS: Final[dict[str, object]] = {"$unrecordable": True}

#: Levels that `can_hold` keeps free under the arguments. The write of a
#: line starts some frames deeper in the stack than the check before the
#: effect, and `truncate_strings` takes one frame for each level. Without
#: this reserve the check passes arguments that the write cannot walk.
HOLD_RESERVE_LEVELS: Final = 32


class Outcome(Enum):
    """Contract 04 §6.1's `decision` column. `PENDING` is the first of the two
    records a gated call writes (§6.4)."""

    ALLOW = "allow"
    DENY = "deny"
    PENDING = "pending"


@dataclass(frozen=True)
class Sandbox:
    """Which sandbox made the call, and whether that is evidence (§3.2)."""

    id: str | None = None
    trusted: bool = False


UNKNOWN_SANDBOX: Final = Sandbox()


def resolve_sandbox(peer: str | None) -> Sandbox:
    """Map a connection's source address to the sandbox that owns it.

    The seam for contract 04 §9 open point 1. Probe 0a did not measure whether
    one sandbox holds one source address, so the PEP maps nothing and every
    record says so in `sandbox_id_trusted`. One caller, so the measurement
    lands here.
    """
    del peer  # nothing to map it against yet
    return UNKNOWN_SANDBOX


def truncate_strings(value: object) -> object:
    """Cap string values recursively, so one huge value cannot blow up a line.
    Structure and keys are untouched, and nothing is dropped."""
    if isinstance(value, str):
        if len(value) <= AUDIT_ARG_STRING_MAX_CHARS:
            return value
        return value[:AUDIT_ARG_STRING_MAX_CHARS] + AUDIT_TRUNCATION_MARKER

    if isinstance(value, dict):
        return {k: truncate_strings(v) for k, v in cast("dict[str, object]", value).items()}

    if isinstance(value, list):
        return [truncate_strings(v) for v in cast("list[object]", value)]

    return value


def can_hold(args: dict[str, object]) -> bool:
    """Whether a line can hold these arguments in full, apart from the cap
    on a string: they are JSON text in UTF-8, at a depth this writer walks.

    The whole arguments are checked, not what the cap leaves of them. An
    effect gets the whole arguments.

    The check walks `HOLD_RESERVE_LEVELS` more levels than the arguments
    have. A write from a deeper frame then walks what this check passed.
    """
    deeper: object = args
    for _ in range(HOLD_RESERVE_LEVELS):
        deeper = [deeper]

    try:
        truncate_strings(deeper)
        json.dumps(deeper, ensure_ascii=False).encode("utf-8")
    except (RecursionError, ValueError):
        return False

    return True


@dataclass(frozen=True)
class AuditEntry:
    """One decision, ready to write. Everything above `claimed` is trusted."""

    family: str
    tool: str
    args: dict[str, object]
    outcome: Outcome
    reason: str
    grants_rev: str | None = None
    sandbox: Sandbox = UNKNOWN_SANDBOX
    latency_ms: int | None = None
    waited_ms: int = 0
    gate: str | None = None
    claimed: Claimed = NO_CLAIMS
    #: §6.3: family names, caller first, this family last. Empty means the PEP
    #: minted no delegation id for this call, which is a chain of one.
    chain: tuple[str, ...] = field(default_factory=tuple[str, ...])


class FamilyAudit:
    """The v2 log. Composes `AuditLog` rather than extending it: the retention
    sweep, the `O_APPEND` write and the 0640 mode are the same proven
    mechanics, and only the record shape and the directory mode differ."""

    def __init__(self, directory: Path) -> None:
        self._log = AuditLog(directory)
        os.chmod(directory, AUDIT_DIR_MODE)

    def probe_writable(self) -> bool:
        return self._log.probe_writable()

    def sweep_retention(self) -> None:
        self._log.sweep_retention()

    def write(self, entry: AuditEntry) -> None:
        """Append one line. Raises `AuditError` when it cannot be written, and
        the app layer turns that into a 500 even for an allowed call whose
        effect already happened — unrecorded effects are worse.

        Arguments that no line can hold do not cost the record: the line
        then holds `UNRECORDABLE_ARGS` in their place.

        Only a denial can end with that line. `family_app` refuses such a
        call before its effect and before its gate (`can_hold`). If one
        still comes here with another decision, the line is written and
        the write fails: no call with an effect ends well with a line that
        does not hold its arguments."""
        try:
            self._append(entry, truncate_strings(entry.args))
        except (RecursionError, ValueError):
            self._append(entry, UNRECORDABLE_ARGS)
            if entry.outcome is not Outcome.DENY:
                # Not the text of the failure: it can quote the arguments.
                raise AuditError(
                    f"the audit line of {entry.tool} for {entry.family} "
                    f"({entry.outcome.value}) does not hold its arguments"
                ) from None

    def _append(self, entry: AuditEntry, args: object) -> None:
        chain = entry.chain or (entry.family,)
        record: dict[str, object] = {
            # `AuditLog.write` merges its own `ts` first and lets the record's
            # own key win. §6.1 wants RFC 3339 with milliseconds and a `Z`,
            # which `datetime.isoformat` does not write.
            "ts": rfc3339_ms(datetime.now(UTC)),
            "family": entry.family,
            "sandbox_id": entry.sandbox.id,
            "sandbox_id_trusted": entry.sandbox.trusted,
            "grants_rev": entry.grants_rev,
            "tool": entry.tool,
            "args": args,
            "decision": entry.outcome.value,
            "reason": entry.reason,
            "latency_ms": entry.latency_ms,
            "waited_ms": entry.waited_ms,
            "gate": entry.gate,
            "claimed": entry.claimed.as_record(),
            "chain": list(chain),
        }
        self._log.write(record)
