"""The chaperone's audit, read from its one JSONL file.

The chaperone writes one append-only file, `/var/lib/chaperone/audit.jsonl`
(`plugins/default/audit-jsonl` of the chaperone repository, `docs/design.md`
section 5.4). The file is mode 0640 and its directory is 0750, so a member
of the group `chaperone` reads it. Each line is one record of one call. The
records of one call share a run ID and a call number.

A record holds the SHA-256 of the canonical arguments, never the arguments.
The page says so in one line.

Four properties this module holds:

1. Newest first. A call sorts by its newest record in the file.
2. Bounded. The reader reads at most `MAX_TAIL_BYTES` from the end of the
   file, and it does not parse a line longer than `MAX_LINE_BYTES`. Each
   bound that bites is reported on the page.
3. LF is the only line delimiter. U+2028 and U+2029 are legal inside a JSON
   string, so a generic line reader would split one record into two.
4. Strict. A line holds the nine keys of the Rust type `Line` and no other
   key, and each value has the shape that the Rust type gives it. A line
   that fails takes a row of its own, because a hole in the timeline is
   what an audit must not have.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Final

from . import jsonfiles, rfc3339
from .jsonfiles import Json, Surface

#: One line said in every rendering of this page.
ARGS_NOTICE: Final = (
    "This page shows argument digests only: the chaperone records the SHA-256 "
    "of the arguments of each call, never the arguments."
)

#: A Rust line is about 400 bytes: no field holds free text. A line past
#: this cap is not a line of the Rust sink.
MAX_LINE_BYTES: Final = 4096

#: How much of the end of the file one request reads. Past this the older
#: records are not read, and the page says so.
MAX_TAIL_BYTES: Final = 4 * 1024 * 1024

# CONTRACT-QUESTION: no contract of this repository names the line of the
# audit file. Reading taken: the Rust type `Line` and the function
# `event_parts` of `plugins/default/audit-jsonl` in the chaperone repository
# are the shape. A new key or a new detail form of the chaperone makes each
# such line a row with a problem. A change costs the checks below and their
# tests.

#: The nine keys of the Rust type `Line`.
KEYS: Final = frozenset(
    {"run", "call", "started", "time", "app", "tool", "arguments_sha256", "event", "detail"}
)

#: A run ID and a gate ID are 16 bytes as lower-case hex.
_ID: Final = re.compile(r"[0-9a-f]{32}")

_SHA256: Final = re.compile(r"[0-9a-f]{64}")

#: An app ID is a slug of the chaperone (`crates/core/src/ids.rs`).
_APP: Final = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")

#: The tool that the record keeps when the policy does not configure the
#: tool of the call.
UNKNOWN_TOOL: Final = "<unknown>"

#: An exposed name: an upstream slug, `__`, and a tool name
#: (`crates/core/src/exposed.rs`).
_TOOL: Final = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}__[A-Za-z0-9_.-]{1,128}")

#: A deny code and a fault code of the chaperone.
_CODE: Final = r"[a-z][a-z0-9_]{0,63}"

_GATE_PREFIX: Final = "gate:"
_DENY_PREFIX: Final = "deny:"
_ERROR_PREFIX: Final = "is_error:"


class Event(enum.StrEnum):
    """What one record says happened (`AuditEvent` of the chaperone)."""

    DECIDED = "decided"
    RAN = "ran"
    WAITING = "waiting"
    DELIVERED = "delivered"
    FAILED = "failed"


class Decision(enum.StrEnum):
    """The decision of a `decided` record."""

    ALLOW = "allow"
    DENY = "deny"
    GATE = "gate"


#: The detail that each event takes. The Rust function `event_parts` writes
#: these texts.
_DETAIL: Final = {
    Event.DECIDED: re.compile(rf"allow|deny:{_CODE}|gate:[0-9a-f]{{32}}"),
    Event.RAN: re.compile(r"is_error:(?:true|false)"),
    Event.WAITING: re.compile(r"gate:[0-9a-f]{32}"),
    Event.DELIVERED: re.compile(r"gate:[0-9a-f]{32}"),
    Event.FAILED: re.compile(_CODE),
}


@dataclass(frozen=True, slots=True)
class Step:
    """One record of a call: the event, its time and its detail."""

    event: Event
    time: str
    detail: str


@dataclass(frozen=True, slots=True)
class _Record:
    """One line of the file, read and checked."""

    run: str
    call: int
    started: str
    app: str
    tool: str
    arguments_sha256: str
    step: Step

    @property
    def key(self) -> tuple[str, int]:
        return self.run, self.call

    @property
    def facts(self) -> tuple[str, str, str, str]:
        """The fields that each record of one call holds the same."""
        return self.started, self.app, self.tool, self.arguments_sha256


@dataclass(frozen=True, slots=True)
class AuditCall:
    """The records of one call in the read part of the file, as one row."""

    kind: ClassVar[str] = "call"

    run: str
    call: int
    started: str
    #: The app ID, which is the family name. Empty when the policy does not
    #: configure the app of the call.
    app: str
    tool: str
    arguments_sha256: str
    #: Oldest first.
    steps: tuple[Step, ...]

    @property
    def ended(self) -> str:
        return self.steps[-1].time

    @property
    def outcome(self) -> Event:
        """The event of the newest record."""
        return self.steps[-1].event

    @property
    def decision(self) -> Decision | None:
        """`None` when the `decided` record is older than the read part."""
        for step in self.steps:
            if step.event is Event.DECIDED:
                return Decision(step.detail.partition(":")[0])

        return None

    @property
    def gate(self) -> str:
        """The gate ID, or empty when no record names a gate."""
        for step in self.steps:
            if step.detail.startswith(_GATE_PREFIX):
                return step.detail.removeprefix(_GATE_PREFIX)

        return ""

    @property
    def reason(self) -> str:
        """The deny code or the fault code, or empty."""
        for step in self.steps:
            if step.event is Event.FAILED:
                return step.detail

            if step.detail.startswith(_DENY_PREFIX):
                return step.detail.removeprefix(_DENY_PREFIX)

        return ""

    @property
    def is_error(self) -> bool:
        """The upstream's error flag of a `ran` record."""
        return any(
            step.event is Event.RAN and step.detail == f"{_ERROR_PREFIX}true" for step in self.steps
        )


