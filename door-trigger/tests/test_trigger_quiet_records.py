"""`quiet/records.py` against real files under `tmp_path`: `attendance`'s
outcome records and the chaperone's audit file, in their shapes."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from agent_door_trigger.quiet import records
from agent_door_trigger.quiet.records import Called, Ending, HostRecords
from trigger_json_limit import ParserAtItsLimit

FAMILY = "scrum-lead"
CALL = "mail__send_standup_email"
FIRED = datetime(2026, 9, 25, 10, 0, tzinfo=UTC)
#: 16:00 in Phoenix, as UTC.
AFTERNOON = datetime(2026, 9, 25, 23, 0, tzinfo=UTC)
MIDNIGHT = datetime(2026, 9, 25, 7, 0, tzinfo=UTC)

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
FIXTURE_DAY = datetime(2025, 10, 24, tzinfo=UTC)

RUN = "07" * 16
NO_ARGUMENTS_SHA256 = "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"


def _records(tmp_path: Path) -> HostRecords:
    return HostRecords(tmp_path / "outcomes", tmp_path / "audit.jsonl")


def _outcome(tmp_path: Path, name: str, session: str, status: str, at: datetime) -> None:
    """Contract 02 §13.1's record, with the file time the write gave it."""
    directory = tmp_path / "outcomes" / FAMILY
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.json"
    path.write_text(json.dumps({"session": session, "status": status}), encoding="utf-8")
    os.utime(path, (at.timestamp(), at.timestamp()))


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


def test_a_record_that_nests_too_deep_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _outcome(tmp_path, "01OUT", "auto-A", "ok", FIRED + timedelta(minutes=5))
    monkeypatch.setattr(records, "json", ParserAtItsLimit)

    assert _records(tmp_path).ending(FAMILY, "auto-A", FIRED) is Ending.PENDING


# --- called: the daily call, from the audit file ---


def _line(
    call: int,
    event: str,
    detail: str,
    time: str = "2026-09-25T23:01:02.345Z",
    *,
    app: str | None = FAMILY,
    tool: str = CALL,
) -> str:
    """One line in the shape of the Rust type `Line`."""
    record = {
        "run": RUN,
        "call": call,
        "started": time,
        "time": time,
        "app": app,
        "tool": tool,
        "arguments_sha256": NO_ARGUMENTS_SHA256,
        "event": event,
        "detail": detail,
    }
    return json.dumps(record, separators=(",", ":"))


def _ran(call: int = 1, time: str = "2026-09-25T23:01:02.345Z", **fields: str) -> list[str]:
    """An allowed call that ran with no error: `decided`, then `ran`."""
    return [
        _line(call, "decided", "allow", time, **fields),
        _line(call, "ran", "is_error:false", time, **fields),
    ]


def _audit(tmp_path: Path, *lines: str) -> None:
    (tmp_path / "audit.jsonl").write_text("".join(f"{one}\n" for one in lines), encoding="utf-8")


def test_audit_reads_rust_records(tmp_path: Path) -> None:
    ran = _ran(3, "2025-10-24T00:02:00.123Z")
    (tmp_path / "audit.jsonl").write_text(RUST_FIXTURE + "\n".join(ran) + "\n", encoding="utf-8")

    found = _records(tmp_path).called(FAMILY, CALL, FIXTURE_DAY, FIXTURE_DAY + timedelta(hours=1))

    assert found is Called.YES


def test_the_rust_fixture_alone_holds_no_call(tmp_path: Path) -> None:
    """A call that failed, and a denied call, of no app: neither ran."""
    (tmp_path / "audit.jsonl").write_text(RUST_FIXTURE, encoding="utf-8")

    found = _records(tmp_path).called(FAMILY, CALL, FIXTURE_DAY, FIXTURE_DAY + timedelta(hours=1))

    assert found is Called.NO


def test_a_call_that_ran_since_midnight_is_found(tmp_path: Path) -> None:
    _audit(tmp_path, *_ran())

    assert _records(tmp_path).called(FAMILY, CALL, MIDNIGHT, AFTERNOON) is Called.YES


def test_a_time_with_two_fraction_digits_is_read(tmp_path: Path) -> None:
    """The chaperone drops the trailing zeros of the milliseconds."""
    _audit(tmp_path, *_ran(time="2026-09-25T23:01:02.43Z"))

    assert _records(tmp_path).called(FAMILY, CALL, MIDNIGHT, AFTERNOON) is Called.YES


