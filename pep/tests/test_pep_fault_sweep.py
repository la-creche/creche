"""The sweep that clears a fault without traffic (contract 04 §1.6 rule 7).

`grants_stale` blocks turns (contract 05 §3.3), and `attendance` refuses every
turn of a family that carries a turn-blocking fault. If only a call re-read
the grant file, a family whose grant file was missing for one second would
stay down: no call, no re-read, no clear. The sweep is the traffic-free half
of that loop.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from agent_pep.__main__ import _sweep_interval
from agent_pep.family_grants import FamilyStore
from agent_pep.fault_sweep import FAULT_SWEEP_INTERVAL_S, fault_sweep_loop
from agent_pep.faults import FaultWriter
from pep_family_helpers import make_grants, write_grants


def _store(tmp_path: Path) -> tuple[FamilyStore, Path]:
    grants_dir = tmp_path / "grants"
    grants_dir.mkdir()
    faults = FaultWriter(tmp_path / "faults" / "pep")
    return FamilyStore(grants_dir, faults), grants_dir


def _faults_of(tmp_path: Path, family: str) -> list[dict[str, object]]:
    raw = (tmp_path / "faults" / "pep" / f"{family}.json").read_text(encoding="utf-8")
    entries = json.loads(raw)["faults"]
    assert isinstance(entries, list)
    return entries


def test_a_returned_grant_file_clears_the_fault(tmp_path: Path) -> None:
    """The deadlock, end to end, with no call in it."""
    store, grants_dir = _store(tmp_path)
    write_grants(grants_dir, make_grants())
    store.scan()
    (grants_dir / "chat.json").unlink()
    store.scan()
    assert _faults_of(tmp_path, "chat") != []

    # `caregiver` writes the real grant file. Nothing calls the PEP.
    write_grants(grants_dir, make_grants())
    assert store.sweep_faults() == frozenset({"chat"})
    assert _faults_of(tmp_path, "chat") == []


def test_an_absent_file_keeps_its_fault(tmp_path: Path) -> None:
    """Absence may be an accident. `caregiver` decides what it means, so the
    sweep renews the fault instead of clearing it."""
    store, grants_dir = _store(tmp_path)
    write_grants(grants_dir, make_grants())
    store.scan()
    (grants_dir / "chat.json").unlink()
    store.scan()

    assert store.sweep_faults() == frozenset()
    assert _faults_of(tmp_path, "chat") != []


def test_a_clean_family_is_never_swept(tmp_path: Path) -> None:
    """The sweep visits families that carry a fault, and no others: on a
    healthy fleet it costs no `stat` at all."""
    store, grants_dir = _store(tmp_path)
    write_grants(grants_dir, make_grants())
    store.scan()

    assert store.sweep_faults() == frozenset()
    assert not (tmp_path / "faults" / "pep" / "chat.json").exists()


def test_a_store_with_no_directory_sweeps_nothing(tmp_path: Path) -> None:
    store = FamilyStore(None, FaultWriter(None))
    assert store.sweep_faults() == frozenset()


def test_a_sweep_failure_never_stops_the_loop() -> None:
    """A sweep that raises is logged and the next one still runs. The PEP
    keeps serving: a background timer may not take the process down."""
    seen: list[int] = []

    def flaky() -> frozenset[str]:
        seen.append(len(seen))
        if len(seen) == 1:
            raise OSError("grants directory went away")
        return frozenset()

    async def drive() -> None:
        task = asyncio.create_task(fault_sweep_loop(flaky, 0.001))
        while len(seen) < 3:
            await asyncio.sleep(0.001)
        task.cancel()

    asyncio.run(drive())
    assert len(seen) >= 3


def test_the_interval_comes_from_the_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo must not stop the sweep. The fault it clears blocks every turn
    of the family, so the default is the safer answer to an unusable value."""
    monkeypatch.setenv("PEP_FAULT_SWEEP_INTERVAL_S", "2.5")
    assert _sweep_interval() == 2.5

    for unusable in ("", "soon", "0", "-1"):
        monkeypatch.setenv("PEP_FAULT_SWEEP_INTERVAL_S", unusable)
        assert _sweep_interval() == FAULT_SWEEP_INTERVAL_S


def test_the_interval_stays_under_the_refresh() -> None:
    """Contract 04 §1.6 rule 4 rewrites an open fault every 30 s and contract
    05 §3.3.1 rule 7 calls a file older than 90 s stale. A sweep slower than
    either would let a live PEP read as a dead one."""
    assert 0 < FAULT_SWEEP_INTERVAL_S < 30
