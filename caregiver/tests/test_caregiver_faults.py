"""The fault-file reader: input from another process, so a bad entry is
dropped rather than trusted or raised (contract 05 section 3.3.1)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from caregiver.faults import read_fault_file
from caregiver_helpers import UNREADABLE_JSON

NOW = datetime(2026, 9, 19, 12, 0, 0, tzinfo=UTC)


def write(path: Path, body: object) -> None:
    path.write_text(json.dumps(body), encoding="utf-8")


def test_missing_file_is_none(tmp_path: Path) -> None:
    assert read_fault_file(tmp_path / "no-such-file.json", "pep") is None


def test_corrupt_json_is_none(tmp_path: Path) -> None:
    path = tmp_path / "chat.json"
    path.write_text("{not json", encoding="utf-8")
    assert read_fault_file(path, "pep") is None


@pytest.mark.parametrize("raw", UNREADABLE_JSON.values(), ids=UNREADABLE_JSON.keys())
def test_a_file_that_does_not_read_is_none(tmp_path: Path, raw: bytes) -> None:
    """Rule 6: a fault file is input from another process. Content that
    does not read is refused. It does not raise into the pass."""
    path = tmp_path / "chat.json"
    path.write_bytes(raw)
    assert read_fault_file(path, "pep") is None


def test_a_json_list_at_the_top_is_none(tmp_path: Path) -> None:
    path = tmp_path / "chat.json"
    write(path, [1, 2, 3])
    assert read_fault_file(path, "pep") is None


def test_missing_family_or_written_at_is_none(tmp_path: Path) -> None:
    path = tmp_path / "chat.json"
    write(path, {"source": "pep", "faults": []})
    assert read_fault_file(path, "pep") is None


def test_faults_not_a_list_is_none(tmp_path: Path) -> None:
    path = tmp_path / "chat.json"
    write(path, {"family": "chat", "written_at": "2026-09-19T11:59:00Z", "faults": {}})
    assert read_fault_file(path, "pep") is None


def test_a_valid_pep_fault_parses_with_its_detail(tmp_path: Path) -> None:
    path = tmp_path / "chat.json"
    write(
        path,
        {
            "family": "chat",
            "source": "pep",
            "written_at": "2026-09-19T11:59:00Z",
            "faults": [
                {
                    "code": "grants_stale",
                    "blocks_turns": True,
                    "since": "2026-09-19T11:58:41Z",
                    "source": "pep",
                    "message": "grants/chat.json: unknown version 3",
                    "rev": "reg-8c01aa",
                }
            ],
        },
    )
    result = read_fault_file(path, "pep", now=NOW)
    assert result is not None
    assert result.family == "chat"
    assert len(result.faults) == 1
    fault = result.faults[0]
    assert fault.code == "grants_stale"
    assert fault.detail == {"message": "grants/chat.json: unknown version 3", "rev": "reg-8c01aa"}


def test_attendance_may_report_an_unreadable_audit(tmp_path: Path) -> None:
    """Contract 05 section 3.3's `audit_unreadable`.
    It does not stop turns: the PEP's audit is how attendance LABELS a turn
    that waits for a phone tap, never how it decides to run one."""
    path = tmp_path / "chat.json"
    write(
        path,
        {
            "family": "chat",
            "source": "attendance",
            "written_at": "2026-09-19T11:59:00Z",
            "faults": [
                {
                    "code": "audit_unreadable",
                    "blocks_turns": False,
                    "since": "2026-09-19T11:58:41Z",
                    "source": "attendance",
                    "message": "cannot read the PEP's audit at /srv/.../2026-09-19.jsonl",
                }
            ],
        },
    )
    result = read_fault_file(path, "attendance", now=NOW)
    assert result is not None
    assert len(result.faults) == 1
    assert result.faults[0].code == "audit_unreadable"
    assert result.faults[0].blocks_turns is False


def test_blocks_turns_is_forced_from_the_fixed_table_not_the_file(tmp_path: Path) -> None:
    """The table in contract 05 section 3.3 is authoritative; a fault file
    is untrusted input and must not be able to lie about it."""
    path = tmp_path / "chat.json"
    write(
        path,
        {
            "family": "chat",
            "written_at": "2026-09-19T11:59:00Z",
            "faults": [
                {"code": "grants_stale", "blocks_turns": False, "since": "x", "source": "pep"}
            ],
        },
    )
    result = read_fault_file(path, "pep", now=NOW)
    assert result is not None
    assert result.faults[0].blocks_turns is True


def test_an_unknown_code_is_dropped(tmp_path: Path) -> None:
    path = tmp_path / "chat.json"
    write(
        path,
        {
            "family": "chat",
            "written_at": "2026-09-19T11:59:00Z",
            "faults": [{"code": "not_a_real_code", "since": "x", "source": "pep"}],
        },
    )
    result = read_fault_file(path, "pep", now=NOW)
    assert result is not None
    assert result.faults == ()


def test_a_code_belonging_to_a_different_source_is_dropped(tmp_path: Path) -> None:
    """`grants_stale` is the PEP's code. An attendance-written file claiming
    it is either a bug in attendance or a forged file, and either way it is
    dropped, not trusted (rule 6)."""
    path = tmp_path / "chat.json"
    write(
        path,
        {
            "family": "chat",
            "written_at": "2026-09-19T11:59:00Z",
            "faults": [{"code": "grants_stale", "since": "x", "source": "attendance"}],
        },
    )
    result = read_fault_file(path, "attendance", now=NOW)
    assert result is not None
    assert result.faults == ()


def test_one_malformed_entry_does_not_drop_the_rest(tmp_path: Path) -> None:
    path = tmp_path / "chat.json"
    write(
        path,
        {
            "family": "chat",
            "written_at": "2026-09-19T11:59:00Z",
            "faults": [
                "not even an object",
                {
                    "code": "audit_unreadable",
                    "since": "2026-09-19T11:58:00Z",
                    "source": "attendance",
                },
                {"blocks_turns": True},  # no code
            ],
        },
    )
    result = read_fault_file(path, "attendance", now=NOW)
    assert result is not None
    assert [f.code for f in result.faults] == ["audit_unreadable"]


def test_a_fresh_file_is_not_stale(tmp_path: Path) -> None:
    path = tmp_path / "chat.json"
    write(
        path,
        {
            "family": "chat",
            "written_at": (NOW - timedelta(seconds=30)).isoformat().replace("+00:00", "Z"),
            "faults": [{"code": "grants_stale", "since": "x", "source": "pep"}],
        },
    )
    result = read_fault_file(path, "pep", now=NOW)
    assert result is not None
    assert result.faults[0].stale is False


def test_a_file_older_than_90_seconds_marks_every_entry_stale(tmp_path: Path) -> None:
    path = tmp_path / "chat.json"
    write(
        path,
        {
            "family": "chat",
            "written_at": (NOW - timedelta(seconds=91)).isoformat().replace("+00:00", "Z"),
            "faults": [{"code": "grants_stale", "since": "x", "source": "pep"}],
        },
    )
    result = read_fault_file(path, "pep", now=NOW)
    assert result is not None
    assert result.faults[0].stale is True


def test_an_unparseable_written_at_counts_as_stale(tmp_path: Path) -> None:
    path = tmp_path / "chat.json"
    write(
        path,
        {
            "family": "chat",
            "written_at": "not a timestamp",
            "faults": [{"code": "grants_stale", "since": "x", "source": "pep"}],
        },
    )
    result = read_fault_file(path, "pep", now=NOW)
    assert result is not None
    assert result.faults[0].stale is True


# --- the vmm, the third writer (section 3.3.1, decision CH21-1) ---------------

#: The bytes that the vmm writes for one `sandbox_start_failed`. The Rust
#: test `the_bytes_match_the_caregiver_fixture` holds the writer of
#: `vmm-confine` equal to this file.
VMM_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "rust"
    / "crates"
    / "vmm-confine"
    / "tests"
    / "fixtures"
    / "caregiver_fault.json"
)


def one_fault(path: Path, source: str, code: str) -> None:
    write(
        path,
        {
            "family": "chat",
            "source": source,
            "written_at": (NOW - timedelta(seconds=5)).isoformat().replace("+00:00", "Z"),
            "faults": [
                {"code": code, "blocks_turns": True, "since": "2026-09-19T11:58:41Z"},
            ],
        },
    )


def test_the_bytes_the_vmm_writes_read_as_one_fault(tmp_path: Path) -> None:
    path = tmp_path / "chat.json"
    path.write_bytes(VMM_FIXTURE.read_bytes())

    result = read_fault_file(path, "vmm", now=NOW)

    assert result is not None
    assert result.family == "chat"
    [fault] = result.faults
    assert fault.code == "sandbox_start_failed"
    assert fault.source == "vmm"
    assert fault.blocks_turns is False
    assert fault.detail == {"message": "playpen answered fatal: mount_dir_unset"}


VMM_CODES = ("sandbox_start_failed", "egress_assert_failed", "serial_log_full")


@pytest.mark.parametrize("code", VMM_CODES)
def test_no_fault_of_the_vmm_blocks_turns(tmp_path: Path, code: str) -> None:
    """Decision CH82 rules 2 and 6: the next turn tries a start, and each
    start reruns every check, which fails closed. The file says `true`, and
    the table of section 3.3 wins."""
    path = tmp_path / "chat.json"
    one_fault(path, "vmm", code)

    result = read_fault_file(path, "vmm", now=NOW)

    assert result is not None
    assert [(one.code, one.blocks_turns) for one in result.faults] == [(code, False)]


def test_attendance_reports_a_vmm_mismatch_that_blocks_no_turn(tmp_path: Path) -> None:
    """Decisions CH25 rule 1 and CH82 rule 3."""
    path = tmp_path / "chat.json"
    one_fault(path, "attendance", "vmm_protocol_mismatch")

    result = read_fault_file(path, "attendance", now=NOW)

    assert result is not None
    assert [(one.code, one.blocks_turns) for one in result.faults] == [
        ("vmm_protocol_mismatch", False)
    ]


@pytest.mark.parametrize(
    ("source", "code"),
    [
        # The start faults are the vmm's since minor 18 (decision CH21-1).
        ("attendance", "sandbox_start_failed"),
        ("attendance", "egress_assert_failed"),
        ("pep", "serial_log_full"),
        ("vmm", "vmm_protocol_mismatch"),
        ("vmm", "grants_stale"),
        # Minor 18 drops the codes whose only source went at the cut.
        ("attendance", "protocol_mismatch"),
        ("attendance", "protocol_violation"),
        ("attendance", "orphan_processes"),
        ("vmm", "image_behind"),
    ],
)
def test_a_code_the_writer_may_not_raise_is_dropped(tmp_path: Path, source: str, code: str) -> None:
    """Rule 6: a code of another writer, or a code outside the table."""
    path = tmp_path / "chat.json"
    one_fault(path, source, code)

    result = read_fault_file(path, source, now=NOW)

    assert result is not None
    assert result.faults == ()
