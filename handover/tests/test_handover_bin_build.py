"""Step 8 for a `kind: binary` component: the unpack.

CI builds the programs, packs them into one archive and signs it. Root runs
no build of a binary component. Step 8 unpacks the archive that check P6
verified, a `VerifiedArtifact`, into `<install.to>.new`. Then come the two
checks that every program tree gets: the staged verify hook and the walk.

1. No command of the manifest's `build` runs, and no child starts.
2. The steps that only a venv needs do not run either: no
   `uv venv --relocatable`, and no walk of a `site-packages` that a binary
   tree does not have.
3. Each fault of the unpack is a failed stage.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from handover.catalog import BINARY_INSTALL_ENV, Kind
from handover.executor import install
from handover.executor.install import RELOCATABLE_ARGV, VENV_ENV_NAME, StepFailed
from handover.executor.unpack import UnpackFault, UnpackRefused
from handover_bin_fixtures import (
    BINARY_NAME,
    PROGRAM_BYTES,
    PROGRAMS,
    Staging,
    archive_bytes,
    make_staging,
    verified_artifact,
)


def _unpack(staging: Staging, archive: bytes | None = None, component: str = BINARY_NAME) -> None:
    """Step 8 for the binary manifest of `staging`, with the real unpack of
    an archive that check P6 verified."""
    verified = verified_artifact(staging.tmp_path / "copy", component, archive)
    staging.installer.build(staging.manifest, staging.source, staging.paths, verified)


def test_ci_sets_cargos_install_root() -> None:
    """The variable of a binary build is the pure half's, for CI. Root sets
    it for no child."""
    assert BINARY_INSTALL_ENV == "CARGO_INSTALL_ROOT"
    assert VENV_ENV_NAME == "UV_PROJECT_ENVIRONMENT"


def test_a_binary_stage_unpacks_the_verified_archive(tmp_path: Path) -> None:
    staging = make_staging(tmp_path)

    _unpack(staging)

    new = staging.paths.new
    assert sorted(one.name for one in (new / "bin").iterdir()) == sorted(PROGRAMS)
    for name in PROGRAMS:
        facts = os.lstat(new / "bin" / name)
        assert stat.S_ISREG(facts.st_mode), name
        assert stat.S_IMODE(facts.st_mode) == 0o755, name
        assert (new / "bin" / name).read_bytes() == PROGRAM_BYTES


def test_a_binary_stage_starts_no_child(tmp_path: Path) -> None:
    """The manifest's `build` argv is CI's. Root starts no child with it,
    and no other child either."""
    staging = make_staging(tmp_path)

    _unpack(staging)

    assert staging.manifest.build
    assert staging.run.seen == []


def test_a_binary_stage_makes_no_python_environment(tmp_path: Path) -> None:
    """`uv venv --relocatable` is a venv step."""
    staging = make_staging(tmp_path)

    _unpack(staging)

    assert all(one.argv[:3] != RELOCATABLE_ARGV for one in staging.run.seen)


def test_a_binary_tree_with_no_site_packages_stages(tmp_path: Path) -> None:
    """The venv walk refuses a tree that has no `site-packages`. A tree of
    compiled programs has none, and is not walked as a venv."""
    staging = make_staging(tmp_path)

    _unpack(staging)

    assert (staging.paths.new / "bin" / BINARY_NAME).is_file()
    assert not list(staging.paths.new.glob("lib/*/site-packages"))


def test_a_stale_staged_tree_is_removed_first(tmp_path: Path) -> None:
    """The unpack makes `<install.to>.new` itself. A tree that an earlier
    run left there goes first."""
    staging = make_staging(tmp_path)
    (staging.paths.new / "bin").mkdir(parents=True)
    (staging.paths.new / "bin" / "stale").write_bytes(b"left behind")

    _unpack(staging)

    assert not (staging.paths.new / "bin" / "stale").exists()


def test_a_binary_stage_with_no_verified_archive_fails(tmp_path: Path) -> None:
    staging = make_staging(tmp_path)

    with pytest.raises(StepFailed, match="no archive that check P6 verified"):
        staging.installer.build(staging.manifest, staging.source, staging.paths)

    assert not staging.paths.new.exists()


def test_the_verified_archive_of_another_component_fails(tmp_path: Path) -> None:
    staging = make_staging(tmp_path)

    with pytest.raises(StepFailed, match="no archive that check P6 verified"):
        _unpack(staging, component="caregiver")


def test_a_copy_that_left_after_the_check_fails_the_stage(tmp_path: Path) -> None:
    staging = make_staging(tmp_path)
    verified = verified_artifact(staging.tmp_path / "copy")
    verified.archive.unlink()

    with pytest.raises(StepFailed, match="could not be unpacked: FileNotFoundError"):
        staging.installer.build(staging.manifest, staging.source, staging.paths, verified)


def test_a_refused_unpack_fails_the_stage_with_its_fixed_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(archive: Path, into: Path, component: str | None = None) -> None:
        del archive, into, component
        raise UnpackRefused(UnpackFault.EXTRA_BYTES)

    staging = make_staging(tmp_path)
    monkeypatch.setattr(install, "extract_tar", refuse)

    with pytest.raises(StepFailed) as caught:
        _unpack(staging)

    assert caught.value.detail == f"{BINARY_NAME}: {UnpackFault.EXTRA_BYTES}"


def test_an_archive_with_no_verify_hook_stops_the_stage(tmp_path: Path) -> None:
    """The check every kind gets runs after the unpack."""
    staging = make_staging(tmp_path)

    with pytest.raises(StepFailed, match="verify hook is missing"):
        _unpack(staging, archive_bytes(programs={BINARY_NAME: PROGRAM_BYTES}))


@pytest.mark.parametrize("kind", [Kind.VENV, Kind.COMPOSE, Kind.OCI_IMAGE])
def test_every_other_kind_still_gets_the_uv_variable(tmp_path: Path, kind: Kind) -> None:
    """The kinds the executor built before read the variable they read
    before, so no existing build changes."""
    staging = make_staging(tmp_path, made=None, kind=kind)

    with pytest.raises(StepFailed, match="wrote no"):
        staging.build()

    (build,) = staging.builds()
    assert build.env == ((VENV_ENV_NAME, str(staging.paths.new)),)
