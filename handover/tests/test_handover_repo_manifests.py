"""This repository's own `component.yaml` files, against contract 06.

A fixture proves the resolver. This module proves the repository: the
manifests it holds parse, match the catalog, form an acyclic graph, and
require each contract at the version its provider declares. A provider that
moves without its consumers fails here.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from handover.catalog import (
    ARRIVING,
    CATALOG_BY_NAME,
    CONTRACT_OWNER,
    RETIRING,
    ContractId,
    Kind,
    Repo,
)
from handover.cli import EXIT_OK
from handover.cli import main as cli_main
from handover.discovery import ManifestSet, discover
from handover.executor.install import RESTART_TIMEOUT_S
from handover.resolve import resolve
from handover.state import ReleaseState

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The two components that live in repositories agent-control cannot write
#: (contract 06 §10.1).
ELSEWHERE = ("mcp-servers", "registry-data")


@pytest.fixture(scope="module")
def found() -> ManifestSet:
    return discover([REPO_ROOT])


def _provided(found: ManifestSet) -> dict[ContractId, tuple[int, int]]:
    """Each contract's version, as the one component that provides it says."""
    return {
        one.contract: (one.major, one.minor)
        for manifest in found.manifests().values()
        for one in manifest.provides
    }


def test_every_agent_control_component_declares_itself(found: ManifestSet) -> None:
    """All but a retiring one, whose directory has left this tree
    (`catalog.RETIRING`), and an arriving one, whose directory has not
    reached it yet (`catalog.ARRIVING`)."""
    here = {row.name for row in CATALOG_BY_NAME.values() if row.repo is Repo.AGENT_CONTROL}

    assert set(found.manifests()) == here - RETIRING - ARRIVING


def test_only_the_other_repos_components_are_missing(found: ManifestSet) -> None:
    assert found.missing == ELSEWHERE


def test_a_manifest_sits_where_the_catalog_says(found: ManifestSet) -> None:
    for name, item in found.found.items():
        assert item.directory == CATALOG_BY_NAME[name].path


#: Contracts no manifest provides today. `component-manifest` is the
#: temporary exception: the first `handover` release is installed by the old
#: `releasectl` executor, whose contract table would hold two providers of it
#: (the live `releasectl` stamp and the deploying `handover`) and refuse with
#: C2 or C3. A follow-up restores `provides` once `handover` is live and
#: `releasectl` is retired, and empties this set.
UNPROVIDED_FOR_NOW: frozenset[ContractId] = frozenset({ContractId.COMPONENT_MANIFEST})


def test_the_repo_set_resolves_with_a_green_table(found: ManifestSet) -> None:
    resolution = resolve(found.manifests(), ReleaseState(), {})

    assert resolution.order == ()
    assert len(resolution.contracts) == len(ContractId) - len(UNPROVIDED_FOR_NOW)


def test_handover_provides_nothing_until_releasectl_retires(found: ManifestSet) -> None:
    """Pinned, so the follow-up that restores `component-manifest` is a
    deliberate change (`UNPROVIDED_FOR_NOW`, `handover/component.yaml`).
    The owner stays `handover`: only the claim waits."""
    assert found.manifests()["handover"].provides == ()
    assert CONTRACT_OWNER[ContractId.COMPONENT_MANIFEST] == "handover"


def test_every_floor_is_the_providers_current_minor(found: ManifestSet) -> None:
    """Nothing is built against an older draft, so every floor is the
    version the contract's provider declares today."""
    provided = _provided(found)
    for name, manifest in found.manifests().items():
        for required in manifest.requires:
            expected = provided[required.contract]
            assert (required.major, required.min_minor) == expected, f"{name} {required.contract}"


def test_handover_is_not_a_dependency_of_anything(found: ManifestSet) -> None:
    """Contract 06 §1.1 rule 1: it deploys last, so nothing may wait on it."""
    for manifest in found.manifests().values():
        assert "handover" not in manifest.depends_on