def test_a_gated_call_that_ran_is_found(tmp_path: Path) -> None:
    gate = "gate:" + "0a" * 16
    _audit(
        tmp_path,
        _line(1, "decided", gate),
        _line(1, "waiting", gate),
        _line(1, "ran", "is_error:false", "2026-09-25T23:20:00.000Z"),
    )

    assert _records(tmp_path).called(FAMILY, CALL, MIDNIGHT, AFTERNOON) is Called.YES


def test_a_call_that_ran_with_an_error_is_no_call(tmp_path: Path) -> None:
    """The upstream answered an error: the mail never went."""
    _audit(tmp_path, _line(1, "decided", "allow"), _line(1, "ran", "is_error:true"))

    assert _records(tmp_path).called(FAMILY, CALL, MIDNIGHT, AFTERNOON) is Called.NO


def test_a_call_that_could_not_run_is_no_call(tmp_path: Path) -> None:
    _audit(tmp_path, _line(1, "decided", "allow"), _line(1, "failed", "not_connected"))

    assert _records(tmp_path).called(FAMILY, CALL, MIDNIGHT, AFTERNOON) is Called.NO


def test_another_family_or_tool_is_no_call(tmp_path: Path) -> None:
    _audit(
        tmp_path,
        *_ran(1, app="chat"),
        *_ran(2, tool="mail__send_standup_email_v2"),
    )

    assert _records(tmp_path).called(FAMILY, CALL, MIDNIGHT, AFTERNOON) is Called.NO


def test_yesterdays_call_is_no_call(tmp_path: Path) -> None:
    _audit(tmp_path, *_ran(time="2026-09-25T06:59:59.000Z"))

    assert _records(tmp_path).called(FAMILY, CALL, MIDNIGHT, AFTERNOON) is Called.NO


def test_a_verb_is_read_as_its_tool(tmp_path: Path) -> None:
    """A daily call may name a verb. The audit names the tool of the verb."""
    _audit(
        tmp_path,
        *_ran(1, tool="attendance__enqueue"),
        *_ran(2, tool="release__request"),
    )

    found = _records(tmp_path)
    assert found.called(FAMILY, "enqueue", MIDNIGHT, AFTERNOON) is Called.YES
    assert found.called(FAMILY, "release", MIDNIGHT, AFTERNOON) is Called.YES
    assert found.called(FAMILY, "embed", MIDNIGHT, AFTERNOON) is Called.NO


def test_no_audit_file_is_no_call(tmp_path: Path) -> None:
    assert _records(tmp_path).called(FAMILY, CALL, MIDNIGHT, AFTERNOON) is Called.NO


def test_an_audit_it_cannot_read_is_unknown(tmp_path: Path) -> None:
    """A user unit whose manager predates the `chaperone` group reads nothing
    here. The gate wakes on it rather than guess."""
    (tmp_path / "audit.jsonl").mkdir()

    assert _records(tmp_path).called(FAMILY, CALL, MIDNIGHT, AFTERNOON) is Called.UNKNOWN


def test_a_broken_line_is_skipped(tmp_path: Path) -> None:
    _audit(tmp_path, "{" + CALL, *_ran())

    assert _records(tmp_path).called(FAMILY, CALL, MIDNIGHT, AFTERNOON) is Called.YES


def test_a_line_with_another_key_is_skipped(tmp_path: Path) -> None:
    decided, ran = _ran()
    extra = json.dumps(json.loads(ran) | {"args": {}})
    _audit(tmp_path, decided, extra)

    assert _records(tmp_path).called(FAMILY, CALL, MIDNIGHT, AFTERNOON) is Called.NO


def test_a_line_still_being_written_is_not_read(tmp_path: Path) -> None:
    """The sink writes a line and its LF in one write."""
    decided, ran = _ran()
    (tmp_path / "audit.jsonl").write_text(f"{decided}\n{ran}", encoding="utf-8")

    assert _records(tmp_path).called(FAMILY, CALL, MIDNIGHT, AFTERNOON) is Called.NO


def test_only_the_tail_is_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old = _ran(1)
    newer = _ran(2, tool="mail__other")
    _audit(tmp_path, *old, *newer)
    monkeypatch.setattr(records, "MAX_TAIL_BYTES", len(newer[0]) + len(newer[1]) + 10)

    assert _records(tmp_path).called(FAMILY, CALL, MIDNIGHT, AFTERNOON) is Called.NO


def test_a_line_that_nests_too_deep_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _audit(tmp_path, *_ran())
    monkeypatch.setattr(records, "json", ParserAtItsLimit)

    assert _records(tmp_path).called(FAMILY, CALL, MIDNIGHT, AFTERNOON) is Called.NO
