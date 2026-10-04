"""The unit rules, for a `kind: binary` component.

Two rules of the executor were keyed on `kind: venv`, because a venv was
the only tree that holds the program its unit starts. A tree of compiled
programs holds them too, at the same paths. So both rules apply to it.

1. Contract 06 §1 rule 8: the unit a release restarts must start a program
   inside `install.to`. If it does not, the release swaps a tree and
   changes nothing that runs.
2. `install.py` rule 8: a sibling unit that runs out of the same tree is
   staged, kept, refreshed and restarted with the component's own unit.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from handover.catalog import Kind
from handover.errors import Refusal, RefusalCode
from handover.executor.install import PROGRAM_KINDS
from handover_bin_fixtures import SIBLING_UNIT, UNIT, make_staging, unit_text

#: A program in no tree that a release installs.
ELSEWHERE = "/opt/creche/.venv/bin/noticeboard"


def test_the_kinds_whose_tree_holds_its_programs() -> None:
    assert frozenset({Kind.VENV, Kind.BINARY}) == PROGRAM_KINDS


# -- contract 06 §1 rule 8 ---------------------------------------------------


def test_a_binary_unit_that_starts_a_program_outside_the_tree_is_refused(
    tmp_path: Path,
) -> None:
    """The installed unit starts a program that the release does not
    install. The check runs before the build, so nothing has moved."""
    staging = make_staging(tmp_path, unit=UNIT)
    staging.install_unit(unit_text(ELSEWHERE))

    with pytest.raises(Refusal) as caught:
        staging.installer.check_unit_binds(staging.manifest, staging.source)

    assert caught.value.code is RefusalCode.UNIT
    assert "is not inside" in caught.value.detail
    assert staging.builds() == []


def test_a_binary_unit_that_starts_its_own_tree_passes(tmp_path: Path) -> None:
    staging = make_staging(tmp_path, unit=UNIT)
    staging.install_unit(unit_text(staging.program()))

    staging.installer.check_unit_binds(staging.manifest, staging.source)


def test_a_binary_unit_with_no_program_is_refused(tmp_path: Path) -> None:
    """A unit root reads no program out of is a unit root cannot say
    starts the tree."""
    staging = make_staging(tmp_path, unit=UNIT)
    staging.install_unit("[Unit]\nDescription=starts nothing\n")

    with pytest.raises(Refusal) as caught:
        staging.installer.check_unit_binds(staging.manifest, staging.source)

    assert caught.value.code is RefusalCode.UNIT


def test_the_release_that_corrects_a_binary_unit_is_not_refused(tmp_path: Path) -> None:
    """The unit that is read is the one step 9 leaves in force: the file
    the fetched tree carries, where a unit is installed."""
    staging = make_staging(tmp_path, unit=UNIT)
    staging.install_unit(unit_text(ELSEWHERE))
    staging.carry_unit(unit_text(staging.program()))

    staging.installer.check_unit_binds(staging.manifest, staging.source)


def test_a_binary_unit_that_is_not_installed_is_not_checked(tmp_path: Path) -> None:
    """Step 9 lists such a unit under `manual` and restarts nothing."""
    staging = make_staging(tmp_path, unit=UNIT)

    staging.installer.check_unit_binds(staging.manifest, staging.source)


def test_a_binary_component_with_no_unit_is_not_checked(tmp_path: Path) -> None:
    staging = make_staging(tmp_path)

    staging.installer.check_unit_binds(staging.manifest, staging.source)


# -- sibling units -----------------------------------------------------------


def _with_sibling(tmp_path: Path, sibling_starts: str | None = None):
    """A binary component whose unit is installed, with a second installed
    unit that the fetched tree also carries."""
    staging = make_staging(tmp_path, unit=UNIT)
    own = unit_text(staging.program())
    door = unit_text(sibling_starts or staging.program("noticeboard-door"))
    staging.install_unit(own)
    staging.carry_unit(own)
    staging.install_unit(door, SIBLING_UNIT)
    staging.carry_unit(door, SIBLING_UNIT)

    return staging


def test_a_binary_components_sibling_unit_is_staged_with_its_own(tmp_path: Path) -> None:
    """A component of compiled programs can run a second unit out of the
    same tree, as `attendance` runs its doors. Its file travels in the
    artifact, so step 9 can refresh it from the live tree."""
    staging = _with_sibling(tmp_path)
    staging.paths.new.mkdir(parents=True)

    staging.installer.stage_unit(staging.manifest, staging.source, staging.paths.new)

    assert (staging.paths.new / "systemd" / UNIT).is_file()
    assert (staging.paths.new / "systemd" / SIBLING_UNIT).is_file()


def test_a_binary_components_sibling_is_one_of_its_units(tmp_path: Path) -> None:
    staging = _with_sibling(tmp_path)

    found = staging.installer.sibling_units(staging.manifest, staging.source)

    assert [one.name for one in found] == [SIBLING_UNIT]


def test_a_unit_that_starts_another_tree_is_no_sibling(tmp_path: Path) -> None:
    """The four tests of a sibling hold for a binary component as they do
    for a venv: a unit that starts a program outside the tree is not one."""
    staging = _with_sibling(tmp_path, sibling_starts=ELSEWHERE)

    assert staging.installer.sibling_units(staging.manifest, staging.source) == ()


@pytest.mark.parametrize("kind", [Kind.COMPOSE, Kind.OCI_IMAGE])
def test_a_kind_with_no_program_tree_has_no_sibling(tmp_path: Path, kind: Kind) -> None:
    """A compose project's unit starts docker, and an image has no unit.
    Neither kind gains the unit rules."""
    staging = make_staging(tmp_path, unit=UNIT, kind=kind)
    own = unit_text(staging.program())
    staging.install_unit(own)
    staging.install_unit(own, SIBLING_UNIT)
    staging.carry_unit(own, SIBLING_UNIT)

    assert staging.installer.sibling_units(staging.manifest, staging.source) == ()
    staging.install_unit(unit_text(ELSEWHERE))
    staging.installer.check_unit_binds(staging.manifest, staging.source)
