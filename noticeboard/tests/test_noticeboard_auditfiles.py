"""Reading the PEP's audit files (contract 04 §6)."""

from __future__ import annotations

import json
from pathlib import Path

from noticeboard.auditfiles import (
    ARGS_NOTICE,
    MAX_LINE_BYTES,
    AuditFilter,
    known_days,
    read_page,
)
from noticeboard_helpers import audit_line, deep_object, write_audit_day

#: A record under `MAX_LINE_BYTES` that nests past the limit of the JSON reader.
DEEP_RECORD_LEVELS = 250_000


def test_the_page_says_it_shows_full_arguments() -> None:
    assert "full tool arguments" in ARGS_NOTICE


def test_records_come_back_newest_first_within_a_day(tmp_path: Path) -> None:
    write_audit_day(
        tmp_path,
        "2026-09-19",
        [audit_line(tool="first"), audit_line(tool="second"), audit_line(tool="third")],
    )

    page = read_page(tmp_path, AuditFilter())

    assert [one.tool for one in page.rows] == ["third", "second", "first"]


def test_newer_days_come_before_older_ones(tmp_path: Path) -> None:
    write_audit_day(tmp_path, "2026-09-17", [audit_line(tool="old")])
    write_audit_day(tmp_path, "2026-09-19", [audit_line(tool="new")])

    page = read_page(tmp_path, AuditFilter())

    assert [one.tool for one in page.rows] == ["new", "old"]
    assert page.days_read == ("2026-09-19", "2026-09-17")


def test_a_file_that_is_not_a_day_is_ignored(tmp_path: Path) -> None:
    write_audit_day(tmp_path, "2026-09-19", [audit_line()])
    (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "2026-09.jsonl").write_text("{}\n", encoding="utf-8")

    assert known_days(tmp_path) == ("2026-09-19",)


def test_a_day_name_with_a_trailing_newline_is_ignored(tmp_path: Path) -> None:
    # A `$` also matches before a final newline. `\Z` does not.
    write_audit_day(tmp_path, "2026-09-19", [audit_line()])
    (tmp_path / "2026-09-20.jsonl\n").write_text("{}\n", encoding="utf-8")

    assert known_days(tmp_path) == ("2026-09-19",)


def test_the_family_filter_is_exact(tmp_path: Path) -> None:
    write_audit_day(
        tmp_path,
        "2026-09-19",
        [audit_line(family="chat"), audit_line(family="chat-two"), audit_line(family="code")],
    )

    page = read_page(tmp_path, AuditFilter(family="chat"))

    assert [one.family for one in page.rows] == ["chat"]


def test_the_tool_filter_is_a_substring(tmp_path: Path) -> None:
    write_audit_day(
        tmp_path,
        "2026-09-19",
        [audit_line(tool="kagi__kagi_search_fetch"), audit_line(tool="ha_call")],
    )

    page = read_page(tmp_path, AuditFilter(tool="kagi"))

    assert [one.tool for one in page.rows] == ["kagi__kagi_search_fetch"]


def test_the_decision_filter_finds_denials(tmp_path: Path) -> None:
    write_audit_day(
        tmp_path,
        "2026-09-19",
        [
            audit_line(decision="allow"),
            audit_line(decision="deny", reason="not_granted"),
            audit_line(decision="pending", gate="01K5J9QWB2M4N6Q8S0V2W4Y6A8"),
        ],
    )

    page = read_page(tmp_path, AuditFilter(decision="deny"))

    assert len(page.rows) == 1
    assert page.rows[0].reason == "not_granted"


def test_the_session_filter_reads_the_claimed_field(tmp_path: Path) -> None:
    other = {"session_id": "tui-01JBQ7ZZ9D6M0Q4RXT2J8HYVBK", "turn_id": None, "delegation_id": None}
    write_audit_day(tmp_path, "2026-09-19", [audit_line(), audit_line(claimed=other)])

    page = read_page(tmp_path, AuditFilter(session="tui-"))

    assert len(page.rows) == 1
    assert page.rows[0].session_id.startswith("tui-")


def test_a_dropped_header_reads_empty_not_missing(tmp_path: Path) -> None:
    """Contract 04 §6.2: a dropped header reads null, and that is the fact."""
    blank = {"session_id": None, "turn_id": None, "delegation_id": None}
    write_audit_day(tmp_path, "2026-09-19", [audit_line(claimed=blank)])

    page = read_page(tmp_path, AuditFilter())

    assert page.rows[0].session_id == ""
    assert page.rows[0].turn_id == ""


