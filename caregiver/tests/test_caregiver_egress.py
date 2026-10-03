"""The two plane endpoints, and the canary probe that proves denial.

Contract 01 §3.7 rule 4: LiteLLM at the host's LAN address, port 4000, and the PEP
at port 8300 are implicit and are never listed in a family file. Rule 5:
an empty `egress` list is the normal attended case. A sandbox built from
the family list alone therefore reaches neither plane, and every turn
fails. These tests keep both planes reachable."""

from __future__ import annotations

import os
from pathlib import Path

from caregiver.apply import ApplyResult, apply_once
from caregiver.driver import DriverError, FakeDriver, SandboxSpec
from caregiver.egress import (
    EgressConfig,
    default_canaries,
    litellm_endpoint,
    pep_endpoint,
)
from caregiver.lan import LAN_ADDRESS_ENV
from caregiver.litellm_keys import FakeLiteLLMKeys
from caregiver_helpers import write_registry

FAMILY: str = "chat"


class ReachableCanaryDriver(FakeDriver):
    """A sandbox that can reach a canary: `sbx policy check` answers
    "Allowed" for a host the manager just proved denied. `SbxDriver` turns
    that into a `DriverError` (driver.py `assert_egress`), so the fake
    raises the same error from the same call."""

    def assert_egress(self, name: str, allowed: tuple[str, ...], denied: tuple[str, ...]) -> None:
        super().assert_egress(name, allowed, denied)
        raise DriverError(f"{denied[0]} is REACHABLE from {name} — deny-by-default is broken.")


def apply_family(
    registry_root: Path, state_root: Path, *, driver: FakeDriver | None = None
) -> ApplyResult:
    return apply_once(
        registry_root,
        FAMILY,
        state_root=state_root,
        image="sha256:deadbeef",
        driver=driver if driver is not None else FakeDriver(),
        litellm=FakeLiteLLMKeys(),
    )


def registry_with_egress(tmp_path: Path, egress: list[str]) -> Path:
    root = tmp_path / "registry"
    write_registry(root, egress=egress)
    return root


def call_named(driver: FakeDriver, op: str) -> tuple[object, ...]:
    """The arguments of the one call with this name. Every test here
    drives a single fresh family, so there is exactly one of each."""
    matching = [call.args for call in driver.calls if call.op == op]
    assert len(matching) == 1, f"expected one {op} call, got {len(matching)}"
    return matching[0]


# --- the empty list, the normal attended case ----------------------------


def test_an_empty_egress_list_still_allows_both_planes(tmp_path: Path) -> None:
    registry_root = registry_with_egress(tmp_path, [])
    driver = FakeDriver()
    apply_family(registry_root, tmp_path / "state", driver=driver)
    allow = call_named(driver, "set_egress")[1]
    assert allow == (litellm_endpoint(), pep_endpoint())


def test_the_planes_are_also_asserted_reachable(tmp_path: Path) -> None:
    registry_root = registry_with_egress(tmp_path, [])
    driver = FakeDriver()
    apply_family(registry_root, tmp_path / "state", driver=driver)
    allowed = call_named(driver, "assert_egress")[1]
    assert litellm_endpoint() in allowed
    assert pep_endpoint() in allowed


def test_the_family_list_follows_the_two_planes(tmp_path: Path) -> None:
    registry_root = registry_with_egress(tmp_path, ["github.com", "api.github.com"])
    driver = FakeDriver()
    apply_family(registry_root, tmp_path / "state", driver=driver)
    allow = call_named(driver, "set_egress")[1]
    assert allow == (litellm_endpoint(), pep_endpoint(), "github.com", "api.github.com")


def test_the_planes_never_appear_in_the_family_file(tmp_path: Path) -> None:
    """The endpoints are the manager's config, not the family's file. A
    family file that listed them would fail validation anyway: contract 01
    §3.7 rule 1 refuses an IP literal."""
    registry_root = registry_with_egress(tmp_path, [])
    driver = FakeDriver()
    apply_family(registry_root, tmp_path / "state", driver=driver)
    family_yaml = (registry_root / "families" / FAMILY / "family.yaml").read_text(encoding="utf-8")
    assert litellm_endpoint() not in family_yaml
    assert pep_endpoint() not in family_yaml
    assert call_named(driver, "set_egress")[1] == (litellm_endpoint(), pep_endpoint())


