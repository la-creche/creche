"""The PEP's audit, read from the flat files (contract 04 §6).

The PEP serves no read endpoint for the audit, on purpose: an endpoint
would be new surface on the process that holds every upstream credential,
and reading a flat file needs none. So the noticeboard reads
`/srv/agents/state/rework/audit/YYYY-MM-DD.jsonl` directly, by group.

The reader sees FULL tool arguments (§6.1), and the page says so in one
line, because a reader who does not know that is a reader who could paste
the page somewhere it does not belong.

Three properties this module holds:

1. Newest first, across days and within a day.
2. Bounded. A day file is read from its tail when it is huge, one line is
   dropped when it is absurd, and the scan stops at a cap. Every bound that
   bites is reported on the page rather than silently changing the answer.
3. LF is the only line delimiter. U+2028 and U+2029 are legal inside a JSON
   string, so a generic line reader would split one record into two
   (contract 02 §8's rule, which holds for any NDJSON).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from . import jsonfiles
from .jsonfiles import Json

#: One line said in every rendering of this page.
ARGS_NOTICE: Final = (
    "This page shows full tool arguments, exactly as the PEP recorded them. "
    "The audit files are readable by this service alone (contract 04 §6)."
)

#: Contract 04 §6: one file per UTC day. Anything else in the directory is
#: not an audit file and is not read.
DAY_NAME: Final = re.compile(r"^\d{4}-\d{2}-\d{2}\.jsonl$")

#: A record can hold a 256 KiB request's arguments, with one string value
#: capped at 8 KiB (§6.1). Half a megabyte is past any real line.
MAX_LINE_BYTES: Final = 512 * 1024

#: How much of one day file is read. Past this the tail is read instead, so
#: a busy day still answers newest-first without loading it all.
MAX_DAY_BYTES: Final = 32 * 1024 * 1024

#: How many lines one request will look at before it gives up and asks for
#: a narrower filter. A page must answer in bounded time.
MAX_LINES_SCANNED: Final = 40_000

#: How many day files one request will open.
MAX_DAYS: Final = 30

#: How much of one record's arguments a page renders.
MAX_ARGS_CHARS: Final = 20_000

MAX_CHAIN: Final = 10


@dataclass(frozen=True)
class AuditFilter:
    """The page's four filters. Empty means "every value"."""

    family: str = ""
    session: str = ""
    tool: str = ""
    decision: str = ""

    @property
    def active(self) -> bool:
        return bool(self.family or self.session or self.tool or self.decision)

    def matches(self, row: AuditRow) -> bool:
        """Exact on family and decision, substring on tool and session.

        A family name and a decision are closed vocabularies, so an exact
        match is what a reader means. A tool name carries a server prefix
        (`kagi__kagi_search_fetch`) and a session id carries a door prefix,
        so a substring is what a reader means there.

        A record that would not parse always matches. A filter cannot
        exclude what could not be read, and a hole a filter hides is worse
        than a row that does not belong (invariant 15).
        """
        if row.problem:
            return True

        if self.family and row.family != self.family:
            return False

        if self.decision and row.decision != self.decision:
            return False

        if self.tool and self.tool not in row.tool:
            return False

        return not (self.session and self.session not in row.session_id)


@dataclass(frozen=True)
class AuditRow:
    """Contract 04 §6.1, §6.2 and §6.3, as one page row."""

    ts: str
    family: str
    sandbox_id: str
    sandbox_id_trusted: bool
    grants_rev: str
    tool: str
    decision: str
    reason: str
    latency_ms: int
    waited_ms: int
    gate: str
    #: §6.2: all three claimed keys are always present, and a dropped header
    #: reads null. The page says "claimed" beside them for that reason.
    session_id: str
    turn_id: str
    delegation_id: str
    chain: tuple[str, ...]
    args: str
    args_truncated: bool
    problem: str

    @property
    def gated(self) -> bool:
        return bool(self.gate)


@dataclass(frozen=True)
class AuditPage:
    """One page of records, plus everything that bounded it."""

    rows: tuple[AuditRow, ...]
    offset: int
    limit: int
    has_more: bool
    days_read: tuple[str, ...]
    scanned: int
    #: Every bound that bit, and every day file that would not open. A page
    #: renders these as a report so the numbers on it are never silently
    #: partial.
    problems: tuple[str, ...]

    @property
    def previous_offset(self) -> int:
        return max(0, self.offset - self.limit)

    @property
    def next_offset(self) -> int:
        return self.offset + self.limit