def test_paging_walks_backwards_through_the_records(tmp_path: Path) -> None:
    write_audit_day(
        tmp_path, "2026-09-19", [audit_line(tool=f"tool-{index}") for index in range(10)]
    )

    first = read_page(tmp_path, AuditFilter(), offset=0, limit=4)
    second = read_page(tmp_path, AuditFilter(), offset=4, limit=4)
    last = read_page(tmp_path, AuditFilter(), offset=8, limit=4)

    assert [one.tool for one in first.rows] == ["tool-9", "tool-8", "tool-7", "tool-6"]
    assert first.has_more
    assert [one.tool for one in second.rows] == ["tool-5", "tool-4", "tool-3", "tool-2"]
    assert last.has_more is False
    assert last.next_offset == 12
    assert second.previous_offset == 0


def test_full_arguments_are_rendered(tmp_path: Path) -> None:
    write_audit_day(tmp_path, "2026-09-19", [audit_line(args={"query": "boiler", "limit": 3})])

    page = read_page(tmp_path, AuditFilter())

    assert '"query": "boiler"' in page.rows[0].args
    assert not page.rows[0].args_truncated


def test_enormous_arguments_are_capped_and_say_so(tmp_path: Path) -> None:
    write_audit_day(tmp_path, "2026-09-19", [audit_line(args={"blob": "x" * 30_000})])

    page = read_page(tmp_path, AuditFilter())

    assert page.rows[0].args_truncated


def test_a_record_that_will_not_parse_takes_a_row(tmp_path: Path) -> None:
    """Invariant 15: a hole a filter hides is worse than an odd row."""
    path = write_audit_day(tmp_path, "2026-09-19", [audit_line()])
    path.write_text(path.read_text(encoding="utf-8") + "{ broken\n", encoding="utf-8")

    page = read_page(tmp_path, AuditFilter(family="chat"))

    problems = [one for one in page.rows if one.problem]
    assert len(problems) == 1
    assert "is not JSON" in problems[0].problem


def test_a_record_that_nests_too_deep_takes_a_row(tmp_path: Path) -> None:
    """The JSON reader raises RecursionError on this record, not ValueError."""
    path = write_audit_day(tmp_path, "2026-09-19", [audit_line()])
    deep = deep_object(DEEP_RECORD_LEVELS)
    assert len(deep) < MAX_LINE_BYTES
    path.write_bytes(path.read_bytes() + deep + b"\n")

    page = read_page(tmp_path, AuditFilter())

    assert len(page.rows) == 2
    assert "nests deeper than the reader allows" in page.rows[0].problem
    assert page.rows[1].tool == "kagi__kagi_search_fetch"


def test_an_absurd_record_is_not_parsed_and_says_so(tmp_path: Path) -> None:
    line = json.dumps(audit_line(args={"blob": "y" * (MAX_LINE_BYTES + 10)}))
    (tmp_path / "2026-09-19.jsonl").write_text(line + "\n", encoding="utf-8")

    page = read_page(tmp_path, AuditFilter())

    assert "passed" in page.rows[0].problem


def test_a_missing_audit_directory_reports_and_does_not_raise(tmp_path: Path) -> None:
    page = read_page(tmp_path / "nothing-here", AuditFilter())

    assert page.rows == ()
    assert page.problems
    assert "cannot list the audit directory" in page.problems[0]


def test_a_line_break_inside_a_string_does_not_split_a_record(tmp_path: Path) -> None:
    """U+2028 is legal inside JSON, so only LF may delimit (contract 02 §8).

    The record is written with `ensure_ascii=False`, so a RAW U+2028 lands
    in the file. A reader that splits on every Unicode line break sees two
    broken halves here. This reader splits on LF alone and sees one record.
    """
    line = json.dumps(audit_line(args={"text": "one\u2028two"}), ensure_ascii=False)
    (tmp_path / "2026-09-19.jsonl").write_text(line + "\n", encoding="utf-8")

    page = read_page(tmp_path, AuditFilter())

    assert len(page.rows) == 1
    assert page.rows[0].problem == ""
    assert "\u2028" in page.rows[0].args


def test_the_chain_is_read_and_bounded(tmp_path: Path) -> None:
    write_audit_day(
        tmp_path, "2026-09-19", [audit_line(family="vault-oracle", chain=["chat", "vault-oracle"])]
    )

    page = read_page(tmp_path, AuditFilter())

    assert page.rows[0].chain == ("chat", "vault-oracle")


def test_an_untrusted_sandbox_id_is_visible(tmp_path: Path) -> None:
    write_audit_day(tmp_path, "2026-09-19", [audit_line(sandbox_id_trusted=False)])

    page = read_page(tmp_path, AuditFilter())

    assert not page.rows[0].sandbox_id_trusted
