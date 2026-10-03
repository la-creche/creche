"""An installed tree holds its own code (contract 06 §8.2).

`uv sync` installs a workspace member EDITABLE. A tree built with
`uv sync --frozen --package caregiver` holds the third-party packages,
and `lib/python3.12/site-packages/_editable_impl_caregiver.pth` names
`/opt/creche/caregiver/src`, so the running reconciler reads its code
from `/opt/creche`.

`--no-editable` fixes the build. This module proves the FENCE: the executor
refuses a staged tree that reads code from outside itself, whatever wrote it,
and it refuses before anything is swapped.

A fake `uv` cannot show this fault, so these tests write the tree by hand
and the guard in `test_handover_ced_uv_guard.py` runs the real one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from handover.catalog import Kind
from handover.errors import Refusal, RefusalCode
from handover.executor.install import Installer, paths_of
from handover.executor.selfcontained import check_tree, escapes
from handover.manifest import ComponentManifest, parse_manifest
from handover_executor_fixtures import FakeRun, fake_host, git_host_answers
from handover_fixtures import manifest_text

PYTHON_DIR = "python3.12"


def _tree(root: Path, name: str = "chaperone.new") -> Path:
    """A staged venv tree: a site-packages, a bin and a verify hook."""
    tree = root / name
    site = tree / "lib" / PYTHON_DIR / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    component = name.removesuffix(".new").removesuffix(".prev")
    (tree / "bin").mkdir(parents=True, exist_ok=True)
    hook = tree / "bin" / f"{component}-verify"
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    hook.chmod(0o755)

    return tree


def _site(tree: Path) -> Path:
    return tree / "lib" / PYTHON_DIR / "site-packages"


def _dist_info(tree: Path, name: str, body: object) -> Path:
    """One `direct_url.json`, PEP 610's file, beside a package."""
    directory = _site(tree) / f"{name}-0.1.0.dist-info"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "direct_url.json"
    path.write_text(json.dumps(body), encoding="utf-8")

    return path


# ---- the walk ---------------------------------------------------------------


