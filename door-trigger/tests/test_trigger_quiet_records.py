"""`quiet/records.py` against real files under `tmp_path`: `sessiond`'s
outcome records and the PEP's audit, in their contract shapes."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agent_door_trigger.quiet.records import Called, Ending, HostRecords

FAMILY = "scrum-lead"
CALL = "mail__send_standup_email"
FIRED = datetime(2026, 9, 25, 10, 0, tzinfo=UTC)
#: 16:00 in Phoenix, as UTC: the audit's file name is the UTC day.
AFTERNOON = datetime(2026, 9, 25, 23, 0, tzinfo=UTC)
MIDNIGHT = datetime(2026, 9, 25, 7, 0, tzinfo=UTC)


def _records(tmp_path: Path) -> HostRecords:
    return HostRecords(tmp_path / "outcomes", tmp_path / "audit")


def _outcome(tmp_path: Path, name: str, session: str, status: str, at: datetime) -> None:
    """Contract 02 §13.1's record, with the file time the write gave it."""
    directory = tmp_path / "outcomes" / FAMILY
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.json"
    path.write_text(json.dumps({"session": session, "status": status}), encoding="utf-8")
    os.utime(path, (at.timestamp(), at.timestamp()))


def _audit(tmp_path: Path, day: str, *records: dict[str, Any]) -> None:
    """Contract 04 §6.1's records, one per line."""
    directory = tmp_path / "audit"
    directory.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(one) for one in records]
    (directory / f"{day}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _call(
    ts: str, *, family: str = FAMILY, tool: str = CALL, reason: str = "granted"
) -> dict[str, Any]:
    return {"ts": ts, "family": family, "tool": tool, "decision": "allow", "reason": reason}


# --- ending: how the firing the gate let through ended ---


def test_an_ok_record_is_ok(tmp_path: Path) -> None:
    _outcome(tmp_path, "01OUT", "auto-A", "ok", FIRED + timedelta(minutes=5))

    assert _records(tmp_path).ending(FAMILY, "auto-A", FIRED) is Ending.OK


def test_any_other_status_failed(tmp_path: Path) -> None:
    _outcome(tmp_path, "01OUT", "auto-A", "timeout", FIRED + timedelta(minutes=5))

    assert _records(tmp_path).ending(FAMILY, "auto-A", FIRED) is Ending.FAILED


def test_no_record_yet_is_pending(tmp_path: Path) -> None:
    _outcome(tmp_path, "01OUT", "auto-OTHER", "ok", FIRED + timedelta(minutes=5))

    assert _records(tmp_path).ending(FAMILY, "auto-A", FIRED) is Ending.PENDING


def test_no_directory_is_pending(tmp_path: Path) -> None:
    assert _records(tmp_path).ending(FAMILY, "auto-A", FIRED) is Ending.PENDING


def test_a_record_older_than_the_firing_is_not_read(tmp_path: Path) -> None:
    """The file time bounds the scan: the directory keeps every job."""
    _outcome(tmp_path, "01OUT", "auto-A", "ok", FIRED - timedelta(minutes=5))

    assert _records(tmp_path).ending(FAMILY, "auto-A", FIRED) is Ending.PENDING


def test_a_broken_record_is_skipped(tmp_path: Path) -> None:
    _outcome(tmp_path, "01OUT", "auto-A", "ok", FIRED + timedelta(minutes=5))
    (tmp_path / "outcomes" / FAMILY / "01BAD.json").write_text("{", encoding="utf-8")

    assert _records(tmp_path).ending(FAMILY, "auto-A", FIRED) is Ending.OK


# --- called: the daily call, from the audit ---


def test_a_granted_call_since_midnight_is_found(tmp_path: Path) -> None:
    _audit(tmp_path, "2026-09-25", _call("2026-09-25T23:01:02.345Z"))

    assert _records(tmp_path).called(FAMILY, CALL, MIDNIGHT, AFTERNOON) is Called.YES


def test_a_tapped_call_is_found(tmp_path: Path) -> None:
    _audit(tmp_path, "2026-09-25", _call("2026-09-25T23:01:02.345Z", reason="approved"))

    assert _records(tmp_path).called(FAMILY, CALL, MIDNIGHT, AFTERNOON) is Called.YES


def test_a_failed_upstream_is_no_call(tmp_path: Path) -> None:
    """`upstream_failed` is `allow`, and the mail never went."""
    _audit(tmp_path, "2026-09-25", _call("2026-09-25T23:01:02.345Z", reason="upstream_failed"))

    assert _records(tmp_path).called(FAMILY, CALL, MIDNIGHT, AFTERNOON) is Called.NO


def test_another_family_or_tool_is_no_call(tmp_path: Path) -> None:
    _audit(
        tmp_path,
        "2026-09-25",
        _call("2026-09-25T23:01:02.345Z", family="chat"),
        _call("2026-09-25T23:01:02.345Z", tool="mail__send_standup_email_v2"),
    )

    assert _records(tmp_path).called(FAMILY, CALL, MIDNIGHT, AFTERNOON) is Called.NO


def test_yesterdays_call_is_no_call(tmp_path: Path) -> None:
    _audit(tmp_path, "2026-09-25", _call("2026-09-25T06:59:59.000Z"))

    assert _records(tmp_path).called(FAMILY, CALL, MIDNIGHT, AFTERNOON) is Called.NO


def test_the_local_day_spans_two_utc_files(tmp_path: Path) -> None:
    """16:30 in Phoenix is past midnight UTC."""
    _audit(tmp_path, "2026-09-26", _call("2026-09-26T00:10:00.000Z"))
    late = datetime(2026, 9, 26, 0, 30, tzinfo=UTC)

    assert _records(tmp_path).called(FAMILY, CALL, MIDNIGHT, late) is Called.YES


def test_no_audit_file_is_no_call(tmp_path: Path) -> None:
    assert _records(tmp_path).called(FAMILY, CALL, MIDNIGHT, AFTERNOON) is Called.NO


def test_an_audit_it_cannot_read_is_unknown(tmp_path: Path) -> None:
    """A user unit whose manager predates the `agents` group reads nothing
    here (systemd/AGENTS.md). The gate wakes on it rather than guess."""
    (tmp_path / "audit" / "2026-09-25.jsonl").mkdir(parents=True)

    assert _records(tmp_path).called(FAMILY, CALL, MIDNIGHT, AFTERNOON) is Called.UNKNOWN


def test_a_broken_line_is_skipped(tmp_path: Path) -> None:
    directory = tmp_path / "audit"
    directory.mkdir()
    good = json.dumps(_call("2026-09-25T23:01:02.345Z"))
    (directory / "2026-09-25.jsonl").write_text(f"{{{CALL}\n{good}\n", encoding="utf-8")

    assert _records(tmp_path).called(FAMILY, CALL, MIDNIGHT, AFTERNOON) is Called.YES
