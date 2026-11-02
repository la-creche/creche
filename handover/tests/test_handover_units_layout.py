"""The layout of a `units` tree (contract 06 §1 rule 12, §8.3 item 1).

`executor/units.py`. Step 8 of a `host` release reads the unit files of the
staged tree and refuses, with code `unit`, a tree that holds anything else
under `units/`.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

import pytest
from handover.errors import Refusal, RefusalCode
from handover.executor.units import (
    MAX_UNIT_FILES,
    Scope,
    UnitFile,
    component_units,
    unit_files,
)
from handover.manifest import Markup, parse_manifest
from handover_fixtures import HOST_MANIFEST, manifest_text

HOST = "host"
TIMER = "units/user/creche-follow.timer"
SERVICE = "units/user/creche-follow.service"
DROP_IN = "units/system/creche-chaperone.service.d/creche.conf"

#: The unit of a component, as its own manifest names it.
COMPONENT_UNIT = "creche-noticeboard.service"


def _tree(tmp_path: Path, *files: str) -> Path:
    tree = tmp_path / "host.new"
    tree.mkdir()
    for name in files:
        (tree / name).parent.mkdir(parents=True, exist_ok=True)
        (tree / name).write_text(f"# {name}\n", encoding="utf-8")

    return tree


def _refusal(tree: Path, owned: frozenset[str] = frozenset()) -> Refusal:
    with pytest.raises(Refusal) as caught:
        unit_files(HOST, tree, owned)

    assert caught.value.code is RefusalCode.UNIT
    assert caught.value.subject == HOST

    return caught.value


def test_the_two_kinds_of_unit_file_are_read(tmp_path: Path) -> None:
    tree = _tree(tmp_path, TIMER, SERVICE, DROP_IN, "bin/host-verify")

    assert unit_files(HOST, tree, frozenset()) == (
        UnitFile(Scope.SYSTEM, "creche-chaperone.service", PurePosixPath(DROP_IN)),
        UnitFile(Scope.USER, "creche-follow.service", PurePosixPath(SERVICE)),
        UnitFile(Scope.USER, "creche-follow.timer", PurePosixPath(TIMER)),
    )


def test_each_file_is_linked_where_section_8_3_says(tmp_path: Path) -> None:
    user, system = tmp_path / "user", tmp_path / "system"
    drop_in, timer = unit_files(HOST, _tree(tmp_path, TIMER, DROP_IN), frozenset())

    assert timer.installed(user, system) == user / "creche-follow.timer"
    assert drop_in.installed(user, system) == (
        system / "creche-chaperone.service.d" / "creche.conf"
    )


def test_a_tree_with_no_units_directory_holds_no_unit_file(tmp_path: Path) -> None:
    assert unit_files(HOST, _tree(tmp_path, "bin/host-verify"), frozenset()) == ()


def test_the_units_of_creche_release_today_are_read(tmp_path: Path) -> None:
    """The 13 files of creche-release's `units/` on 2026-11-02."""
    names = [
        f"units/user/creche-{unit}.{kind}"
        for unit in ("code-corpus-sync", "follow", "library@", "registry-sync", "sbx-drift")
        for kind in ("service", "timer")
    ]
    names += ["units/user/creche-watchdog.service", "units/user/creche-watchdog.timer", DROP_IN]

    assert len(unit_files(HOST, _tree(tmp_path, *names), frozenset())) == 13


@pytest.mark.parametrize(
    "name",
    [
        "units/README.md",
        "units/user/notes.txt",
        "units/user/x.target",
        "units/user/sub/x.timer",
        "units/system/x.service",
        "units/system/x.service.d/creche.txt",
        "units/system/x.d/creche.conf",
        "units/system/x.service.d/deeper/creche.conf",
        "units/other/x.timer",
    ],
)
def test_a_file_outside_the_two_layouts_is_refused(tmp_path: Path, name: str) -> None:
    refusal = _refusal(_tree(tmp_path, TIMER, name))

    assert refusal.detail.startswith(name)
    assert "is not units/user/<unit>" in refusal.detail


@pytest.mark.parametrize(
    "name",
    [
        "units/user/creche-handover.path",
        "units/user/creche-handover.service",
        "units/user/creche-handover-intake.service",
        "units/system/creche-handover.service.d/override.conf",
    ],
)
def test_a_handover_unit_is_refused(tmp_path: Path, name: str) -> None:
    refusal = _refusal(_tree(tmp_path, name))

    assert "a handover unit" in refusal.detail


def test_a_component_unit_is_refused(tmp_path: Path) -> None:
    refusal = _refusal(_tree(tmp_path, f"units/user/{COMPONENT_UNIT}"), frozenset({COMPONENT_UNIT}))

    assert "the unit of a component" in refusal.detail


def test_a_drop_in_may_extend_a_component_unit(tmp_path: Path) -> None:
    """Contract 06 §1 rule 12 lists a drop-in that extends a component's unit
    among what `host` holds."""
    name = f"units/system/{COMPONENT_UNIT}.d/creche.conf"

    (found,) = unit_files(HOST, _tree(tmp_path, name), frozenset({COMPONENT_UNIT}))

    assert (found.scope, found.unit) == (Scope.SYSTEM, COMPONENT_UNIT)


def test_a_link_is_refused(tmp_path: Path) -> None:
    tree = _tree(tmp_path, TIMER)
    (tree / "units" / "user" / "again.timer").symlink_to("creche-follow.timer")

    assert "is a link" in _refusal(tree).detail


def test_a_linked_directory_is_refused(tmp_path: Path) -> None:
    tree = _tree(tmp_path, TIMER)
    (tmp_path / "elsewhere").mkdir()
    (tree / "units" / "system").symlink_to(tmp_path / "elsewhere")

    assert _refusal(tree).detail.startswith("units/system is a link")


def test_a_units_entry_that_is_a_file_is_refused(tmp_path: Path) -> None:
    assert "is a link or not a directory" in _refusal(_tree(tmp_path, "units")).detail


def test_a_fifo_is_refused(tmp_path: Path) -> None:
    tree = _tree(tmp_path, TIMER)
    os.mkfifo(tree / "units" / "user" / "x.timer")

    assert "is a link or not a directory" in _refusal(tree).detail


def test_more_than_the_cap_is_refused(tmp_path: Path) -> None:
    names = [f"units/user/u{index}.timer" for index in range(MAX_UNIT_FILES + 1)]

    assert "more than" in _refusal(_tree(tmp_path, *names)).detail


def test_the_cap_itself_is_read(tmp_path: Path) -> None:
    names = [f"units/user/u{index}.timer" for index in range(MAX_UNIT_FILES)]

    assert len(unit_files(HOST, _tree(tmp_path, *names), frozenset())) == MAX_UNIT_FILES


def test_the_component_units_are_the_units_the_manifests_name() -> None:
    text = manifest_text("noticeboard").replace("unit: null", f"unit: {COMPONENT_UNIT}")
    noticeboard = parse_manifest(text, "c.yaml", Markup.YAML)
    host = parse_manifest(HOST_MANIFEST, "component.toml", Markup.TOML)

    assert component_units([noticeboard, host]) == frozenset({COMPONENT_UNIT})
