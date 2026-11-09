"""Seam 5: two families converging at once, read by the real `attendance`.

Packet MCF. Until it, `caregiver` walked the families in sorted order and
ran one pass at a time, so one slow call held every family behind it, and
`attendance` read documents older than the 90 seconds contract 05 §2 rule 5
allows.

    the real loop ──► families/ops/status.json  ──► StatusReader ──► serves
                 └──► families/chat/status.json ──► StatusReader ──► serves
                      (chat's pass is held inside its timer step)

The reader is `attendance`'s own, not an assertion about JSON: what matters
is that the second family is servable while the first is stuck, and that
the first one keeps a fresh document of its last pass.

`loop.look` with a `Passes` of its own is exactly what `serve` runs each
tick; `serve` adds the tick and the stop, which `caregiver/tests` covers
without a second service in the room."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Final

import pytest
from agent_family import FamilyFile
from attendance.family_status import FamilyState, StatusReader, check_may_serve
from caregiver.litellm_keys import FakeLiteLLMKeys
from caregiver.loop import LoopConfig, LoopState, Passes, look
from caregiver.policy_fleet import FakeChecker, FleetPolicy, PolicyTarget
from caregiver.reconcile import Actors
from caregiver.timers import FakeUnits, TimerOutcome, UnitWriter, apply_timers
from caregiver.vmm import VmmClient

from caregiver import reconcile as reconcile_module

from .conftest import CHAT_FAMILY, FAMILY, FENCES, write_family

#: The second family. Same shape as `chat`, another name, so the only
#: difference between the two is which one the hold holds.
OTHER: Final = "ops"

#: Every wait here is bounded, and none of them is a sleep: a test that
#: would hang fails instead.
WAIT_S: Final = 30.0


class NoVmm:
    """No family is deleted here, so no drop is ever asked for."""

    def drop_family(self, family: str) -> None:
        raise AssertionError(f"no delete in this test, and {family} was dropped")


class Hold:
    """Holds one family inside its timer step, the way one call that hangs
    holds the real pass."""

    def __init__(self, hold: str) -> None:
        self._hold = hold
        self._inside = threading.Event()
        self.release = threading.Event()

    def __call__(self, family: FamilyFile, units: UnitWriter) -> TimerOutcome:
        if family.name == self._hold:
            self._inside.set()
            self.release.wait(WAIT_S)

        return apply_timers(family, units)

    def wait_inside(self) -> None:
        assert self._inside.wait(WAIT_S), f"{self._hold} never reached its timer step"


@pytest.fixture
def two_families(tmp_path: Path) -> Path:
    root = tmp_path / "registry"
    write_family(root, CHAT_FAMILY)
    other: dict[str, Any] = {**CHAT_FAMILY, "name": OTHER}
    write_family(root, other)
    return root


def test_the_second_family_serves_while_the_first_is_held(
    two_families: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant 9 for the family that is NOT busy: its permissions land
    and `attendance` may run a turn against them, with no reference to what
    the other family's pass is doing."""
    state_root = tmp_path / "state"
    vmm: VmmClient = NoVmm()
    config = LoopConfig(registry_root=two_families, state_root=state_root, fences=FENCES, vmm=vmm)
    actors = Actors(
        FakeLiteLLMKeys(),
        FakeUnits(),
        FleetPolicy(PolicyTarget(state_root.parent / "chaperone"), FakeChecker()),
    )
    reader = StatusReader(state_root)
    state = LoopState()

    with Passes(config.max_concurrent_passes, grace_s=WAIT_S) as first:
        assert look(config, actors, state, first) == (FAMILY, OTHER)
        assert first.drain(WAIT_S) is True

    hold = Hold(FAMILY)
    monkeypatch.setattr(reconcile_module, "apply_timers", hold)
    for name in (FAMILY, OTHER):
        write_family(two_families, {**CHAT_FAMILY, "name": name, "description": "Edited."})

    with Passes(config.max_concurrent_passes, grace_s=WAIT_S) as passes:
        assert look(config, actors, state, passes) == (FAMILY, OTHER)
        assert passes.wait_done(OTHER, WAIT_S) is True
        hold.wait_inside()

        served = reader.require(OTHER)
        check_may_serve(served)
        assert served.state is FamilyState.IN_SYNC
        assert served.is_stale() is False

        # And the held one is not silent: its last document still serves.
        held = reader.require(FAMILY)
        check_may_serve(held)
        assert held.is_stale() is False

        hold.release.set()
        assert passes.drain(WAIT_S) is True

    assert reader.require(FAMILY).state is FamilyState.IN_SYNC
