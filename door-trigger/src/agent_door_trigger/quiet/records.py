"""The two host records the gate reads beside the chaperone.

1. `attendance`'s outcome record (contract 02 §13.1): how the wake the gate
   let through ended. Written by temp file and rename before the session is
   deleted, so a session gone with no record is a job that vanished.
2. The chaperone's audit file: whether the family made its daily call. One
   append-only JSONL file, `/var/lib/chaperone/audit.jsonl` on the host,
   mode 0640 for the group `chaperone` (chaperone `docs/design.md` §7.6).
   Each line is one record of one call (§5.4). The records of one call share
   a run ID and a call number. A call that ran writes one `ran` record.

Neither read raises. What cannot be read answers the value the gate wakes on.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol

from agent_family import CALL_SEPARATOR, Verb

from ..untrusted import as_object, field_text
from .chaperone import ATTENDANCE

_LOG = logging.getLogger(__name__)

#: Contract 02 §13.1's `status` for a job that ended cleanly.
STATUS_OK: Final = "ok"

#: An outcome record is a few hundred bytes. Anything far larger is not one.
MAX_OUTCOME_BYTES: Final = 64 * 1024

#: How much of the end of the audit file one check reads. A call older than
#: this part is not found, and the gate wakes.
MAX_TAIL_BYTES: Final = 4 * 1024 * 1024

#: A Rust line is about 400 bytes: no field holds free text.
MAX_LINE_BYTES: Final = 4096

# CONTRACT-QUESTION: no contract of this repository names the line of the
# audit file. Reading taken: the Rust type `Line` and the function
# `event_parts` of `plugins/default/audit-jsonl` in the chaperone repository
# are the shape. A line with another key is not read, and the gate wakes.

#: The nine keys of the Rust type `Line`.
AUDIT_KEYS: Final = frozenset(
    {"run", "call", "started", "time", "app", "tool", "arguments_sha256", "event", "detail"}
)

#: The event and the detail of a call that ran with no error.
RAN: Final = "ran"
RAN_CLEAN: Final = "is_error:false"

#: A daily call that names a verb is one tool of one upstream. `embed` and
#: `ha_call` have no tool, so the audit never holds them.
VERB_TOOLS: Final[Mapping[str, str]] = {
    Verb.ENQUEUE: f"{ATTENDANCE}{CALL_SEPARATOR}{Verb.ENQUEUE}",
    Verb.JOB_STATUS: f"{ATTENDANCE}{CALL_SEPARATOR}{Verb.JOB_STATUS}",
    Verb.RELEASE: f"release{CALL_SEPARATOR}request",
}


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
    """`Records` over the outcome directory and the audit file of this host."""

    def __init__(self, outcomes_root: Path, audit_file: Path) -> None:
        self._outcomes = outcomes_root
        self._audit = audit_file

    def ending(self, family: str, session: str, since: datetime) -> Ending:
        for body in self._outcomes_since(self._outcomes / family, since):
            if field_text(body, "session") != session:
                continue

            return Ending.OK if field_text(body, "status") == STATUS_OK else Ending.FAILED

        return Ending.PENDING

    def called(self, family: str, call: str, since: datetime, until: datetime) -> Called:
        # CONTRACT-QUESTION: the chaperone's design names no reader of the
        # audit file. This is a second one beside the noticeboard, reading
        # records by tool name. A record holds an argument digest, never an
        # argument.
        tool = call if CALL_SEPARATOR in call else VERB_TOOLS.get(call, call)
        try:
            lines = _tail(self._audit)
        except FileNotFoundError:
            # No file: the chaperone never audited a call.
            return Called.NO
        except OSError as exc:
            _LOG.warning("quiet: cannot read the audit %s (%s)", self._audit, exc.strerror)
            return Called.UNKNOWN

        for record in filter(None, map(_audit_line, lines)):
            if record.ran_cleanly(family, tool) and record.time >= since:
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


@dataclass(frozen=True, slots=True)
class AuditLine:
    """The fields of one audit line that the gate reads."""

    run: str
    call: int
    time: datetime
    app: str | None
    tool: str
    event: str
    detail: str

    def ran_cleanly(self, family: str, tool: str) -> bool:
        """Whether this is the `ran` record of a call of `tool` by `family`
        that ran with no error. One call writes one `ran` record at most."""
        return (
            self.app == family
            and self.tool == tool
            and self.event == RAN
            and self.detail == RAN_CLEAN
        )


def _tail(path: Path) -> list[bytes]:
    """The complete lines of the end of the file. Raises OSError."""
    with path.open("rb") as handle:
        size = handle.seek(0, 2)
        start = max(0, size - MAX_TAIL_BYTES)
        handle.seek(start)
        raw = handle.read(MAX_TAIL_BYTES)

    pieces = raw.split(b"\n")
    if start:
        _LOG.warning("quiet: read the newest %d bytes of the audit, of %d", MAX_TAIL_BYTES, size)
        # The seek lands inside a line, so the first piece goes.
        pieces = pieces[1:]

    # The sink writes a line and its LF in one write. The piece after the
    # last LF is a write in progress, or nothing. LF is the only delimiter:
    # U+2028 is legal inside a JSON string.
    return [one for one in pieces[:-1] if one.strip()]


def _audit_line(raw: bytes) -> AuditLine | None:
    """The line, when it has the nine keys of `Line` and the gate's fields
    have their Rust types. Anything else is None."""
    if len(raw) > MAX_LINE_BYTES:
        return None

    try:
        body = as_object(json.loads(raw))
        time = datetime.fromisoformat(field_text(body, "time"))
    except (ValueError, RecursionError):
        # RecursionError: a line that nests too deep is not a ValueError.
        return None

    run, call, app = body.get("run"), body.get("call"), body.get("app")
    if (
        frozenset(body) != AUDIT_KEYS
        or time.tzinfo is None
        or not isinstance(run, str)
        or type(call) is not int
        or not (app is None or isinstance(app, str))
    ):
        return None

    return AuditLine(
        run=run,
        call=call,
        time=time,
        app=app,
        tool=field_text(body, "tool"),
        event=field_text(body, "event"),
        detail=field_text(body, "detail"),
    )
