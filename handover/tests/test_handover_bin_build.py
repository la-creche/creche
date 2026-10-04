"""Step 8 for a `kind: binary` component: the build.

The trust model is the venv one. The build argv comes from the manifest. It
runs as root, in the fetched tree, with the fixed environment and the same
time limit as a venv build. Two things differ, and both are held here.

1. The variable that names the staged tree. `uv` reads
   `UV_PROJECT_ENVIRONMENT`. `cargo install` reads `CARGO_INSTALL_ROOT` and
   writes each program at `<that path>/bin/<name>`.
2. The steps that only a venv needs do not run: no `uv venv --relocatable`,
   and no walk of a `site-packages` that a binary tree does not have.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from handover.catalog import Kind
from handover.executor.host import As
from handover.executor.install import (
    BINARY_ENV_NAME,
    BUILD_TIMEOUT_S,
    RELOCATABLE_ARGV,
    VENV_ENV_NAME,
    StepFailed,
)
from handover_bin_fixtures import BINARY_NAME, make_staging


def test_the_binary_variable_is_cargos_install_root() -> None:
    assert BINARY_ENV_NAME == "CARGO_INSTALL_ROOT"
    assert VENV_ENV_NAME == "UV_PROJECT_ENVIRONMENT"


def test_a_binary_build_gets_the_staged_tree_as_the_cargo_root(tmp_path: Path) -> None:
    """`cargo install` with no `--root` writes under `CARGO_INSTALL_ROOT`,
    so a build argv with no interpolation lands in `<install.to>.new`."""
    staging = make_staging(tmp_path)

    staging.build()

    (build,) = staging.builds()
    assert build.env == ((BINARY_ENV_NAME, str(staging.paths.new)),)


def test_a_binary_build_runs_as_a_venv_build_runs(tmp_path: Path) -> None:
    """The same user rule, the same working directory and the same time
    limit. The argv is the manifest's own, word for word."""
    staging = make_staging(tmp_path)

    staging.build()

    (build,) = staging.builds()
    assert build.argv == staging.manifest.build[0]
    assert build.identity is As.ROOT
    assert build.cwd == staging.source
    assert build.timeout_s == BUILD_TIMEOUT_S


def test_a_binary_build_makes_no_python_environment(tmp_path: Path) -> None:
    """`uv venv --relocatable` is a venv step. Run for a binary component,
    it makes a directory that the build then has to work around."""
    staging = make_staging(tmp_path)

    staging.build()

    assert all(one.argv[:3] != RELOCATABLE_ARGV for one in staging.run.seen)
    assert len(staging.run.seen) == 1


def test_a_binary_tree_with_no_site_packages_builds(tmp_path: Path) -> None:
    """The venv walk refuses a tree that has no `site-packages`. A tree of
    compiled programs has none, and is not walked as a venv."""
    staging = make_staging(tmp_path)

    staging.build()

    assert (staging.paths.new / "bin" / BINARY_NAME).is_file()
    assert not list(staging.paths.new.glob("lib/*/site-packages"))


def test_a_binary_build_that_fails_stops_the_stage(tmp_path: Path) -> None:
    staging = make_staging(tmp_path)
    staging.run.fails["cargo"] = 101

    with pytest.raises(StepFailed, match="build failed"):
        staging.build()


def test_a_binary_build_that_writes_no_tree_stops_the_stage(tmp_path: Path) -> None:
    """A build that exits 0 and installs nothing is not a staged tree."""
    staging = make_staging(tmp_path, made=None)

    with pytest.raises(StepFailed, match="wrote no"):
        staging.build()


@pytest.mark.parametrize("kind", [Kind.VENV, Kind.COMPOSE, Kind.OCI_IMAGE])
def test_every_other_kind_still_gets_the_uv_variable(tmp_path: Path, kind: Kind) -> None:
    """The kinds the executor built before read the variable they read
    before, so no existing build changes."""
    staging = make_staging(tmp_path, made=None, kind=kind)

    with pytest.raises(StepFailed, match="wrote no"):
        staging.build()

    (build,) = staging.builds()
    assert build.env == ((VENV_ENV_NAME, str(staging.paths.new)),)
