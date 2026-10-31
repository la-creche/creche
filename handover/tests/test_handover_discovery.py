"""Finding `component.yaml`, and contract 06 §10's catalog rows."""

from __future__ import annotations

from pathlib import Path

import pytest
from handover.catalog import CATALOG, CATALOG_BY_NAME, RETIRING
from handover.discovery import (
    MAX_MANIFEST_FILES,
    discover,
    read_one,
    releasable,
    require_complete,
)
from handover.errors import Refusal, RefusalCode
from handover.manifest import MAX_MANIFEST_BYTES
from handover_fixtures import manifest_text, write_manifest

from handover import discovery

CONTROL_NAMES = (
    "attendance",
    "caregiver",
    "noticeboard",
    "playpen",
    "handover",
)


def _control_repo(root: Path) -> Path:
    """A fixture tree holding every creche component."""
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
    assert found.found["noticeboard"].subject == "noticeboard/component.yaml"


def test_missing_names_what_no_root_held(tmp_path: Path) -> None:
    found = discover([_control_repo(tmp_path)])

    assert found.missing == ("chaperone", "mcp-servers", "registry-data")


def test_require_complete_refuses_and_names_them(tmp_path: Path) -> None:
    found = discover([_control_repo(tmp_path)])

    with pytest.raises(Refusal) as caught:
        require_complete(found)

    assert caught.value.code is RefusalCode.CATALOG
    assert "chaperone" in caught.value.detail
    assert "mcp-servers" in caught.value.detail
    assert "registry-data" in caught.value.detail


def _all_four(tmp_path: Path, control_names: tuple[str, ...]) -> list[Path]:
    """Four roots, with the creche components `control_names` lists."""
    control = tmp_path / "creche"
    for name in control_names:
        write_manifest(control, name, manifest_text(name))

    return [control, *_other_roots(tmp_path)]


def _other_roots(tmp_path: Path) -> list[Path]:
    """The roots of the three other repositories, each with its manifest."""
    roots: list[Path] = []
    for repo, name in (("mcp", "mcp-servers"), ("registry", "registry-data")):
        roots.append(tmp_path / repo)
        write_manifest(roots[-1], name, manifest_text(name))

    roots.append(tmp_path / "chaperone")
    write_manifest(roots[-1], "chaperone", manifest_text("chaperone"))

    return roots


def _retire(monkeypatch: pytest.MonkeyPatch) -> str:
    """Put one creche component into `RETIRING` for one test. The set is
    empty today (`catalog.RETIRING`), and only a name with a row has a
    manifest to read."""
    assert frozenset() == RETIRING
    retiring = "noticeboard"
    assert retiring in CATALOG_BY_NAME
    monkeypatch.setattr(discovery, "RETIRING", frozenset({retiring}))

    return retiring


