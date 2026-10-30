"""The fleet `policy.toml` in each verb that moves a grant or a token.

A pass writes the fragment of its family and assembles the one file of the
fleet. A rotation moves the digests of the fragment. A delete removes the
fragment, then assembles the file again."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from agent_family import FamilyState, Index, load_registry
from caregiver.cli import main
from caregiver.credentials import read_creds, token_sha256
from caregiver.delete import delete_family
from caregiver.driver import FakeDriver
from caregiver.egress import EgressConfig
from caregiver.litellm_keys import FakeLiteLLMKeys
from caregiver.policy_fleet import FakeChecker, FleetPolicy, PolicyTarget, TargetFile
from caregiver.reconcile import Actors, ReconcileResult, reconcile_family
from caregiver.rotate import (
    Mode,
    RotateRequest,
    Scope,
    rotate,
    rotate_serving,
    serving_family,
    settle,
    valid_family,
)
from caregiver.steps import WHOLE_FILE
from caregiver.switch import FakeSwitchClient
from caregiver.timers import FakeUnits
from caregiver_helpers import expire_overlap, toml_text, write_registry

from caregiver import paths

FAMILY = "chat"
IMAGE = "sha256:deadbeef"
SERVER = "kagi"
REVISION = "reg-policy-wire"


@dataclass
class Fleet:
    """One family, its state root, the configuration directory of chaperone
    and the fakes."""

    registry_root: Path
    state_root: Path
    config_dir: Path
    checker: FakeChecker = field(default_factory=FakeChecker)
    driver: FakeDriver = field(default_factory=FakeDriver)
    litellm: FakeLiteLLMKeys = field(default_factory=FakeLiteLLMKeys)

    @property
    def policy(self) -> FleetPolicy:
        return FleetPolicy(PolicyTarget(self.config_dir), self.checker)

    def run(self) -> ReconcileResult:
        actors = Actors(
            self.driver,
            self.litellm,
            FakeSwitchClient(),
            EgressConfig(),
            FakeUnits(),
            self.policy,
        )
        return reconcile_family(
            load_registry(self.registry_root),
            FAMILY,
            state_root=self.state_root,
            image=IMAGE,
            actors=actors,
        )

    def fragment(self) -> Path:
        return paths.policy_fragment_path(self.state_root, FAMILY)

    def apps(self) -> dict[str, Any]:
        text = (self.config_dir / TargetFile.POLICY).read_text(encoding="utf-8")
        return tomllib.loads(text).get("apps", {})

    def digests(self) -> list[str]:
        return self.apps()[FAMILY]["token_sha256"]

    def token(self) -> str:
        creds = read_creds(paths.creds_path(self.state_root, FAMILY))
        assert creds is not None
        return creds.pep_token


@pytest.fixture
def fleet(tmp_path: Path) -> Fleet:
    ready = Fleet(tmp_path / "registry", tmp_path / "state", tmp_path / "etc-chaperone")
    write_registry(ready.registry_root)
    return ready


def write_server(registry_root: Path, tools: tuple[str, ...]) -> None:
    body = {
        "name": SERVER,
        "identity": "Search.",
        "install": {"source": "agent-mcp"},
        "run": {"entrypoint": "kagi-mcp", "env": {"KAGI_API_KEY": "secret:kagi_api_key"}},
        "tools": [{"name": tool, "description": f"{tool}."} for tool in tools],
    }
    directory = registry_root / "mcp" / SERVER
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "server.toml").write_text(toml_text(body), encoding="utf-8")


def test_reconcile_writes_the_fragment_and_assembles(fleet: Fleet) -> None:
    result = fleet.run()
    assert result.status.state is FamilyState.IN_SYNC
    assert fleet.fragment().is_file()
    assert fleet.digests() == [token_sha256(fleet.token())]
    # The new table alone, then the joined file.
    assert len(fleet.checker.checked) == 2


def test_a_pass_writes_no_json_grant_file(fleet: Fleet) -> None:
    """`policy.toml` is the one policy output. The grant file of the
    Python PEP left with that PEP."""
    fleet.run()
    assert paths.policy_fragment_path(fleet.state_root, FAMILY).is_file()
    assert not (paths.policy_fragments_dir(fleet.state_root) / f"{FAMILY}.json").exists()


def test_a_pass_that_moves_nothing_checks_nothing(fleet: Fleet) -> None:
    fleet.run()
    checked = len(fleet.checker.checked)
    fleet.run()
    assert len(fleet.checker.checked) == checked


def test_a_server_edit_moves_the_fragment(fleet: Fleet) -> None:
    write_server(fleet.registry_root, ("search",))
    write_registry(fleet.registry_root, tools={SERVER: "all"})
    fleet.run()
    write_server(fleet.registry_root, ("search", "extract"))
    fleet.run()
    assert sorted(fleet.apps()[FAMILY]["grants"][SERVER]["tools"]) == ["extract", "search"]


def test_a_family_the_assembler_leaves_out_reads_invalid(fleet: Fleet) -> None:
    fleet.checker.refused_apps = frozenset({FAMILY})
    result = fleet.run()
    assert result.status.state is FamilyState.INVALID
    assert result.status.validation is not None
    assert result.status.validation.first_error == f"{WHOLE_FILE}: the app {FAMILY} is refused"
    assert fleet.driver.ops() == ()


def test_status_prints_the_line_of_the_assembly(
    fleet: Fleet, capsys: pytest.CaptureFixture[str]
) -> None:
    fleet.checker.refused_apps = frozenset({FAMILY})
    fleet.run()
    assert main(["status", FAMILY, "--state-root", str(fleet.state_root)]) == 0
    printed = capsys.readouterr().out
    assert f"{FAMILY}: invalid" in printed
    assert f"error: {WHOLE_FILE}: the app {FAMILY} is refused" in printed


def test_a_left_out_family_comes_back_once_the_check_passes(fleet: Fleet) -> None:
    fleet.checker.refused_apps = frozenset({FAMILY})
    fleet.run()
    fleet.checker.refused_apps = frozenset()
    result = fleet.run()
    assert result.status.state is FamilyState.IN_SYNC
    assert FAMILY in fleet.apps()


def test_a_refused_edit_keeps_the_last_good_table(fleet: Fleet) -> None:
    write_server(fleet.registry_root, ("search", "extract"))
    write_registry(fleet.registry_root, tools={SERVER: ["search"]})
    fleet.run()
    last = fleet.fragment().read_text(encoding="utf-8")

    write_registry(fleet.registry_root, tools={SERVER: ["search", "extract"]})
    fleet.checker.refused_apps = frozenset({FAMILY})
    result = fleet.run()

    assert result.status.state is FamilyState.INVALID
    assert fleet.fragment().read_text(encoding="utf-8") == last
    assert sorted(fleet.apps()[FAMILY]["grants"][SERVER]["tools"]) == ["search"]


def test_a_check_that_does_not_run_moves_no_fragment(fleet: Fleet) -> None:
    write_server(fleet.registry_root, ("search", "extract"))
    write_registry(fleet.registry_root, tools={SERVER: ["search"]})
    fleet.run()
    last = fleet.fragment().read_text(encoding="utf-8")

    write_registry(fleet.registry_root, tools={SERVER: ["search", "extract"]})
    fleet.checker.unavailable = True
    result = fleet.run()

    assert result.status.state is FamilyState.INVALID
    assert fleet.fragment().read_text(encoding="utf-8") == last
    assert sorted(fleet.apps()[FAMILY]["grants"][SERVER]["tools"]) == ["search"]


def test_a_check_that_does_not_run_leaves_the_family_invalid(fleet: Fleet) -> None:
    fleet.checker.unavailable = True
    result = fleet.run()
    assert result.status.state is FamilyState.INVALID
    assert not (fleet.config_dir / TargetFile.POLICY).exists()


def test_an_exposed_name_over_the_cap_reads_invalid_and_mints_nothing(fleet: Fleet) -> None:
    write_server(fleet.registry_root, ("t" * 60,))
    write_registry(fleet.registry_root, tools={SERVER: "all"})
    result = fleet.run()
    assert result.status.state is FamilyState.INVALID
    assert read_creds(paths.creds_path(fleet.state_root, FAMILY)) is None
    assert not fleet.fragment().exists()


def test_a_graceful_rotation_puts_two_digests_in_the_fragment(fleet: Fleet) -> None:
    fleet.run()
    old = fleet.token()
    family = valid_family(load_registry(fleet.registry_root), FAMILY, state_root=fleet.state_root)
    assert family is not None
    rotate(
        RotateRequest(scope=Scope.TOKEN, mode=Mode.GRACEFUL),
        family,
        Index(),
        state_root=fleet.state_root,
        litellm=fleet.litellm,
        policy=fleet.policy,
        revision=REVISION,
    )
    assert sorted(fleet.digests()) == sorted([token_sha256(fleet.token()), token_sha256(old)])


def test_a_settled_rotation_leaves_one_digest(fleet: Fleet) -> None:
    fleet.run()
    family = valid_family(load_registry(fleet.registry_root), FAMILY, state_root=fleet.state_root)
    assert family is not None
    rotate(
        RotateRequest(scope=Scope.TOKEN, mode=Mode.GRACEFUL),
        family,
        Index(),
        state_root=fleet.state_root,
        litellm=fleet.litellm,
        policy=fleet.policy,
        revision=REVISION,
    )
    expire_overlap(fleet.state_root)
    assert settle(FAMILY, state_root=fleet.state_root, policy=fleet.policy) is True
    assert fleet.digests() == [token_sha256(fleet.token())]


def test_a_serving_rotation_moves_the_digests_of_the_fragment(fleet: Fleet) -> None:
    fleet.run()
    old = fleet.token()
    family = serving_family(load_registry(fleet.registry_root), FAMILY, state_root=fleet.state_root)
    assert family is not None
    rotate_serving(
        RotateRequest(scope=Scope.TOKEN, mode=Mode.GRACEFUL),
        family,
        state_root=fleet.state_root,
        litellm=fleet.litellm,
        policy=fleet.policy,
    )
    assert sorted(fleet.digests()) == sorted([token_sha256(fleet.token()), token_sha256(old)])


def test_a_delete_removes_the_fragment_and_the_app_table(fleet: Fleet) -> None:
    fleet.run()
    left = delete_family(
        FAMILY,
        state_root=fleet.state_root,
        driver=fleet.driver,
        litellm=fleet.litellm,
        policy=fleet.policy,
    )
    assert left is None
    assert not fleet.fragment().exists()
    assert FAMILY not in fleet.apps()


def test_a_delete_whose_assembly_fails_says_why(fleet: Fleet) -> None:
    fleet.run()
    fleet.checker.unavailable = True
    left = delete_family(
        FAMILY,
        state_root=fleet.state_root,
        driver=fleet.driver,
        litellm=fleet.litellm,
        policy=fleet.policy,
    )
    assert left == "the fake checker does not run"
    assert not paths.creds_path(fleet.state_root, FAMILY).exists()


def test_reconcile_once_writes_policy_toml_into_the_named_directory(
    fleet: Fleet, tmp_path: Path
) -> None:
    named = tmp_path / "named-chaperone"
    argv = [
        "reconcile-once",
        str(fleet.registry_root),
        FAMILY,
        "--image",
        IMAGE,
        "--state-root",
        str(fleet.state_root),
        "--chaperone-config-dir",
        str(named),
        "--write",
    ]
    code = main(
        argv,
        driver=fleet.driver,
        litellm=fleet.litellm,
        switch=FakeSwitchClient(),
        units=FakeUnits(),
        checker=fleet.checker,
    )
    assert code == 0
    assert FAMILY in tomllib.loads((named / TargetFile.POLICY).read_text(encoding="utf-8"))["apps"]


def test_a_run_with_a_state_root_of_its_own_writes_inside_that_root(fleet: Fleet) -> None:
    argv = [
        "reconcile-once",
        str(fleet.registry_root),
        FAMILY,
        "--image",
        IMAGE,
        "--state-root",
        str(fleet.state_root),
        "--write",
    ]
    main(
        argv,
        driver=fleet.driver,
        litellm=fleet.litellm,
        switch=FakeSwitchClient(),
        units=FakeUnits(),
        checker=fleet.checker,
    )
    assert (fleet.state_root / paths.CHAPERONE_SCRATCH_DIR / TargetFile.POLICY).is_file()