def read_page(audit_dir: Path, wanted: AuditFilter, offset: int = 0, limit: int = 50) -> AuditPage:
    """Newest first, filtered, paged."""
    days, problems = _day_files(audit_dir)
    rows: list[AuditRow] = []
    scanned = 0
    skipped = 0
    has_more = False

    for day in days:
        if scanned >= MAX_LINES_SCANNED or has_more:
            break

        lines, trouble = _lines_newest_first(day)
        problems.extend(trouble)

        for raw in lines:
            scanned += 1

            if scanned > MAX_LINES_SCANNED:
                problems.append(
                    f"stopped after {MAX_LINES_SCANNED} records. "
                    f"Narrow the filter to reach older ones"
                )
                break

            row = _row(raw, day.name)

            if not wanted.matches(row):
                continue

            if skipped < offset:
                skipped += 1
                continue

            # One past the page proves there is a next page without
            # counting every remaining record.
            if len(rows) == limit:
                has_more = True
                break

            rows.append(row)

    return AuditPage(
        rows=tuple(rows),
        offset=offset,
        limit=limit,
        has_more=has_more,
        days_read=tuple(one.stem for one in days),
        scanned=scanned,
        problems=tuple(problems),
    )


def known_days(audit_dir: Path) -> tuple[str, ...]:
    """Every audit day on disk, newest first."""
    days, _ = _day_files(audit_dir)
    return tuple(one.stem for one in days)


def _day_files(audit_dir: Path) -> tuple[list[Path], list[str]]:
    """Day files newest first. `YYYY-MM-DD` sorts by date as text."""
    problems: list[str] = []

    try:
        found = sorted(
            (one for one in audit_dir.iterdir() if DAY_NAME.match(one.name)),
            key=lambda one: one.name,
            reverse=True,
        )
    except OSError as error:
        # A user manager's supplementary groups are fixed when it starts,
        # so a service may not group-read the PEP's 0640 files until that
        # manager restarts (README gotcha 4). That is a banner, not a crash.
        return [], [f"cannot list the audit directory: {error.strerror or error}"]

    if len(found) > MAX_DAYS:
        problems.append(f"showing the newest {MAX_DAYS} day files of {len(found)}")

    return found[:MAX_DAYS], problems


def _lines_newest_first(path: Path) -> tuple[list[bytes], list[str]]:
    """One day's records, newest first, read from the tail when huge."""
    problems: list[str] = []

    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > MAX_DAY_BYTES:
                handle.seek(size - MAX_DAY_BYTES)
                problems.append(f"{path.stem}: read the newest {MAX_DAY_BYTES} bytes of {size}")
                # The seek lands mid-record, so the first partial line goes.
                handle.readline()

            raw = handle.read()
    except OSError as error:
        return [], [f"cannot read {path.name}: {error.strerror or error}"]

    lines = [one for one in raw.split(b"\n") if one.strip()]
    lines.reverse()

    return lines, problems


def _row(raw: bytes, day: str) -> AuditRow:
    if len(raw) > MAX_LINE_BYTES:
        return _bad_row(f"{day}: a record passed {MAX_LINE_BYTES} bytes and was not parsed")

    body, problem = jsonfiles.parse_object(raw, day)

    if body is None:
        return _bad_row(problem or f"{day}: a record would not parse")

    claimed = jsonfiles.child(body, "claimed")
    args, truncated = _args(body)

    return AuditRow(
        ts=jsonfiles.whole(body, "ts"),
        family=jsonfiles.whole(body, "family"),
        sandbox_id=jsonfiles.whole(body, "sandbox_id"),
        sandbox_id_trusted=jsonfiles.flag(body, "sandbox_id_trusted"),
        grants_rev=jsonfiles.whole(body, "grants_rev"),
        tool=jsonfiles.whole(body, "tool"),
        decision=jsonfiles.whole(body, "decision"),
        reason=jsonfiles.whole(body, "reason"),
        latency_ms=jsonfiles.integer(body, "latency_ms"),
        waited_ms=jsonfiles.integer(body, "waited_ms"),
        gate=jsonfiles.whole(body, "gate"),
        session_id=jsonfiles.whole(claimed, "session_id"),
        turn_id=jsonfiles.whole(claimed, "turn_id"),
        delegation_id=jsonfiles.whole(claimed, "delegation_id"),
        chain=jsonfiles.strings(body, "chain", MAX_CHAIN),
        args=args,
        args_truncated=truncated,
        problem="",
    )


def _args(body: Json) -> tuple[str, bool]:
    """The full arguments, rendered for a page (§6.1).

    "Full" bounds redaction, not volume: every argument that was sent is
    here. Only the rendering is capped, and the page says when it was.
    """
    value = body.get("args")

    if value is None:
        return "", False

    try:
        rendered = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return "<arguments this noticeboard could not render>", False

    if len(rendered) <= MAX_ARGS_CHARS:
        return rendered, False

    return rendered[:MAX_ARGS_CHARS], True


def _bad_row(problem: str) -> AuditRow:
    """A record this noticeboard could not read. It still takes a row, because a
    hole in the timeline is exactly what an audit must not have."""
    return AuditRow(
        ts="",
        family="",
        sandbox_id="",
        sandbox_id_trusted=False,
        grants_rev="",
        tool="",
        decision="",
        reason="",
        latency_ms=0,
        waited_ms=0,
        gate="",
        session_id="",
        turn_id="",
        delegation_id="",
        chain=(),
        args="",
        args_truncated=False,
        problem=problem,
    )