def test_a_tree_that_holds_its_own_code_passes(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    (_site(tree) / "chaperone").mkdir()
    _dist_info(tree, "chaperone", {"url": "file:///src/chaperone", "dir_info": {}})

    assert escapes(tree) == ()


def test_an_editable_pth_naming_the_repository_is_found(tmp_path: Path) -> None:
    """The exact file the host carried."""
    tree = _tree(tmp_path)
    pth = _site(tree) / "_editable_impl_caregiver.pth"
    pth.write_text("/opt/creche/caregiver/src\n", encoding="utf-8")

    found = escapes(tree)

    assert len(found) == 1
    assert "_editable_impl_caregiver.pth" in found[0]


def test_a_relative_pth_line_that_climbs_out_is_found(tmp_path: Path) -> None:
    """A `.pth` line is resolved against site-packages, so `../../..` escapes
    a tree just as an absolute path does."""
    tree = _tree(tmp_path)
    (_site(tree) / "climb.pth").write_text("../../../../elsewhere\n", encoding="utf-8")

    assert len(escapes(tree)) == 1


def test_a_pth_line_inside_the_tree_is_allowed(tmp_path: Path) -> None:
    """A relative line that stays inside is what a plain wheel may write."""
    tree = _tree(tmp_path)
    (_site(tree) / "inside.pth").write_text("./extra\n", encoding="utf-8")

    assert escapes(tree) == ()


def test_the_import_line_uv_writes_is_allowed(tmp_path: Path) -> None:
    """`uv venv --relocatable` writes `_virtualenv.pth`, whose one line is
    `import _virtualenv`. `site` executes such a line instead of adding a
    path, and refusing it would refuse every tree the executor builds."""
    tree = _tree(tmp_path)
    (_site(tree) / "_virtualenv.pth").write_text("import _virtualenv\n", encoding="utf-8")

    assert escapes(tree) == ()


def test_blank_lines_and_comments_are_not_paths(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    (_site(tree) / "quiet.pth").write_text("\n# a comment\n\n", encoding="utf-8")

    assert escapes(tree) == ()


def test_an_editable_direct_url_is_found(tmp_path: Path) -> None:
    """PEP 610's own marker. A wheel built from a directory carries the same
    file with no `editable` key, so the flag and not the file is the fault."""
    tree = _tree(tmp_path)
    _dist_info(tree, "agent_family", {"url": "file:///src/family", "dir_info": {"editable": True}})

    found = escapes(tree)

    assert len(found) == 1
    assert "agent_family" in found[0]


def test_a_direct_url_root_cannot_parse_is_found(tmp_path: Path) -> None:
    """Fail closed. A file root cannot read is a package root cannot say is
    non-editable, and invariant 19 answers that with a report."""
    tree = _tree(tmp_path)
    path = _dist_info(tree, "agent_family", {})
    path.write_text("{not json", encoding="utf-8")

    assert len(escapes(tree)) == 1


def test_a_tree_with_no_site_packages_is_found(tmp_path: Path) -> None:
    """A `kind: venv` build that wrote no site-packages wrote no venv, and a
    walk that found nothing must not read as a pass."""
    tree = tmp_path / "empty.new"
    (tree / "bin").mkdir(parents=True)

    assert len(escapes(tree)) == 1


def test_every_escape_is_reported_at_once(tmp_path: Path) -> None:
    """One release names every fault, so a second build is not a second
    discovery."""
    tree = _tree(tmp_path)
    (_site(tree) / "one.pth").write_text("/opt/creche/a/src\n", encoding="utf-8")
    (_site(tree) / "two.pth").write_text("/opt/creche/b/src\n", encoding="utf-8")
    _dist_info(tree, "agent_family", {"url": "file:///x", "dir_info": {"editable": True}})

    assert len(escapes(tree)) == 3


def test_check_tree_raises_its_own_refusal_code(tmp_path: Path) -> None:
    """The ledger's `refused_check` tells the operator which fault this was, so it
    is neither `manifest` nor a bare step failure."""
    tree = _tree(tmp_path)
    (_site(tree) / "_editable_impl_chaperone.pth").write_text("/opt/x/src\n", encoding="utf-8")

    with pytest.raises(Refusal) as raised:
        check_tree("chaperone", tree)

    assert raised.value.code is RefusalCode.EDITABLE
    assert raised.value.subject == "chaperone"


# ---- the executor -----------------------------------------------------------


#: `manifest_text` writes no `build`, and every test below needs one: the
#: fake `uv` writes the staged tree from a hook on this argv.
BUILD_FIELD = """runs_as: operator
build:
  - ["/usr/local/bin/uv", "sync", "--frozen", "--no-editable", "--package", "chaperone"]"""


def _manifest(tmp_path: Path, name: str = "chaperone", **edits: str) -> ComponentManifest:
    text = manifest_text(name).replace("runs_as: operator", BUILD_FIELD)
    root = tmp_path / "components"
    text = text.replace(f"/opt/components/{name}", str(root / name))
    for old, new in edits.items():
        text = text.replace(old, new)

    return parse_manifest(text, f"{name}/component.yaml")


def _build(tmp_path: Path, write: str) -> tuple[FakeRun, ComponentManifest, Path]:
    """A build whose `uv sync` writes `write` into the staged site-packages."""
    manifest = _manifest(tmp_path)
    paths = paths_of(manifest, (tmp_path / "components",))

    def make(_: object) -> None:
        tree = _tree(paths.new.parent, paths.new.name)
        if write:
            (_site(tree) / "_editable_impl_chaperone.pth").write_text(write, encoding="utf-8")

    run = FakeRun(answers=git_host_answers(), hooks={"sync": make})
    source = tmp_path / "work" / "chaperone"
    source.mkdir(parents=True)
    Installer(fake_host(tmp_path, run)).build(manifest, source, paths)

    return run, manifest, paths.new


def test_a_staged_tree_that_escapes_refuses_the_release(tmp_path: Path) -> None:
    with pytest.raises(Refusal) as raised:
        _build(tmp_path, "/opt/creche/chaperone/src\n")

    assert raised.value.code is RefusalCode.EDITABLE


def test_a_refused_build_swaps_nothing(tmp_path: Path) -> None:
    """The check runs at step 8, so the live tree is untouched: `install.to`
    still holds whatever was there and no rename has happened."""
    live = tmp_path / "components" / "chaperone"
    (live / "bin").mkdir(parents=True)
    (live / "bin" / "marker").write_text("live\n", encoding="utf-8")

    with pytest.raises(Refusal):
        _build(tmp_path, "/opt/creche/chaperone/src\n")

    assert (live / "bin" / "marker").is_file()


def test_a_self_contained_staged_tree_builds(tmp_path: Path) -> None:
    run, _, new = _build(tmp_path, "")

    assert run.ran("sync")
    assert new.is_dir()


def test_a_component_that_is_not_a_venv_is_not_walked(tmp_path: Path) -> None:
    """A compose project and an image tree have no site-packages, and the
    walk would refuse both for the one reason that does not apply to them."""
    manifest = _manifest(tmp_path, "infra", **{f"kind: {Kind.VENV}": f"kind: {Kind.COMPOSE}"})
    assert manifest.kind is Kind.COMPOSE

    paths = paths_of(manifest, (tmp_path / "components",))

    def make(_: object) -> None:
        (paths.new / "bin").mkdir(parents=True, exist_ok=True)
        hook = paths.new / "bin" / "infra-verify"
        hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        hook.chmod(0o755)

    run = FakeRun(answers=git_host_answers(), hooks={"sync": make})
    source = tmp_path / "work" / "infra"
    source.mkdir(parents=True)
    Installer(fake_host(tmp_path, run)).build(manifest, source, paths)

    assert paths.new.is_dir()