@dataclass(frozen=True, slots=True)
class Unreadable:
    """A line that this noticeboard could not read. It still takes a row."""

    kind: ClassVar[str] = "unreadable"

    problem: str


type AuditRow = AuditCall | Unreadable


@dataclass(frozen=True)
class AuditFilter:
    """The page's three filters. Empty means "every value"."""

    family: str = ""
    tool: str = ""
    decision: str = ""

    @property
    def active(self) -> bool:
        return bool(self.family or self.tool or self.decision)

    def matches(self, row: AuditRow) -> bool:
        """Exact on family and decision, substring on tool.

        The app ID of a record is the family name. A family name and a
        decision are closed vocabularies, so an exact match is what a reader
        means. A tool name carries an upstream prefix
        (`kagi__kagi_search_fetch`), so a substring is what a reader means
        there.

        An unreadable line always matches. A filter cannot exclude what
        could not be read.
        """
        if isinstance(row, Unreadable):
            return True

        if self.family and row.app != self.family:
            return False

        decision = row.decision
        if self.decision and (decision is None or decision.value != self.decision):
            return False

        return not (self.tool and self.tool not in row.tool)


@dataclass(frozen=True)
class AuditPage:
    """One page of calls, plus everything that bounded it."""

    rows: tuple[AuditRow, ...]
    offset: int
    limit: int
    has_more: bool
    #: How many lines the request read.
    lines_read: int
    #: Every bound that bit, and a file that would not open. A page renders
    #: these as a report so the numbers on it are never silently partial.
    problems: tuple[str, ...]

    @property
    def previous_offset(self) -> int:
        return max(0, self.offset - self.limit)

    @property
    def next_offset(self) -> int:
        return self.offset + self.limit


def read_page(path: Path, wanted: AuditFilter, offset: int = 0, limit: int = 50) -> AuditPage:
    """Newest first, grouped by call, filtered, paged."""
    lines, problems = _tail(path)
    rows = [one for one in _rows(lines, path.name) if wanted.matches(one)]

    return AuditPage(
        rows=tuple(rows[offset : offset + limit]),
        offset=offset,
        limit=limit,
        has_more=len(rows) > offset + limit,
        lines_read=len(lines),
        problems=tuple(problems),
    )


