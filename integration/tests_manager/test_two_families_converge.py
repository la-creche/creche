"""Seam 5: two families converging at once, read by the real `attendance`.

Packet MCF. Until it, `caregiver` walked the families in sorted order and
ran one pass at a time. A sandbox replacement costs 1 to 3.5 minutes, so
the new image of 2026-09-21 took twenty minutes across eleven families,
and for all of it `attendance` read documents older than the 90 seconds
contract 05 §2 rule 5 allows.

    the real loop ──► families/ops/status.json  ──► StatusReader ──► serves
                 └──► families/chat/status.json ──► StatusReader ──► reconciling
                      (chat is held inside `sbx create` throughout)

The reader is `attendance`'s own, not an assertion about JSON: what matters
is that the second family is servable while the first is stuck, and that
the first one's document says what it is doing rather than going stale.

`loop.look` with a `Passes` of its own is exactly what `serve` runs each
tick; `serve` adds the tick and the stop, which `caregiver/tests` covers
without a second service in the room."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Final

import pytest
from attendance.family_status import FamilyState, StatusReader, check_may_serve
from caregiver.driver import FakeDriver, SandboxSpec
from caregiver.egress import EgressConfig
from caregiver.litellm_keys import FakeLiteLLMKeys
from caregiver.loop import LoopConfig, LoopState, Passes, look
from caregiver.reconcile import Actors
from caregiver.switch import FakeSwitchClient
from caregiver.timers import FakeUnits

from .conftest import CHAT_FAMILY, FAMILY, IMAGE, write_family

#: The second family. Same shape as `chat`, another name, so the only
#: difference between the two is which one the driver holds.
OTHER: Final = "ops"

#: Every wait here is bounded, and none of them is a sleep: a test that
#: would hang fails instead.
WAIT_S: Final = 30.0


class HoldingDriver(FakeDriver):
    """Holds one family inside `sbx create`, the way a first image pull
    holds the real one for three and a half minutes."""

    def __init__(self, hold: str) -> None:
        super().__init__()
        self._hold = hold
        self._inside = threading.Event()
        self.release = threading.Event()

    def create(self, spec: SandboxSpec) -> None:
        if spec.name.startswith(f"{self._hold}-s"):
            self._inside.set()
            self.release.wait(WAIT_S)

        super().create(spec)

    def wait_inside(self) -> None:
        assert self._inside.wait(WAIT_S), f"{self._hold} never reached its create"


@pytest.fixture
def two_families(tmp_path: Path) -> Path:
    root = tmp_path / "registry"
    write_family(root, CHAT_FAMILY)
    other: dict[str, Any] = {**CHAT_FAMILY, "name": OTHER}
    write_family(root, other)
    return root


def test_the_second_family_serves_while_the_first_is_held(
    two_families: Path, tmp_path: Path
) -> None:
    """Invariant 9 for the family that is NOT busy: its permissions land
    and `attendance` may run a turn against them, with no reference to what
    the other family's sandbox is doing."""
    state_root = tmp_path / "state"
    driver = HoldingDriver(hold=FAMILY)
    config = LoopConfig(registry_root=two_families, state_root=state_root, image=IMAGE)
    actors = Actors(driver, FakeLiteLLMKeys(), FakeSwitchClient(), EgressConfig(), FakeUnits())
    reader = StatusReader(state_root)

    with Passes(config.max_concurrent_passes, grace_s=WAIT_S) as passes:
        assert look(config, actors, LoopState(), passes) == (FAMILY, OTHER)
        assert passes.wait_done(OTHER, WAIT_S) is True
        driver.wait_inside()

        served = reader.require(OTHER)
        check_may_serve(served)
        assert served.state is FamilyState.IN_SYNC
        assert served.is_stale() is False

        # And the held one is not silent: it says which step it is inside.
        held = reader.require(FAMILY)
        assert held.state is FamilyState.RECONCILING
        assert held.is_stale() is False

        driver.release.set()
        assert passes.drain(WAIT_S) is True

    assert reader.require(FAMILY).state is FamilyState.IN_SYNC
