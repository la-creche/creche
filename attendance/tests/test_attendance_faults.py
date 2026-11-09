"""The fault file (contract 05 §3.3.1)."""

from __future__ import annotations

import json
import stat
from pathlib import Path

from attendance.faults import FAULT_SOURCE, FaultCode, FaultReporter, blocks_turns
from attendance.paths import fault_file

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


def test_clear_all_empties_the_file(tmp_path: Path) -> None:
    reporter = FaultReporter(tmp_path)
    reporter.raise_fault(_FAMILY, FaultCode.VMM_PROTOCOL_MISMATCH, "major 2 against 1")
    reporter.raise_fault(_FAMILY, FaultCode.AUDIT_UNREADABLE)
    reporter.clear_all(_FAMILY)

    payload = json.loads(fault_file(tmp_path, _FAMILY).read_text())

    assert payload["faults"] == []
    assert reporter.open_faults(_FAMILY) == []
