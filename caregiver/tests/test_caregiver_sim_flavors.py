"""Which image a family's sandbox is created from.

`playpen/Dockerfile` builds two targets and the platform is started
with one image reference per flavor. The family file names the flavor;
this module holds `caregiver` to resolving it, to replacing the sandbox
when it moves, and to raising a fault rather than falling back to `base`
when the platform was given no image for the flavor a family asked for.

    family.yaml sandbox.image: python
      -> SandboxImages(base=..., python=...).resolve("python")
      -> sbx create ... -t <the python reference>

A fall-back would run a family on a filesystem its file does not ask for,
and nothing anywhere would say so."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from agent_family import SandboxFlavor, load_registry
from caregiver.applied import read_applied
from caregiver.driver import FakeDriver, SandboxSpec
from caregiver.egress import EgressConfig
from caregiver.images import IMAGE_FLAVOR_UNCONFIGURED, SandboxImages
from caregiver.litellm_keys import FakeLiteLLMKeys
from caregiver.reconcile import Actors, ReconcileResult, reconcile_family
from caregiver.switch import FakeSwitchClient
from caregiver.timers import FakeUnits
from caregiver_helpers import write_registry

from caregiver import paths, sandboxes

FAMILY: str = "chat"
BASE_IMAGE: str = "127.0.0.1:5000/agent-sandbox@sha256:aaaa"
PYTHON_IMAGE: str = "127.0.0.1:5000/agent-sandbox-python@sha256:bbbb"

BOTH: SandboxImages = SandboxImages(base=BASE_IMAGE, python=PYTHON_IMAGE)
BASE_ONLY: SandboxImages = SandboxImages(base=BASE_IMAGE)


# --- the mapping itself ------------------------------------------------------


def test_resolve_answers_the_reference_for_each_flavor() -> None:
    assert BOTH.resolve(str(SandboxFlavor.BASE)) == BASE_IMAGE
    assert BOTH.resolve(str(SandboxFlavor.PYTHON)) == PYTHON_IMAGE


def test_a_flavor_with_no_reference_resolves_to_none() -> None:
    assert BASE_ONLY.resolve(str(SandboxFlavor.PYTHON)) is None


def test_an_unknown_flavor_resolves_to_none() -> None:
    """The validator refuses one, so this can only be a file that reached
    the reconciler another way. It still never falls back to `base`."""
    assert BOTH.resolve("rust") is None


def test_configured_names_the_flavors_this_platform_can_serve() -> None:
    assert BOTH.configured() == ("base", "python")
    assert BASE_ONLY.configured() == ("base",)


# --- the reconciler ----------------------------------------------------------


@dataclass
class Fleet:
    registry_root: Path
    state_root: Path
    images: SandboxImages = BOTH
    driver: FakeDriver = field(default_factory=FakeDriver)
    litellm: FakeLiteLLMKeys = field(default_factory=FakeLiteLLMKeys)
    switch: FakeSwitchClient = field(default_factory=FakeSwitchClient)
    units: FakeUnits = field(default_factory=FakeUnits)

    def write(self, **overrides: object) -> None:
        write_registry(self.registry_root, **overrides)

    def run(self) -> ReconcileResult:
        actors = Actors(self.driver, self.litellm, self.switch, EgressConfig(), self.units)
        return reconcile_family(
            load_registry(self.registry_root),
            FAMILY,
            state_root=self.state_root,
            image=self.images.base,
            python_image=self.images.python,
            actors=actors,
        )

    def created(self) -> tuple[str, ...]:
        """The image reference of every `sbx create` this pass made."""
        specs = (call.args[0] for call in self.driver.calls if call.op == "create")
        return tuple(one.image for one in specs if isinstance(one, SandboxSpec))

    def live_ids(self) -> tuple[str, ...]:
        return tuple(
            one.id
            for one in sandboxes.read_ledger(self.state_root, FAMILY)
            if one.state in sandboxes.LIVE_STATES
        )


@pytest.fixture
def fleet(tmp_path: Path) -> Fleet:
    return Fleet(tmp_path / "registry", tmp_path / "state")


def test_a_family_with_no_image_field_runs_on_base(fleet: Fleet) -> None:
    fleet.write()
    fleet.run()
    assert fleet.created() == (BASE_IMAGE,)


def test_a_family_that_asks_for_python_runs_on_the_python_image(fleet: Fleet) -> None:
    fleet.write(sandbox={"cpus": 2, "memory": "2g", "image": "python"})
    result = fleet.run()
    assert fleet.created() == (PYTHON_IMAGE,)
    assert not result.status.faults


def test_the_status_document_names_the_image_the_sandbox_was_made_from(fleet: Fleet) -> None:
    fleet.write(sandbox={"cpus": 2, "memory": "2g", "image": "python"})
    result = fleet.run()
    assert [one.image for one in result.status.sandboxes] == [PYTHON_IMAGE]


def test_the_applied_snapshot_records_the_resolved_reference(fleet: Fleet) -> None:
    fleet.write(sandbox={"cpus": 2, "memory": "2g", "image": "python"})
    fleet.run()
    applied = read_applied(fleet.state_root, FAMILY)
    assert applied is not None
    assert applied.image == PYTHON_IMAGE


def test_changing_the_flavor_replaces_the_sandbox(fleet: Fleet) -> None:
    fleet.write()
    fleet.run()
    fleet.driver.calls.clear()

    fleet.write(sandbox={"cpus": 2, "memory": "2g", "image": "python"})
    result = fleet.run()

    assert fleet.created() == (PYTHON_IMAGE,)
    assert "switch_sandbox" in result.ran
    assert "destroy_sandbox" in result.ran


# --- the flavor this platform was given no image for -------------------------


def unconfigured(fleet: Fleet) -> Fleet:
    fleet.images = BASE_ONLY
    fleet.write(sandbox={"cpus": 2, "memory": "2g", "image": "python"})
    return fleet


def test_an_unconfigured_flavor_raises_a_fault_a_reader_can_act_on(fleet: Fleet) -> None:
    result = unconfigured(fleet).run()
    codes = [one.code for one in result.status.faults]
    assert codes == [IMAGE_FLAVOR_UNCONFIGURED]


def test_the_fault_names_the_flavor_and_what_this_platform_has(fleet: Fleet) -> None:
    result = unconfigured(fleet).run()
    detail = result.status.faults[0].detail
    assert detail["flavor"] == "python"
    assert detail["configured"] == ["base"]


def test_an_unconfigured_flavor_never_falls_back_to_base(fleet: Fleet) -> None:
    unconfigured(fleet).run()
    assert fleet.created() == ()


def test_an_unconfigured_flavor_leaves_the_applied_snapshot_alone(fleet: Fleet) -> None:
    unconfigured(fleet).run()
    assert read_applied(fleet.state_root, FAMILY) is None


def test_an_unconfigured_flavor_does_not_stop_the_family_serving(fleet: Fleet) -> None:
    """A manager that was not given an image is a platform fault, not a
    permission one. Blocking turns would take down a family that is still
    serving perfectly from the sandbox its last good revision built."""
    result = unconfigured(fleet).run()
    assert not result.status.faults[0].blocks_turns


def test_an_unconfigured_flavor_still_writes_the_grant_file(fleet: Fleet) -> None:
    """The sandbox axis is what stops. The PEP denies every call for a
    family with no grant file, and that is a worse outage than no
    sandbox."""
    unconfigured(fleet).run()
    assert paths.grant_path(fleet.state_root, FAMILY).is_file()


def test_a_family_already_serving_keeps_its_sandbox(fleet: Fleet) -> None:
    """Invariant 19's reading: an edit the platform cannot honour changes
    nothing, and the last good definition keeps serving."""
    fleet.write()
    fleet.run()
    before = fleet.live_ids()

    fleet.images = BASE_ONLY
    fleet.write(sandbox={"cpus": 2, "memory": "2g", "image": "python"})
    result = fleet.run()

    assert fleet.live_ids() == before
    assert [one.code for one in result.status.faults] == [IMAGE_FLAVOR_UNCONFIGURED]
