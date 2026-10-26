"""Reading the chaperone's one audit file."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from noticeboard.auditfiles import (
    ARGS_NOTICE,
    MAX_LINE_BYTES,
    AuditCall,
    AuditFilter,
    Decision,
    Event,
    Unreadable,
    read_page,
)
from noticeboard_helpers import (
    NO_ARGUMENTS_SHA256,
    RUN,
    Notices,
    audit_record,
    write_audit,
)

from noticeboard import auditfiles

#: The three lines of `writes_safe_records_with_mode_0600` in
#: `plugins/default/audit-jsonl/src/lib.rs` of the chaperone repository. The
#: test there writes an allowed call that fails, then a denied call. The
#: policy is absent, so the app is null and the tool is `<unknown>`.
RUST_FIXTURE = (
    '{"run":"07070707070707070707070707070707","call":1,'
    '"started":"2025-10-24T00:00:00.123Z","time":"2025-10-24T00:00:00.123Z",'
    '"app":null,"tool":"<unknown>",'
    '"arguments_sha256":"44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",'
    '"event":"decided","detail":"allow"}\n'
    '{"run":"07070707070707070707070707070707","call":1,'
    '"started":"2025-10-24T00:00:00.123Z","time":"2025-10-24T00:01:00.123Z",'
    '"app":null,"tool":"<unknown>",'
    '"arguments_sha256":"44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",'
    '"event":"failed","detail":"not_connected"}\n'
    '{"run":"07070707070707070707070707070707","call":2,'
    '"started":"2025-10-24T00:00:00.123Z","time":"2025-10-24T00:00:00.123Z",'
    '"app":null,"tool":"<unknown>",'
    '"arguments_sha256":"44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",'
    '"event":"decided","detail":"deny:tool_not_granted"}\n'
)

#: A gate ID: 16 bytes as hex.
GATE = "0a" * 16

#: The problem of a text that is not strict JSON, after the file name.
NOT_STRICT = "is not JSON: the text breaks a rule of strict JSON"


def audit_file(tmp_path: Path) -> Path:
    return tmp_path / "audit.jsonl"


def calls(rows: tuple[AuditCall | Unreadable, ...]) -> list[AuditCall]:
    found = [one for one in rows if isinstance(one, AuditCall)]
    assert len(found) == len(rows), [one for one in rows if isinstance(one, Unreadable)]
    return found


def problem_of(tmp_path: Path, line: str) -> str:
    """Write one line, read the page and give the problem of its one row."""
    audit_file(tmp_path).write_text(line + "\n", encoding="utf-8")
    page = read_page(audit_file(tmp_path), AuditFilter())

    assert len(page.rows) == 1
    row = page.rows[0]
    assert isinstance(row, Unreadable)
    return row.problem


def test_audit_reads_rust_records(tmp_path: Path) -> None:
    audit_file(tmp_path).write_text(RUST_FIXTURE, encoding="utf-8")

    page = read_page(audit_file(tmp_path), AuditFilter())

    denied, failed = calls(page.rows)
    assert (denied.run, denied.call) == (RUN, 2)
    assert denied.decision is Decision.DENY
    assert denied.reason == "tool_not_granted"
    assert denied.outcome is Event.DECIDED
    assert denied.app == ""
    assert denied.tool == "<unknown>"
    assert denied.arguments_sha256 == NO_ARGUMENTS_SHA256

    assert (failed.run, failed.call) == (RUN, 1)
    assert [step.event for step in failed.steps] == [Event.DECIDED, Event.FAILED]
    assert failed.decision is Decision.ALLOW
    assert failed.outcome is Event.FAILED
    assert failed.reason == "not_connected"
    assert failed.started == "2025-10-24T00:00:00.123Z"
    assert failed.ended == "2025-10-24T00:01:00.123Z"
    assert page.problems == ()


def test_the_page_says_it_shows_argument_digests_only() -> None:
    assert "argument digests only" in ARGS_NOTICE


def test_a_gated_call_is_one_row_with_its_gate(tmp_path: Path) -> None:
    write_audit(
        audit_file(tmp_path),
        [
            audit_record(1, "decided", f"gate:{GATE}", tool="attendance__enqueue"),
            audit_record(1, "waiting", f"gate:{GATE}", tool="attendance__enqueue"),
            audit_record(2, "decided", "allow"),
            audit_record(2, "ran", "is_error:false"),
            audit_record(1, "ran", "is_error:true", tool="attendance__enqueue"),
        ],
    )

    page = read_page(audit_file(tmp_path), AuditFilter())

    gated, other = calls(page.rows)
    assert gated.call == 1
    assert gated.decision is Decision.GATE
    assert gated.gate == GATE
    assert [step.event for step in gated.steps] == [Event.DECIDED, Event.WAITING, Event.RAN]
    assert gated.is_error
    assert other.call == 2
    assert not other.is_error
    assert other.gate == ""


def test_a_delivered_record_names_its_gate(tmp_path: Path) -> None:
    write_audit(
        audit_file(tmp_path),
        [audit_record(3, "decided", "allow"), audit_record(3, "delivered", f"gate:{GATE}")],
    )

    (row,) = calls(read_page(audit_file(tmp_path), AuditFilter()).rows)

    assert row.outcome is Event.DELIVERED
    assert row.gate == GATE


def test_two_runs_with_one_call_number_are_two_rows(tmp_path: Path) -> None:
    write_audit(
        audit_file(tmp_path),
        [audit_record(1), audit_record(1, run="08" * 16)],
    )

    rows = calls(read_page(audit_file(tmp_path), AuditFilter()).rows)

    assert [one.run for one in rows] == ["08" * 16, RUN]


def test_a_call_whose_decision_is_outside_the_tail_still_shows(tmp_path: Path) -> None:
    write_audit(audit_file(tmp_path), [audit_record(4, "ran", "is_error:false")])

    (row,) = calls(read_page(audit_file(tmp_path), AuditFilter()).rows)

    assert row.decision is None
    assert row.outcome is Event.RAN


def test_the_family_filter_is_exact_on_the_app(tmp_path: Path) -> None:
    write_audit(
        audit_file(tmp_path),
        [audit_record(1, app="chat"), audit_record(2, app="chatty"), audit_record(3, app=None)],
    )

    rows = calls(read_page(audit_file(tmp_path), AuditFilter(family="chat")).rows)

    assert [one.call for one in rows] == [1]


def test_the_tool_filter_is_a_substring(tmp_path: Path) -> None:
    write_audit(
        audit_file(tmp_path),
        [audit_record(1, tool="kagi__kagi_search_fetch"), audit_record(2, tool="mail__send")],
    )

    rows = calls(read_page(audit_file(tmp_path), AuditFilter(tool="kagi")).rows)

    assert [one.call for one in rows] == [1]


def test_the_decision_filter_finds_denials(tmp_path: Path) -> None:
    write_audit(
        audit_file(tmp_path),
        [audit_record(1), audit_record(2, detail="deny:rate_limited")],
    )

    rows = calls(read_page(audit_file(tmp_path), AuditFilter(decision="deny")).rows)

    assert [one.call for one in rows] == [2]
    assert rows[0].reason == "rate_limited"


def test_an_unreadable_line_matches_every_filter(tmp_path: Path) -> None:
    audit_file(tmp_path).write_text("not json\n", encoding="utf-8")

    page = read_page(audit_file(tmp_path), AuditFilter(family="chat", decision="deny"))

    assert len(page.rows) == 1
    assert isinstance(page.rows[0], Unreadable)


def test_paging_walks_backwards_through_the_calls(tmp_path: Path) -> None:
    write_audit(audit_file(tmp_path), [audit_record(number) for number in range(1, 6)])

    first = read_page(audit_file(tmp_path), AuditFilter(), offset=0, limit=2)
    second = read_page(audit_file(tmp_path), AuditFilter(), offset=2, limit=2)
    last = read_page(audit_file(tmp_path), AuditFilter(), offset=4, limit=2)

    assert [one.call for one in calls(first.rows)] == [5, 4]
    assert first.has_more
    assert [one.call for one in calls(second.rows)] == [3, 2]
    assert [one.call for one in calls(last.rows)] == [1]
    assert not last.has_more


def test_a_huge_file_is_read_from_its_tail_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_audit(audit_file(tmp_path), [audit_record(number) for number in range(1, 101)])
    one_line = len(json.dumps(audit_record(100), separators=(",", ":"))) + 1
    monkeypatch.setattr(auditfiles, "MAX_TAIL_BYTES", one_line * 10 + 7)

    page = read_page(audit_file(tmp_path), AuditFilter(), limit=500)

    rows = calls(page.rows)
    assert [one.call for one in rows] == list(range(100, 90, -1))
    assert any("read the newest" in one for one in page.problems)


def test_an_unterminated_last_line_is_not_read(tmp_path: Path) -> None:
    """The sink writes a line and its LF in one write. A text after the last
    LF is a write in progress."""
    write_audit(audit_file(tmp_path), [audit_record(1)])
    with audit_file(tmp_path).open("a", encoding="utf-8") as handle:
        handle.write('{"run":"07')

    page = read_page(audit_file(tmp_path), AuditFilter())

    assert [one.call for one in calls(page.rows)] == [1]


def test_a_missing_audit_file_reports_and_does_not_raise(tmp_path: Path) -> None:
    page = read_page(audit_file(tmp_path), AuditFilter())

    assert page.rows == ()
    assert any("audit.jsonl" in one for one in page.problems)


def test_an_unreadable_audit_file_reports_and_does_not_raise(tmp_path: Path) -> None:
    audit_file(tmp_path).mkdir()

    page = read_page(audit_file(tmp_path), AuditFilter())

    assert page.rows == ()
    assert any("cannot read" in one for one in page.problems)


def test_an_absurd_line_is_not_parsed_and_says_so(tmp_path: Path) -> None:
    line = json.dumps(audit_record(tool="x" * MAX_LINE_BYTES))

    assert str(MAX_LINE_BYTES) in problem_of(tmp_path, line)


def test_a_line_that_is_not_strict_records_a_notice_with_no_family(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded = Notices().install(monkeypatch)

    problem = problem_of(tmp_path, '{"run": NaN}')

    assert problem == f"audit.jsonl {NOT_STRICT}"
    assert [(one[0], one[2]) for one in recorded.calls] == [("audit.line", None)]


def test_a_line_break_inside_a_string_does_not_split_a_line(tmp_path: Path) -> None:
    """U+2028 is legal inside a JSON string. The reader splits on LF alone,
    so the line is one unreadable row, not two rows."""
    line = json.dumps(audit_record(detail="deny:\u2028x"), ensure_ascii=False)

    assert "detail" in problem_of(tmp_path, line)


@pytest.mark.parametrize(
    ("change", "word"),
    [
        ({"extra": 1}, "extra"),
        ({"run": "07" * 15}, "run"),
        ({"run": "AB" * 16}, "run"),
        ({"call": 0}, "call"),
        ({"call": True}, "call"),
        ({"started": "2025-10-24 00:00:00Z"}, "started"),
        ({"time": 7}, "time"),
        ({"app": ""}, "app"),
        ({"app": "Chat"}, "app"),
        ({"tool": "kagi"}, "tool"),
        ({"arguments_sha256": "00"}, "arguments_sha256"),
        ({"event": "allowed"}, "event"),
        ({"detail": "permit"}, "detail"),
        ({"event": "ran", "detail": "allow"}, "detail"),
        ({"event": "waiting", "detail": "gate:short"}, "detail"),
        ({"event": "failed", "detail": "Not Connected"}, "detail"),
    ],
)
def test_a_line_outside_the_rust_shape_takes_a_row(
    tmp_path: Path, change: dict[str, Any], word: str
) -> None:
    line = json.dumps(audit_record(**change))

    assert word in problem_of(tmp_path, line)


def test_a_line_with_a_missing_key_takes_a_row(tmp_path: Path) -> None:
    record = audit_record()
    del record["detail"]

    assert "detail" in problem_of(tmp_path, json.dumps(record))


def test_records_of_one_call_that_disagree_take_a_row(tmp_path: Path) -> None:
    write_audit(
        audit_file(tmp_path),
        [audit_record(1), audit_record(1, "ran", "is_error:false", tool="mail__send")],
    )

    page = read_page(audit_file(tmp_path), AuditFilter())

    (row,) = page.rows
    assert isinstance(row, Unreadable)
    assert "disagree" in row.problem
