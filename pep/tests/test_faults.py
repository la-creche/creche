"""The PEP's fault file (contract 04 §1.6, contract 05 §3.3.1)."""

from __future__ import annotations

import json
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_pep.faults import FAULT_REFRESH_S, GRANTS_STALE, FaultWriter


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _read(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_no_directory_writes_nothing(tmp_path: Path) -> None:
    writer = FaultWriter(None)
    writer.raise_stale("chat", "grants/chat.json: absent", None)
    writer.clear("chat")
    assert list(tmp_path.iterdir()) == []
    assert writer.open_families() == frozenset()


def test_raise_writes_the_contract_shape(tmp_path: Path) -> None:
    clock = _Clock(datetime(2026, 9, 18, 19, 20, 41, tzinfo=UTC))
    writer = FaultWriter(tmp_path / "pep", clock=clock)
    writer.raise_stale("chat", "grants/chat.json: unknown version 3", "reg-8c01aa")

    document = _read(tmp_path / "pep" / "chat.json")
    assert document["family"] == "chat"
    assert document["source"] == "pep"
    assert document["written_at"] == "2026-09-18T19:20:41Z"
    faults = document["faults"]
    assert isinstance(faults, list)
    assert faults == [
        {
            "code": GRANTS_STALE,
            "blocks_turns": True,
            "since": "2026-09-18T19:20:41Z",
            "source": "pep",
            "message": "grants/chat.json: unknown version 3",
            "rev": "reg-8c01aa",
        }
    ]


def test_modes_are_pinned(tmp_path: Path) -> None:
    directory = tmp_path / "pep"
    writer = FaultWriter(directory)
    writer.raise_stale("chat", "absent", None)

    assert stat.S_IMODE(directory.stat().st_mode) == 0o750
    assert stat.S_IMODE((directory / "chat.json").stat().st_mode) == 0o640


def test_repeat_does_not_rewrite_until_the_refresh(tmp_path: Path) -> None:
    clock = _Clock(datetime(2026, 9, 18, 19, 20, 41, tzinfo=UTC))
    writer = FaultWriter(tmp_path / "pep", clock=clock)
    path = tmp_path / "pep" / "chat.json"

    writer.raise_stale("chat", "absent", None)
    clock.advance(FAULT_REFRESH_S / 2)
    writer.raise_stale("chat", "absent", None)
    assert _read(path)["written_at"] == "2026-09-18T19:20:41Z"

    # Past the interval the file is rewritten so it never reads as stale
    # (contract 05 §3.3.1 rule 7), and `since` still points at the first sight.
    clock.advance(FAULT_REFRESH_S)
    writer.raise_stale("chat", "absent", None)
    document = _read(path)
    assert document["written_at"] != "2026-09-18T19:20:41Z"
    faults = document["faults"]
    assert isinstance(faults, list)
    first = faults[0]
    assert isinstance(first, dict)
    assert first["since"] == "2026-09-18T19:20:41Z"


def test_a_changed_message_rewrites_at_once(tmp_path: Path) -> None:
    clock = _Clock(datetime(2026, 9, 18, 19, 20, 41, tzinfo=UTC))
    writer = FaultWriter(tmp_path / "pep", clock=clock)
    writer.raise_stale("chat", "absent", None)

    clock.advance(1)
    writer.raise_stale("chat", "unknown version 3", "reg-1")
    document = _read(tmp_path / "pep" / "chat.json")
    faults = document["faults"]
    assert isinstance(faults, list)
    first = faults[0]
    assert isinstance(first, dict)
    assert first["message"] == "unknown version 3"
    assert first["rev"] == "reg-1"
    # A different fault starts its own clock.
    assert first["since"] == "2026-09-18T19:20:42Z"


def test_clear_empties_the_list(tmp_path: Path) -> None:
    writer = FaultWriter(tmp_path / "pep")
    writer.raise_stale("chat", "absent", None)
    writer.clear("chat")

    assert _read(tmp_path / "pep" / "chat.json")["faults"] == []
    assert writer.open_families() == frozenset()


def test_clear_without_a_fault_writes_no_file(tmp_path: Path) -> None:
    writer = FaultWriter(tmp_path / "pep")
    writer.clear("chat")
    assert not (tmp_path / "pep" / "chat.json").exists()


def test_a_name_that_is_not_a_family_is_refused(tmp_path: Path) -> None:
    writer = FaultWriter(tmp_path / "pep")
    writer.raise_stale("../escape", "absent", None)

    assert list((tmp_path / "pep").iterdir()) == []
    assert writer.open_families() == frozenset()


def test_a_write_failure_is_not_fatal(tmp_path: Path) -> None:
    directory = tmp_path / "pep"
    writer = FaultWriter(directory)
    directory.chmod(0o500)
    try:
        writer.raise_stale("chat", "absent", None)
        # Nothing recorded, so the next attempt tries again rather than
        # believing a write that never landed.
        assert writer.open_families() == frozenset()
    finally:
        directory.chmod(0o750)


def test_a_clear_that_cannot_be_written_stays_open(tmp_path: Path) -> None:
    """The fault keeps its entry, so the next clear tries again rather than
    reporting a file the reader never got."""
    directory = tmp_path / "pep"
    writer = FaultWriter(directory)
    writer.raise_stale("chat", "absent", None)

    directory.chmod(0o500)
    try:
        writer.clear("chat")
        assert writer.open_families() == frozenset({"chat"})
    finally:
        directory.chmod(0o750)