def _tail(path: Path) -> tuple[list[bytes], list[str]]:
    """The complete lines of the end of the file, newest first."""
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, 2)
            start = max(0, size - MAX_TAIL_BYTES)
            handle.seek(start)
            raw = handle.read(MAX_TAIL_BYTES)
    except FileNotFoundError:
        return [], [f"{path.name} is missing"]
    except OSError as error:
        # The noticeboard reads the file as a member of the group
        # `chaperone`. A user manager's groups are fixed when it starts.
        return [], [f"cannot read {path.name}: {error.strerror or error}"]

    problems: list[str] = []
    pieces = raw.split(b"\n")

    if start:
        problems.append(f"read the newest {MAX_TAIL_BYTES} bytes of {size}")
        # The seek lands inside a line, so the first piece goes.
        pieces = pieces[1:]

    # The sink writes a line and its LF in one write. The piece after the
    # last LF is a write in progress, or nothing.
    lines = [one for one in pieces[:-1] if one.strip()]
    lines.reverse()

    return lines, problems


def _rows(lines: list[bytes], name: str) -> list[AuditRow]:
    """Group the records by call. The order is that of the newest record."""
    order: list[tuple[str, int] | Unreadable] = []
    records: dict[tuple[str, int], list[_Record]] = {}

    for raw in lines:
        record = _record(raw, name)

        if isinstance(record, Unreadable):
            order.append(record)
            continue

        if record.key not in records:
            records[record.key] = []
            order.append(record.key)

        records[record.key].append(record)

    return [one if isinstance(one, Unreadable) else _call(records[one]) for one in order]


def _call(newest_first: list[_Record]) -> AuditRow:
    newest = newest_first[0]

    if any(one.facts != newest.facts for one in newest_first):
        return Unreadable(
            f"the records of run {newest.run} call {newest.call} disagree on "
            f"the start, the app, the tool or the argument digest"
        )

    return AuditCall(
        run=newest.run,
        call=newest.call,
        started=newest.started,
        app=newest.app,
        tool=newest.tool,
        arguments_sha256=newest.arguments_sha256,
        steps=tuple(one.step for one in reversed(newest_first)),
    )


def _record(raw: bytes, name: str) -> _Record | Unreadable:
    if len(raw) > MAX_LINE_BYTES:
        return Unreadable(f"{name}: a line passed {MAX_LINE_BYTES} bytes and was not parsed")

    body, problem = jsonfiles.parse_object(raw, name, MAX_LINE_BYTES, surface=Surface.AUDIT_LINE)

    if body is None:
        return Unreadable(problem or f"{name}: a line would not parse")

    return _checked(body, name)


def _checked(body: Json, name: str) -> _Record | Unreadable:
    """The record of a line that has the shape of the Rust type, or the
    first key that breaks it."""
    if frozenset(body) != KEYS:
        keys = ", ".join(sorted(frozenset(body) ^ KEYS))
        return Unreadable(f"{name}: a line has the wrong keys: {keys}")

    run = _matching(body["run"], _ID)
    call = _call_number(body["call"])
    started = _time(body["started"])
    ended = _time(body["time"])
    app = _app(body["app"])
    tool = _tool(body["tool"])
    digest = _matching(body["arguments_sha256"], _SHA256)
    event = _event(body["event"])
    bad = [
        key
        for key, value in (
            ("run", run),
            ("call", call),
            ("started", started),
            ("time", ended),
            ("app", app),
            ("tool", tool),
            ("arguments_sha256", digest),
            ("event", event),
        )
        if value is None
    ]

    if bad:
        return Unreadable(f"{name}: a line has a bad {', '.join(bad)}")

    # The list above holds each value that is `None`, so none is.
    assert run and call and started and ended and app is not None and tool and digest and event
    detail = _matching(body["detail"], _DETAIL[event])

    if detail is None:
        return Unreadable(f"{name}: a line has a bad detail for the event {event}")

    return _Record(
        run=run,
        call=call,
        started=started,
        app=app,
        tool=tool,
        arguments_sha256=digest,
        step=Step(event=event, time=ended, detail=detail),
    )


def _matching(value: object, pattern: re.Pattern[str]) -> str | None:
    if isinstance(value, str) and pattern.fullmatch(value):
        return value

    return None


def _call_number(value: object) -> int | None:
    """A `NonZeroU64`. A JSON `true` is no number. The strict reader gives
    an integer of 64 bits at most."""
    if type(value) is int and value >= 1:
        return value

    return None


def _time(value: object) -> str | None:
    if isinstance(value, str) and rfc3339.parse(value) is not None:
        return value

    return None


def _app(value: object) -> str | None:
    """Empty for a JSON null: the policy does not configure the app."""
    if value is None:
        return ""

    return _matching(value, _APP)


def _tool(value: object) -> str | None:
    if value == UNKNOWN_TOOL:
        return UNKNOWN_TOOL

    return _matching(value, _TOOL)


def _event(value: object) -> Event | None:
    if isinstance(value, str) and value in Event:
        return Event(value)

    return None
