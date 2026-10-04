"""The two host records the gate reads beside the PEP.

1. `attendance`'s outcome record (contract 02 §13.1): how the wake the gate
   let through ended. Written by temp file and rename before the session is
   deleted, so a session gone with no record is a job that vanished.
2. The PEP's audit (contract 04 §6): whether the family made its daily call.
   One UTC day per file, `chaperone:agents` 0640, so this door reads it through
   the operator's `agents` group.

Neither read raises. What cannot be read answers the value the gate wakes on.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol

from ..untrusted import as_object, field_text

_LOG = logging.getLogger(__name__)

#: Contract 02 §13.1's `status` for a job that ended cleanly.
STATUS_OK: Final = "ok"

#: An outcome record is a few hundred bytes. Anything far larger is not one.
MAX_OUTCOME_BYTES: Final = 64 * 1024

#: Contract 04 §6.1: an allowed call that ran, plainly or after a tap.
#: `upstream_failed` is also `allow`, and it is a call that did NOT happen.
DECISION_ALLOW: Final = "allow"
RAN_REASONS: Final = frozenset({"granted", "approved"})

AUDIT_SUFFIX: Final = ".jsonl"
ONE_DAY: Final = timedelta(days=1)


class Ending(StrEnum):
    """How a job the gate let through ended, as far as the record says."""

    OK = "ok"
    FAILED = "failed"
    PENDING = "pending"


class Called(StrEnum):
    """Whether the audit holds the call."""

    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"


class Records(Protocol):
    """What the gate reads from the host's own records."""

    def ending(self, family: str, session: str, since: datetime) -> Ending:
        """How `session` ended. `since` is its firing: no record is older."""
        ...

    def called(self, family: str, call: str, since: datetime, until: datetime) -> Called:
        """Whether the family made `call`, and it ran, in `[since, until]`."""
        ...


class HostRecords:
    """`Records` over the two directories on this host."""

    def __init__(self, outcomes_root: Path, audit_root: Path) -> None:
        self._outcomes = outcomes_root
        self._audit = audit_root

    def ending(self, family: str, session: str, since: datetime) -> Ending:
        for body in self._outcomes_since(self._outcomes / family, since):
            if field_text(body, "session") != session:
                continue

            return Ending.OK if field_text(body, "status") == STATUS_OK else Ending.FAILED

        return Ending.PENDING

    def called(self, family: str, call: str, since: datetime, until: datetime) -> Called:
        # CONTRACT-QUESTION: contract 04 §6 names the noticeboard as the audit's
        # only reader. This is a second one, under the same user, reading
        # records by tool name and never an argument.
        for day in _utc_days(since, until):
            path = self._audit / f"{day.isoformat()}{AUDIT_SUFFIX}"
            try:
                found = _day_holds(path, family, call, since)
            except FileNotFoundError:
                # No file: nothing at all was audited that UTC day.
                continue
            except OSError as exc:
                _LOG.warning("quiet: cannot read the audit %s (%s)", path.name, exc.strerror)
                return Called.UNKNOWN

            if found:
                return Called.YES

        return Called.NO

    def _outcomes_since(self, directory: Path, since: datetime) -> Iterator[dict[str, object]]:
        """Every record written at or after `since`. The file time bounds
        the read, because the directory keeps every job the family ran."""
        try:
            entries = list(directory.iterdir())
        except OSError:
            return

        floor = since.timestamp()
        for path in entries:
            try:
                stat = path.stat()
                if stat.st_mtime < floor or stat.st_size > MAX_OUTCOME_BYTES:
                    continue

                body: object = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, RecursionError):
                continue

            yield as_object(body)


def _day_holds(path: Path, family: str, call: str, since: datetime) -> bool:
    """Whether one day's audit holds the call. Raises OSError."""
    with path.open(encoding="utf-8", errors="replace") as lines:
        for line in lines:
            # Most lines name another tool. Skip them before parsing.
            if call not in line:
                continue

            if _ran(line, family, call, since):
                return True

    return False


def _ran(line: str, family: str, call: str, since: datetime) -> bool:
    try:
        record = as_object(json.loads(line))
        at = datetime.fromisoformat(field_text(record, "ts"))
    except (ValueError, RecursionError):
        # RecursionError: a line that nests too deep is not a ValueError.
        return False

    # The PEP writes UTC with a `Z`. A time with no zone cannot be placed.
    if at.tzinfo is None:
        return False

    return (
        field_text(record, "family") == family
        and field_text(record, "tool") == call
        and field_text(record, "decision") == DECISION_ALLOW
        and field_text(record, "reason") in RAN_REASONS
        and at >= since
    )


def _utc_days(since: datetime, until: datetime) -> Iterator[date]:
    """The UTC days `[since, until]` touches: one audit file each."""
    day = since.astimezone(UTC).date()
    last = until.astimezone(UTC).date()
    while day <= last:
        yield day
        day += ONE_DAY
