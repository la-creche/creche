"""`quiet/state.py`: the gate's own record survives a round trip, and one it
cannot read is an empty record, never a crash."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from agent_door_trigger.quiet import state as state_module
from agent_door_trigger.quiet.decide import Wake
from agent_door_trigger.quiet.state import GateState, StateFiles
from trigger_json_limit import ParserAtItsLimit

AT = datetime(2026, 9, 25, 10, 0, tzinfo=UTC)
FIRED = Wake(session="auto-B", at=AT, board="b" * 64, ended=frozenset({"auto-J"}))
GOOD = Wake(session="auto-A", at=AT, board=None, ended=None)


def test_a_record_reads_back_as_written(tmp_path: Path) -> None:
    store = StateFiles(tmp_path / "quiet")
    state = GateState(fired=FIRED, good=GOOD)

    store.write("scrum-lead", state)

    assert store.read("scrum-lead") == state


def test_no_record_is_an_empty_one(tmp_path: Path) -> None:
    assert StateFiles(tmp_path).read("scrum-lead") == GateState()


def test_a_broken_record_is_an_empty_one(tmp_path: Path) -> None:
    (tmp_path / "scrum-lead.json").write_text("{", encoding="utf-8")

    assert StateFiles(tmp_path).read("scrum-lead") == GateState()


def test_a_record_that_nests_too_deep_is_an_empty_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    StateFiles(tmp_path).write("scrum-lead", GateState(good=GOOD))
    monkeypatch.setattr(state_module, "json", ParserAtItsLimit)

    assert StateFiles(tmp_path).read("scrum-lead") == GateState()


def test_a_wake_with_no_zone_or_session_is_dropped(tmp_path: Path) -> None:
    (tmp_path / "scrum-lead.json").write_text(
        '{"fired": {"session": "auto-B", "at": "2026-09-25T10:00:00"},'
        ' "good": {"session": "", "at": "2026-09-25T10:00:00+00:00"}}',
        encoding="utf-8",
    )

    assert StateFiles(tmp_path).read("scrum-lead") == GateState()


def test_a_job_list_it_cannot_trust_is_none(tmp_path: Path) -> None:
    """None, not empty: an empty list would say every job ended long ago."""
    (tmp_path / "scrum-lead.json").write_text(
        '{"fired": null, "good": {"session": "auto-A", "at": "2026-09-25T10:00:00+00:00",'
        ' "board": null, "ended": ["auto-J", 7]}}',
        encoding="utf-8",
    )

    good = StateFiles(tmp_path).read("scrum-lead").good
    assert good is not None
    assert good.ended is None


def test_a_write_leaves_no_temp_file(tmp_path: Path) -> None:
    StateFiles(tmp_path).write("scrum-lead", GateState(good=GOOD))

    assert [one.name for one in tmp_path.iterdir()] == ["scrum-lead.json"]
