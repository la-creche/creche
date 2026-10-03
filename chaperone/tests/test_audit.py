"""Audit files get a real 180 d retention sweep."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

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
