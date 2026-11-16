"""`ci-runner-image/bin/ci-runner-image-release`, beside a stand-in image
build (docs/design/ci-runner.md, section 8, PR 3).

The script makes the files of the ci-runner-image row from the output of
`ci-runner-image/bin/ci-runner-image-build`. The real build needs the sudo
of a GitHub runner, so each test runs a copy of the script beside a
stand-in build in the same tree. The stand-in writes the four files of the
real build: `root.qcow2`, `vmlinuz`, `initrd.img` and `SHA256SUMS`. The
real `zstd` and the real `sha256sum` run.

The tests hold the output to two other sources:

1. `artifacts.disk_files`: the four files that `handover disk-assets`
   names, and so the files that root installs.
2. `ciimage.check_image`: the verify hook of the row passes on the output.
   So the build and the hook read one form of `digests.sha256`.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from typing import Final

import pytest
from handover.artifacts import disk_files
from handover.catalog import CI_DIGESTS_FILE, CI_IMAGE, CI_ROOT_DISK, CI_ROOT_FILE
from handover.ciimage import check_image
from handover.discovery import read_one

REPO: Final = Path(__file__).resolve().parents[2]
SCRIPT: Final = REPO / "ci-runner-image" / "bin" / "ci-runner-image-release"

#: The digest list that the script writes beside the files, for the `sign`
#: job.
SUMS: Final = "SHA256SUMS"

#: The stand-in build: the four files of the real build in CI_IMAGE_OUT.
#: STUB_FAIL=1 fails it before any file. STUB_BAD_SUMS=1 writes a
#: SHA256SUMS that does not match the kernel.
BUILD_STUB: Final = """#!/usr/bin/env bash
set -euo pipefail
[[ "${STUB_FAIL:-0}" == 0 ]] || exit 1
mkdir -p "$CI_IMAGE_OUT"
cd "$CI_IMAGE_OUT"
head -c 300000 /dev/urandom > root.qcow2
printf 'a kernel\\n' > vmlinuz
printf 'an initrd\\n' > initrd.img
sha256sum root.qcow2 vmlinuz initrd.img > SHA256SUMS
if [[ "${STUB_BAD_SUMS:-0}" == 1 ]]; then
  printf 'another kernel\\n' > vmlinuz
fi
"""

needs_zstd = pytest.mark.skipif(shutil.which("zstd") is None, reason="no zstd on this machine")


def _tree(tmp_path: Path) -> Path:
    """A copy of the script beside the stand-in build, in a tree of the
    shape of `ci-runner-image/`."""
    bin_dir = tmp_path / "tree" / "ci-runner-image" / "bin"
    bin_dir.mkdir(parents=True)
    script = bin_dir / SCRIPT.name
    shutil.copy2(SCRIPT, script)
    build = bin_dir / "ci-runner-image-build"
    build.write_text(BUILD_STUB, encoding="utf-8")
    build.chmod(0o755)

    return script


def _run(script: Path, out: Path | str | None, **changed: str) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ["PATH"],
        **({} if out is None else {"DISK_OUT": str(out)}),
        **changed,
    }

    return subprocess.run(
        ["bash", str(script)], env=env, capture_output=True, text=True, timeout=60, check=False
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_the_manifest_builds_with_this_script() -> None:
    """One source for the build: the argv of `ci-runner-image/component.toml`."""
    manifest = read_one(REPO, CI_IMAGE).manifest

    assert manifest.build == (("/usr/bin/env", "bash", str(SCRIPT.relative_to(REPO))),)
    assert os.access(SCRIPT, os.X_OK)


@needs_zstd
def test_the_files_are_the_files_of_the_row(tmp_path: Path) -> None:
    """The four files of `handover disk-assets` and SHA256SUMS, and no other
    entry. SHA256SUMS holds the SHA-256 of each file as it ships."""
    out = tmp_path / "out"

    done = _run(_tree(tmp_path), out)

    assert done.returncode == 0, done.stderr
    names = [one.name for one in disk_files(CI_IMAGE)]
    assert sorted(path.name for path in out.iterdir()) == sorted([*names, SUMS])
    assert (out / SUMS).read_text(encoding="utf-8") == "".join(
        f"{_sha256(out / name)}  {name}\n" for name in names
    )


@needs_zstd
def test_the_verify_hook_passes_on_the_files(tmp_path: Path) -> None:
    """`digests.sha256` is SHA256SUMS of the build, and the zstd stream
    gives the root disk of the build. So the hook passes each check."""
    out = tmp_path / "out"

    done = _run(_tree(tmp_path), out)

    assert done.returncode == 0, done.stderr
    lines = (out / CI_DIGESTS_FILE).read_text(encoding="utf-8").splitlines()
    assert [line.split("  ")[1] for line in lines] == [CI_ROOT_DISK, "vmlinuz", "initrd.img"]
    assert all(one.ok for one in check_image(out)), check_image(out)
    assert f"{CI_ROOT_FILE} is " in done.stderr


@needs_zstd
@pytest.mark.parametrize(
    ("changed", "fault"),
    [
        ({"STUB_FAIL": "1"}, "the image build failed"),
        ({"STUB_BAD_SUMS": "1"}, "the files of the build do not match its SHA256SUMS"),
    ],
    ids=["a-failed-build", "sums-of-other-bytes"],
)
def test_a_failed_step_leaves_the_output_empty(
    tmp_path: Path, changed: dict[str, str], fault: str
) -> None:
    out = tmp_path / "out"

    done = _run(_tree(tmp_path), out, **changed)

    assert done.returncode == 1
    assert done.stderr.endswith(f"ci-runner-image-release: {fault}\n")
    assert list(out.iterdir()) == []


@pytest.mark.parametrize(
    ("out", "fault"),
    [
        (None, "the variable DISK_OUT is not set"),
        ("out", "DISK_OUT is not absolute: out"),
    ],
    ids=["no-variable", "a-relative-path"],
)
def test_a_bad_output_variable_is_refused(tmp_path: Path, out: str | None, fault: str) -> None:
    done = _run(_tree(tmp_path), out)

    assert done.returncode == 1
    assert done.stderr == f"ci-runner-image-release: {fault}\n"


def test_an_output_directory_that_holds_a_file_is_refused(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "left").write_text("from another run\n", encoding="utf-8")

    done = _run(_tree(tmp_path), out)

    assert done.returncode == 1
    assert done.stderr == "ci-runner-image-release: DISK_OUT is not empty\n"
