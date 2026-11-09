"""The fault file (contract 05 §3.3.1)."""

from __future__ import annotations

import json
import stat
from pathlib import Path

from attendance.atomic import TEMP_SUFFIX
from attendance.faults import FAULT_SOURCE, FaultCode, FaultReporter, blocks_turns
from attendance.paths import fault_file, faults_dir

_FAMILY = "chat"


def test_fault_file_shape_and_mode(tmp_path: Path) -> None:
    reporter = FaultReporter(tmp_path)
    reporter.raise_fault(_FAMILY, FaultCode.VMM_PROTOCOL_MISMATCH, "protocol 3 against 2")

    path = fault_file(tmp_path, _FAMILY)
    payload = json.loads(path.read_text())

    assert payload["family"] == _FAMILY
    assert payload["source"] == FAULT_SOURCE
    assert len(payload["faults"]) == 1

    entry = payload["faults"][0]
    assert entry["code"] == "vmm_protocol_mismatch"
    assert entry["blocks_turns"] is False
    assert entry["source"] == FAULT_SOURCE
    assert "sandbox" not in entry

    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_blocks_turns_follows_the_contract_table() -> None:
    """Contract 05 §3.3, decision CH82 rule 3: no fault of this writer
    blocks turns."""
    assert {code.value for code in FaultCode} == {"vmm_protocol_mismatch", "audit_unreadable"}
    assert not any(blocks_turns(code) for code in FaultCode)


def test_an_empty_list_clears_the_faults(tmp_path: Path) -> None:
    reporter = FaultReporter(tmp_path)
    reporter.raise_fault(_FAMILY, FaultCode.AUDIT_UNREADABLE, "the audit is not readable")
    reporter.clear(_FAMILY, FaultCode.AUDIT_UNREADABLE)

    payload = json.loads(fault_file(tmp_path, _FAMILY).read_text())

    assert payload["faults"] == []


def test_since_keeps_the_first_sighting(tmp_path: Path) -> None:
    reporter = FaultReporter(tmp_path)
    reporter.raise_fault(_FAMILY, FaultCode.AUDIT_UNREADABLE, "first")
    first = reporter.open_faults(_FAMILY)[0].since

    reporter.raise_fault(_FAMILY, FaultCode.AUDIT_UNREADABLE, "second")
    again = reporter.open_faults(_FAMILY)[0]

    assert again.since == first
    assert again.message == "second"


def test_start_empties_the_file_of_each_family(tmp_path: Path) -> None:
    """Contract 05 §3.3.1 rule 8: a fresh process has observed no fault. A
    family with no state dir keeps no fault, as in the vmm's start."""
    (tmp_path / "families" / "kept").mkdir(parents=True)
    before = FaultReporter(tmp_path)
    before.raise_fault("removed", FaultCode.VMM_PROTOCOL_MISMATCH, "major 2 against 1")
    before.raise_fault(_FAMILY, FaultCode.AUDIT_UNREADABLE)

    reporter = FaultReporter(tmp_path)
    reporter.start()

    for family in ("removed", _FAMILY):
        payload = json.loads(fault_file(tmp_path, family).read_text())
        assert payload["family"] == family
        assert payload["faults"] == []

    assert not fault_file(tmp_path, "kept").exists()


def test_start_forgets_the_faults_in_memory(tmp_path: Path) -> None:
    reporter = FaultReporter(tmp_path)
    reporter.raise_fault(_FAMILY, FaultCode.VMM_PROTOCOL_MISMATCH, "major 2 against 1")
    reporter.start()

    assert reporter.open_faults(_FAMILY) == []
    assert json.loads(fault_file(tmp_path, _FAMILY).read_text())["faults"] == []


def test_start_removes_a_temp_file_of_a_crash(tmp_path: Path) -> None:
    directory = faults_dir(tmp_path)
    directory.mkdir(parents=True)
    temp = directory / f".{_FAMILY}.json.4242.0{TEMP_SUFFIX}"
    temp.write_text('{"faults": [', encoding="utf-8")
    other = directory / "notes.txt"
    other.write_text("not a fault file", encoding="utf-8")

    FaultReporter(tmp_path).start()

    assert sorted(entry.name for entry in directory.iterdir()) == ["notes.txt"]
    assert other.read_text(encoding="utf-8") == "not a fault file"


def test_start_makes_a_missing_dir(tmp_path: Path) -> None:
    FaultReporter(tmp_path).start()

    directory = faults_dir(tmp_path)
    assert list(directory.iterdir()) == []
    assert stat.S_IMODE(directory.stat().st_mode) == 0o750