def test_a_retiring_component_may_be_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A component leaves in two commits: this code first, then the commit
    that deletes its directory. A requester already installed must plan
    over both trees, so the absent manifest is not `missing`."""
    retiring = _retire(monkeypatch)
    kept = tuple(name for name in CONTROL_NAMES if name != retiring)

    found = discover(_all_four(tmp_path, kept))

    assert retiring not in found.manifests()
    assert found.missing == ()
    require_complete(found)


def test_a_retiring_component_still_reads_when_it_is_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    retiring = _retire(monkeypatch)

    found = discover(_all_four(tmp_path, CONTROL_NAMES))

    assert retiring in found.manifests()
    require_complete(found)


def test_only_a_retiring_component_may_be_absent(tmp_path: Path) -> None:
    kept = tuple(name for name in CONTROL_NAMES if name != "noticeboard")

    found = discover(_all_four(tmp_path, kept))

    assert found.missing == ("noticeboard",)


def test_a_complete_pair_of_roots_is_complete(tmp_path: Path) -> None:
    control = _control_repo(tmp_path / "creche")

    require_complete(discover([control, *_other_roots(tmp_path)]))


def test_a_name_outside_the_catalog_is_refused(tmp_path: Path) -> None:
    text = manifest_text("noticeboard").replace("name: noticeboard", "name: smuggled")
    write_manifest(tmp_path, "noticeboard", text)

    refusal = _refusal([tmp_path])

    assert refusal.code is RefusalCode.CATALOG
    assert "contract 06 §1 does not list" in refusal.detail


def test_a_declared_path_must_match_the_directory(tmp_path: Path) -> None:
    write_manifest(tmp_path, "noticeboard", manifest_text("noticeboard", path="attendance"))

    refusal = _refusal([tmp_path])

    assert refusal.code is RefusalCode.CATALOG
    assert "but sits in noticeboard" in refusal.detail


def test_a_declared_repo_must_match_the_catalog(tmp_path: Path) -> None:
    text = manifest_text("noticeboard").replace("repo: creche", "repo: mcp")
    write_manifest(tmp_path, "noticeboard", text)

    refusal = _refusal([tmp_path])

    assert refusal.code is RefusalCode.CATALOG
    assert "lives in creche" in refusal.detail


def test_a_data_component_cannot_declare_itself_releasable(tmp_path: Path) -> None:
    write_manifest(tmp_path, "registry-data", manifest_text("registry-data", release="yes"))

    refusal = _refusal([tmp_path])

    assert refusal.code is RefusalCode.CATALOG
    assert "releases: no" in refusal.detail


def test_a_declared_kind_must_match_the_catalog(tmp_path: Path) -> None:
    """The allocator reads the kind from the catalog row, and the executor
    builds the kind that the manifest states. The two must not differ."""
    text = manifest_text("noticeboard").replace("kind: venv", "kind: binary")
    write_manifest(tmp_path, "noticeboard", text)

    refusal = _refusal([tmp_path])

    assert refusal.code is RefusalCode.CATALOG
    assert refusal.subject == "noticeboard/component.yaml"
    assert refusal.detail == "noticeboard is kind venv, not binary"


def test_the_executors_reader_refuses_another_kind_too(tmp_path: Path) -> None:
    """`read_one` reads the manifest of one fetched tree, and it applies
    the same catalog rules as the walk."""
    text = manifest_text("playpen").replace("kind: oci-image", "kind: venv")
    write_manifest(tmp_path, "playpen", text)

    with pytest.raises(Refusal) as caught:
        read_one(tmp_path, "playpen")

    assert caught.value.code is RefusalCode.CATALOG
    assert caught.value.detail == "playpen is kind oci-image, not venv"


@pytest.mark.parametrize("name", [row.name for row in CATALOG])
def test_the_fixture_manifest_states_the_kind_of_its_row(tmp_path: Path, name: str) -> None:
    write_manifest(tmp_path, name, manifest_text(name))

    assert read_one(tmp_path, name).manifest.kind is CATALOG_BY_NAME[name].kind


def test_two_roots_cannot_declare_one_component_twice(tmp_path: Path) -> None:
    first = tmp_path / "one"
    second = tmp_path / "two"
    write_manifest(first, "noticeboard", manifest_text("noticeboard"))
    write_manifest(second, "noticeboard", manifest_text("noticeboard"))

    refusal = _refusal([first, second])

    assert refusal.code is RefusalCode.CATALOG
    assert "declared twice" in refusal.detail


def test_a_symlinked_manifest_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "elsewhere.yaml"
    real.write_text(manifest_text("noticeboard"), encoding="utf-8")
    (tmp_path / "noticeboard").mkdir()
    (tmp_path / "noticeboard" / "component.yaml").symlink_to(real)

    refusal = _refusal([tmp_path])

    assert refusal.code is RefusalCode.MANIFEST
    assert "is a symlink" in refusal.detail


def test_an_oversized_manifest_is_refused_before_it_parses(tmp_path: Path) -> None:
    padding = "# " + ("x" * MAX_MANIFEST_BYTES) + "\n"
    write_manifest(tmp_path, "noticeboard", padding + manifest_text("noticeboard"))

    refusal = _refusal([tmp_path])

    assert refusal.code is RefusalCode.MANIFEST
    assert "larger than" in refusal.detail


def test_a_skipped_directory_holds_no_declaration(tmp_path: Path) -> None:
    hidden = tmp_path / ".venv" / "noticeboard"
    hidden.mkdir(parents=True)
    (hidden / "component.yaml").write_text(manifest_text("noticeboard"), encoding="utf-8")

    assert discover([tmp_path]).found == {}


def test_a_symlinked_directory_is_not_walked(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    write_manifest(outside, "noticeboard", manifest_text("noticeboard"))
    root = tmp_path / "root"
    root.mkdir()
    (root / "linked").symlink_to(outside)

    assert discover([root]).found == {}


def test_too_many_manifest_files_is_refused(tmp_path: Path) -> None:
    for index in range(MAX_MANIFEST_FILES + 2):
        directory = tmp_path / f"c{index:03d}"
        directory.mkdir()
        (directory / "component.yaml").write_text("name: noticeboard\n", encoding="utf-8")

    refusal = _refusal([tmp_path])

    assert refusal.code is RefusalCode.CATALOG
    assert "component.yaml files" in refusal.detail


def test_releasable_drops_the_data_component(tmp_path: Path) -> None:
    write_manifest(tmp_path, "registry-data", manifest_text("registry-data"))
    write_manifest(tmp_path, "noticeboard", manifest_text("noticeboard"))

    assert set(releasable(discover([tmp_path]))) == {"noticeboard"}
