"""Audit files get a real 180 d retention sweep."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from chaperone.audit import AUDIT_RETENTION_DAYS, AuditLog


def _day(offset_days: int) -> str:
    return (datetime.now(UTC).date() - timedelta(days=offset_days)).isoformat()


def test_sweep_deletes_only_files_older_than_the_cutoff(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path)
    old = tmp_path / f"{_day(AUDIT_RETENTION_DAYS + 1)}.jsonl"
    recent = tmp_path / f"{_day(AUDIT_RETENTION_DAYS - 1)}.jsonl"
    old.write_text("{}\n", encoding="utf-8")
    recent.write_text("{}\n", encoding="utf-8")

    audit.sweep_retention()

    assert not old.exists()
    assert recent.exists()


def test_sweep_ignores_files_it_cannot_parse_as_a_date(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path)
    odd = tmp_path / "not-a-date.jsonl"
    odd.write_text("{}\n", encoding="utf-8")

    audit.sweep_retention()

    assert odd.exists()


def test_sweep_respects_a_custom_max_age(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path)
    three_days_old = tmp_path / f"{_day(3)}.jsonl"
    three_days_old.write_text("{}\n", encoding="utf-8")

    audit.sweep_retention(max_age_days=1)

    assert not three_days_old.exists()


def _nested(levels: int) -> object:
    value: object = "x"
    for _ in range(levels):
        value = [value]

    return value


#: More levels than the JSON writer of a supported interpreter writes.
TOO_DEEP = 200_000


@pytest.mark.parametrize(
    ("record", "error"),
    [
        pytest.param({"tool": "\ud800"}, ValueError, id="a-string-that-is-not-text"),
        pytest.param({"args": _nested(TOO_DEEP)}, RecursionError, id="nested-too-deep"),
    ],
)
def test_a_record_that_is_no_json_text_leaves_no_file(
    tmp_path: Path, record: dict[str, object], error: type[Exception]
) -> None:
    """The line is made before the file opens. A record that no line can
    hold then leaves no file and no part of a line."""
    audit = AuditLog(tmp_path)

    with pytest.raises(error):
        audit.write(record)

    assert list(tmp_path.iterdir()) == []


def test_a_line_is_the_bytes_of_its_json_text(tmp_path: Path) -> None:
    """Text outside ASCII is written as it is, in UTF-8, with one LF."""
    audit = AuditLog(tmp_path)

    audit.write({"ts": "t", "tool": "caf\u00e9"})

    (path,) = tmp_path.iterdir()
    assert path.read_bytes() == '{"ts": "t", "tool": "caf\u00e9"}\n'.encode()