@pytest.mark.usefixtures("operator_is_an_account")
def test_the_handover_hook_passes_from_the_executors_directory(
    found: ManifestSet, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hook inherits the executor's working directory, and that was
    once a component's own directory. With no `--root`, `check` read the
    manifest there as `./component.yaml` and refused it, so the first
    self-release on the host failed its hook in BOTH trees (2026-09-24). Any
    component's directory stands in for it here. The install path stands in
    for `tmp_path`, an empty tree like the one it names."""
    manifest = found.manifests()["handover"]
    args = [str(tmp_path) if one == manifest.install.to else one for one in manifest.verify.command]
    monkeypatch.chdir(REPO_ROOT / "chaperone")

    assert cli_main(args[1:]) == EXIT_OK


def test_nothing_requires_a_contract_handover_provides(found: ManifestSet) -> None:
    """Contract 06 §1.1 rule 3: a failed hook puts `handover` back ALONE
    and leaves the rest of its set. That is a combination nobody resolved,
    and it is safe only while no consumer reads what `handover` provides."""
    provided = {one.contract for one in found.manifests()["handover"].provides}
    for name, manifest in found.manifests().items():
        wanted = {one.contract for one in manifest.requires}
        assert not wanted & provided, name


def test_the_install_targets_are_all_different(found: ManifestSet) -> None:
    targets = [manifest.install.to for manifest in found.manifests().values()]

    assert len(set(targets)) == len(targets)


def test_no_manifest_names_a_secret(found: ManifestSet) -> None:
    """Contract 06 §8: names only, and today no component declares one."""
    for manifest in found.manifests().values():
        assert manifest.secrets == ()


def test_every_declared_unit_is_a_unit_this_repo_holds(found: ManifestSet) -> None:
    """Contract 06 §4: step 9 refreshes the unit file IN PLACE and restarts
    it. A name `systemd/` does not hold is a release that refreshes nothing
    and then polls `is-active` on a unit that does not exist.

    """
    units = {path.name for path in REPO_ROOT.glob("systemd/*.service")}
    units |= {path.name for path in REPO_ROOT.glob("*/systemd/*.service")}
    for name, manifest in found.manifests().items():
        if manifest.unit is None:
            continue

        assert manifest.unit in units, f"{name}: systemd/ holds no {manifest.unit}"


def test_no_manifest_names_the_executors_own_unit(found: ManifestSet) -> None:
    """No manifest names `creche-handover.service`. It is the path-activated
    oneshot that runs the executor, and a release that restarted it would
    re-enter the drain that is running that release
    (`handover/component.yaml`)."""
    for name, manifest in found.manifests().items():
        assert manifest.unit != "creche-handover.service", name


def test_every_venv_component_builds_a_self_contained_tree(found: ManifestSet) -> None:
    """Contract 06 §8.2. `uv sync` installs a workspace member EDITABLE, so
    a tree built without this flag holds the third-party packages and reads
    every first-party module out of the repository it was built from.

    An editable tree names `/opt/creche/caregiver/src` in its `.pth`
    lines, so `creche-deploy` changes the code every rework service
    loads at its next restart, and `.prev` holds the same lines, so a
    rollback puts the old third-party packages back and keeps the NEW
    first-party code.
    """
    for name, manifest in found.manifests().items():
        if manifest.kind is not Kind.VENV:
            continue

        assert manifest.build, f"{name}: a venv component with no build"
        for argv in manifest.build:
            assert "--no-editable" in argv, f"{name}: {' '.join(argv)}"


#: `uv sync --frozen` installs from this file, so it says what a build holds.
LOCK = REPO_ROOT / "uv.lock"

#: The `build` flag that names one workspace package to install.
PACKAGE_FLAG = "--package"

#: A workspace package: its directory, and every package it needs.
Package = tuple[str, tuple[str, ...]]


def _workspace() -> dict[str, Package]:
    """Each workspace package the lock holds. The root is `virtual`, not one."""
    lock = tomllib.loads(LOCK.read_text(encoding="utf-8"))
    packages: dict[str, Package] = {}
    for entry in lock["package"]:
        directory = entry.get("source", {}).get("editable")
        if directory is None:
            continue

        needs = tuple(one["name"] for one in entry.get("dependencies", ()))
        packages[entry["name"]] = (directory, needs)

    return packages


def _installed(build: tuple[tuple[str, ...], ...], packages: dict[str, Package]) -> set[str]:
    """Every workspace directory one `build` puts in its tree.

    The packages it names, and every workspace package they need, followed
    through the lock: `agent-door-trigger` brings `agent-family` with it.
    """
    todo = [
        argv[at + 1] for argv in build for at, word in enumerate(argv[:-1]) if word == PACKAGE_FLAG
    ]
    seen: set[str] = set()
    while todo:
        name = todo.pop()
        if name in seen or name not in packages:
            continue

        seen.add(name)
        todo.extend(packages[name][1])

    return {packages[name][0] for name in seen}


def test_each_component_bundles_what_its_build_installs(found: ManifestSet) -> None:
    """Contract 06 §1 rule 9. A change under a directory a build installs
    changes the artifact, so it must move the tag, and `CatalogRow.bundles`
    is that list written down. A package added to a build and not there
    ships with no new tag: a merge that changed only `door-trigger/` and
    `family/` once left `attendance` and `noticeboard` on tags older than their code."""
    packages = _workspace()
    for name, item in found.found.items():
        row = CATALOG_BY_NAME[name]
        installed = _installed(item.manifest.build, packages) - {row.path}

        assert set(row.bundles) == installed, name


#: systemd's own `TimeoutStopSec` and `TimeoutStartSec` when a unit sets
#: neither.
SYSTEMD_DEFAULT_TIMEOUT_S = 90

#: Sets both of the above at once.
BOTH_TIMEOUTS = "TimeoutSec"

PART_OF = "PartOf"


def _unit_texts() -> dict[str, str]:
    """Every unit file this repo holds, by name: the two directories
    `test_every_declared_unit_is_a_unit_this_repo_holds` reads."""
    paths = [*REPO_ROOT.glob("systemd/*.service"), *REPO_ROOT.glob("*/systemd/*.service")]

    return {path.name: path.read_text(encoding="utf-8") for path in paths}


def _settings(text: str, key: str) -> list[str]:
    """Every value one `[Service]` or `[Unit]` key takes, comments skipped."""
    found: list[str] = []
    for line in text.splitlines():
        name, sign, value = line.partition("=")
        if sign and name.strip() == key:
            found.append(value.strip())

    return found


def _timeout_s(text: str, key: str) -> int:
    """One timeout in whole seconds, or systemd's default when unset. A
    value such as `1min` fails `int`, loudly: write seconds."""
    values = [*_settings(text, BOTH_TIMEOUTS), *_settings(text, key)]

    return int(values[-1]) if values else SYSTEMD_DEFAULT_TIMEOUT_S


def test_a_restart_outlasts_every_units_own_stop_and_start(found: ManifestSet) -> None:
    """Step 9's `systemctl restart` answers only once its unit has stopped
    and started again, and systemd bounds both. A shorter wait fails a
    healthy release: attendance's 60 s stop met a 30 s wait on 2026-10-01
    and the release restored. A unit `PartOf=` this one stops before it, so
    its stop counts too."""
    units = _unit_texts()
    for name, manifest in found.manifests().items():
        if manifest.unit is None:
            continue

        own = units[manifest.unit]
        parts = [text for text in units.values() if manifest.unit in _settings(text, PART_OF)]
        stop = sum(_timeout_s(text, "TimeoutStopSec") for text in (own, *parts))

        assert stop + _timeout_s(own, "TimeoutStartSec") < RESTART_TIMEOUT_S, name
