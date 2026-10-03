"""Finding `component.yaml`, and contract 06 §10's catalog rows."""

from __future__ import annotations

from pathlib import Path

import pytest
from agent_release.catalog import RETIRING
from agent_release.discovery import (
    MAX_MANIFEST_FILES,
    discover,
    releasable,
    require_complete,
)
from agent_release.errors import Refusal, RefusalCode
from agent_release.manifest import MAX_MANIFEST_BYTES
from release_fixtures import manifest_text, write_manifest

CONTROL_NAMES = ("pep", "sessiond", "managerd", "ui", "playpen", "infra", "releasectl")


def _control_repo(root: Path) -> Path:
    """A fixture tree holding every agent-control component."""
    for name in CONTROL_NAMES:
        write_manifest(root, name, manifest_text(name))

    return root


def _refusal(roots: list[Path]) -> Refusal:
    with pytest.raises(Refusal) as caught:
        discover(roots)

    return caught.value


def test_discover_finds_every_manifest(tmp_path: Path) -> None:
    found = discover([_control_repo(tmp_path)])

    assert set(found.manifests()) == set(CONTROL_NAMES)
    assert found.found["pep"].subject == "pep/component.yaml"


def test_missing_names_what_no_root_held(tmp_path: Path) -> None:
    found = discover([_control_repo(tmp_path)])

    assert found.missing == ("mcp-servers", "registry-data")


def test_require_complete_refuses_and_names_them(tmp_path: Path) -> None:
    found = discover([_control_repo(tmp_path)])

    with pytest.raises(Refusal) as caught:
        require_complete(found)

    assert caught.value.code is RefusalCode.CATALOG
    assert "mcp-servers" in caught.value.detail
    assert "registry-data" in caught.value.detail


def _all_three(tmp_path: Path, control_names: tuple[str, ...]) -> list[Path]:
    """Three roots, with the agent-control components `control_names` lists."""
    control = tmp_path / "agent-control"
    mcp = tmp_path / "agent-mcp"
    registry = tmp_path / "agent-registry"
    for name in control_names:
        write_manifest(control, name, manifest_text(name))

    write_manifest(mcp, "mcp-servers", manifest_text("mcp-servers"))
    write_manifest(registry, "registry-data", manifest_text("registry-data"))

    return [control, mcp, registry]


def test_a_retiring_component_may_be_absent(tmp_path: Path) -> None:
    """A component leaves in two commits: this code first, then the commit
    that deletes its directory. A requester already installed must plan
    over both trees, so the absent manifest is not `missing`."""
    (retiring,) = RETIRING
    kept = tuple(name for name in CONTROL_NAMES if name != retiring)

    found = discover(_all_three(tmp_path, kept))

    assert retiring not in found.manifests()
    assert found.missing == ()
    require_complete(found)


def test_a_retiring_component_still_reads_when_it_is_present(tmp_path: Path) -> None:
    (retiring,) = RETIRING

    found = discover(_all_three(tmp_path, CONTROL_NAMES))

    assert retiring in found.manifests()
    require_complete(found)


def test_only_a_retiring_component_may_be_absent(tmp_path: Path) -> None:
    kept = tuple(name for name in CONTROL_NAMES if name != "pep")

    found = discover(_all_three(tmp_path, kept))

    assert found.missing == ("pep",)


def test_a_complete_pair_of_roots_is_complete(tmp_path: Path) -> None:
    control = _control_repo(tmp_path / "agent-control")
    mcp = tmp_path / "agent-mcp"
    registry = tmp_path / "agent-registry"
    write_manifest(mcp, "mcp-servers", manifest_text("mcp-servers"))
    write_manifest(registry, "registry-data", manifest_text("registry-data"))

    require_complete(discover([control, mcp, registry]))


def test_a_name_outside_the_catalog_is_refused(tmp_path: Path) -> None:
    text = manifest_text("pep").replace("name: pep", "name: smuggled")
    write_manifest(tmp_path, "pep", text)

    refusal = _refusal([tmp_path])

    assert refusal.code is RefusalCode.CATALOG
    assert "contract 06 §1 does not list" in refusal.detail


def test_a_declared_path_must_match_the_directory(tmp_path: Path) -> None:
    write_manifest(tmp_path, "pep", manifest_text("pep", path="sessiond"))

    refusal = _refusal([tmp_path])

    assert refusal.code is RefusalCode.CATALOG
    assert "but sits in pep" in refusal.detail


def test_a_declared_repo_must_match_the_catalog(tmp_path: Path) -> None:
    text = manifest_text("pep").replace("repo: agent-control", "repo: agent-mcp")
    write_manifest(tmp_path, "pep", text)

    refusal = _refusal([tmp_path])

    assert refusal.code is RefusalCode.CATALOG
    assert "lives in agent-control" in refusal.detail


def test_a_data_component_cannot_declare_itself_releasable(tmp_path: Path) -> None:
    write_manifest(tmp_path, "registry-data", manifest_text("registry-data", release="yes"))

    refusal = _refusal([tmp_path])

    assert refusal.code is RefusalCode.CATALOG
    assert "releases: no" in refusal.detail


def test_two_roots_cannot_declare_one_component_twice(tmp_path: Path) -> None:
    first = tmp_path / "one"
    second = tmp_path / "two"
    write_manifest(first, "pep", manifest_text("pep"))
    write_manifest(second, "pep", manifest_text("pep"))

    refusal = _refusal([first, second])

    assert refusal.code is RefusalCode.CATALOG
    assert "declared twice" in refusal.detail


def test_a_symlinked_manifest_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "elsewhere.yaml"
    real.write_text(manifest_text("pep"), encoding="utf-8")
    (tmp_path / "pep").mkdir()
    (tmp_path / "pep" / "component.yaml").symlink_to(real)

    refusal = _refusal([tmp_path])

    assert refusal.code is RefusalCode.MANIFEST
    assert "is a symlink" in refusal.detail


def test_an_oversized_manifest_is_refused_before_it_parses(tmp_path: Path) -> None:
    padding = "# " + ("x" * MAX_MANIFEST_BYTES) + "\n"
    write_manifest(tmp_path, "pep", padding + manifest_text("pep"))

    refusal = _refusal([tmp_path])

    assert refusal.code is RefusalCode.MANIFEST
    assert "larger than" in refusal.detail


def test_a_skipped_directory_holds_no_declaration(tmp_path: Path) -> None:
    hidden = tmp_path / ".venv" / "pep"
    hidden.mkdir(parents=True)
    (hidden / "component.yaml").write_text(manifest_text("pep"), encoding="utf-8")

    assert discover([tmp_path]).found == {}


def test_a_symlinked_directory_is_not_walked(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    write_manifest(outside, "pep", manifest_text("pep"))
    root = tmp_path / "root"
    root.mkdir()
    (root / "linked").symlink_to(outside)

    assert discover([root]).found == {}


def test_too_many_manifest_files_is_refused(tmp_path: Path) -> None:
    for index in range(MAX_MANIFEST_FILES + 2):
        directory = tmp_path / f"c{index:03d}"
        directory.mkdir()
        (directory / "component.yaml").write_text("name: pep\n", encoding="utf-8")

    refusal = _refusal([tmp_path])

    assert refusal.code is RefusalCode.CATALOG
    assert "component.yaml files" in refusal.detail


def test_releasable_drops_the_data_component(tmp_path: Path) -> None:
    write_manifest(tmp_path, "registry-data", manifest_text("registry-data"))
    write_manifest(tmp_path, "pep", manifest_text("pep"))

    assert set(releasable(discover([tmp_path]))) == {"pep"}