def test_the_spec_carries_no_allow_list_of_its_own(tmp_path: Path) -> None:
    """Egress reaches sbx through `policy allow`, never through `create`.
    One path, so a reader has one place to look."""
    registry_root = registry_with_egress(tmp_path, ["github.com"])
    driver = FakeDriver()
    apply_family(registry_root, tmp_path / "state", driver=driver)
    spec = call_named(driver, "create")[0]
    assert isinstance(spec, SandboxSpec)
    network_fields = [one for one in spec.__dataclass_fields__ if one in ("allow", "egress")]
    assert network_fields == []


# --- the canary probe ----------------------------------------------------


def test_apply_probes_the_canaries_as_denied(tmp_path: Path) -> None:
    registry_root = registry_with_egress(tmp_path, [])
    driver = FakeDriver()
    apply_family(registry_root, tmp_path / "state", driver=driver)
    denied = call_named(driver, "assert_egress")[2]
    assert denied == default_canaries()


def test_a_canary_the_family_allows_is_not_probed_as_denied(tmp_path: Path) -> None:
    """A host the family was granted is not a canary for that family.
    Probing it both ways would fail every apply of such a family."""
    registry_root = registry_with_egress(tmp_path, ["example.com:443"])
    driver = FakeDriver()
    apply_family(registry_root, tmp_path / "state", driver=driver)
    denied = call_named(driver, "assert_egress")[2]
    assert "example.com:443" not in denied
    assert len(denied) == len(default_canaries()) - 1


def test_a_reachable_canary_fails_the_apply(tmp_path: Path) -> None:
    registry_root = registry_with_egress(tmp_path, [])
    result = apply_family(registry_root, tmp_path / "state", driver=ReachableCanaryDriver())
    assert result.ok is False
    assert [fault.code for fault in result.status.faults] == ["sandbox_start_failed"]
    assert result.status.sandboxes == ()


def test_a_reachable_canary_burns_the_sandbox_id(tmp_path: Path) -> None:
    """The counter moves BEFORE `sbx create`, so a failed apply spends its
    id and the retry takes the next one.

    The reconciler does this because retrying the same id would
    meet a VM `sbx create` already made, and what `sbx create` does with
    an existing name is unverified. Worse, an apply that found the id
    already written would take the early return and report a sandbox
    whose egress was never proved."""
    from caregiver import paths

    state_root = tmp_path / "state"
    registry_root = registry_with_egress(tmp_path, [])
    apply_family(registry_root, state_root, driver=ReachableCanaryDriver())
    assert paths.read_sandbox_sequence(state_root, FAMILY) == 1


def test_a_reachable_canary_destroys_the_sandbox(tmp_path: Path) -> None:
    """A VM whose egress could not be proved is a hole in deny-by-default
    (invariant 11). It does not get to stay."""
    driver = ReachableCanaryDriver()
    registry_root = registry_with_egress(tmp_path, [])
    apply_family(registry_root, tmp_path / "state", driver=driver)
    assert driver.ops()[-1] == "destroy"


# --- the config object ---------------------------------------------------


def test_the_endpoints_are_configurable(tmp_path: Path) -> None:
    """Defaults as the contract names them, overridable for a test host."""
    config = EgressConfig(litellm="litellm.test:4000", chaperone="chaperone.test:8300")
    assert config.allowed(()) == ("litellm.test:4000", "chaperone.test:8300")


def test_the_default_endpoints_match_the_contract() -> None:
    address = os.environ[LAN_ADDRESS_ENV]
    assert EgressConfig().allowed(()) == (f"{address}:4000", f"{address}:8300")


def test_the_planes_are_never_probed_as_denied() -> None:
    """A canary list that named a plane would contradict the allow list."""
    denied = EgressConfig().denied(())
    assert litellm_endpoint() not in denied
    assert pep_endpoint() not in denied
