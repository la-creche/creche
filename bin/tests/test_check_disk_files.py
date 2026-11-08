"""`bin/check-disk-files.sh`, against a stand-in for the `handover` command.

The `disks` job of release.yml runs the script after the build of the row,
and before the `sign` job signs any file (decision CH78 rule 3). The
stand-in prints the real line form of `handover disk-assets`
(`artifacts.DiskRow.lines`), so the limits are the real ones:
`DISK_FILE_MAX_BYTES` (2 GiB - 1) for a disk and the kernel, and
`DIGESTS_MAX_BYTES` (64 KiB) for `digests.json`. A file past a limit is a
sparse file, so a test writes no 2 GiB.

The ends held here:

1. The files of a good build pass, and the script reads the row of the one
   repository that the job names.
2. A file at its limit passes. A file one byte past its limit fails, for
   each file of the row.
3. A missing file, a link, and an entry that is no file of the row fail.
4. A component with no row, a row in another form, a command that fails
   and an out variable that is not set fail.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Final

import pytest
from handover.artifacts import DiskRow, disk_files
from handover.catalog import DIGESTS_FILE, DIGESTS_MAX_BYTES, DISK_FILE_MAX_BYTES, Repo

REPO: Final = Path(__file__).resolve().parents[2]
SCRIPT: Final = REPO / "bin" / "check-disk-files.sh"

COMPONENT: Final = "disks"
THIS_REPOSITORY: Final = str(Repo.CRECHE)
DIGEST_FILE: Final = "SHA256SUMS"

#: The build of the row, as `disks/component.toml` names it.
BUILD: Final = ("/usr/bin/env", "bash", "disks/bin/playpen-release-disks")

#: The stand-in `handover`: it writes its words to STUB_LOG, prints the file
#: STUB_ROWS and ends with STUB_STATUS.
CLI_STUB: Final = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$STUB_LOG"
cat "$STUB_ROWS"
exit "${STUB_STATUS:-0}"
"""

#: The names of the signed files of the row, in the order of the row.
FILES: Final = tuple(one.name for one in disk_files())

#: The limit of each file of the row.
LIMIT: Final = {one.name: one.max_bytes for one in disk_files()}


def _rows(component: str = COMPONENT) -> str:
    return "".join(f"{line}\n" for line in DiskRow(component, (BUILD,)).lines())


def _good_out(tmp_path: Path) -> Path:
    """What a good build leaves: each file of the row, small, and
    SHA256SUMS."""
    out = tmp_path / "out"
    out.mkdir()
    for name in FILES:
        (out / name).write_bytes(f"the bytes of {name}\n".encode())
    (out / DIGEST_FILE).write_text("the sums\n", encoding="utf-8")

    return out


def _sized(path: Path, size: int) -> None:
    """A file of `size` bytes. A large one is sparse."""
    with path.open("wb") as handle:
        handle.truncate(size)


def _run(
    tmp_path: Path, out: Path | None, rows: str | None = None, **changed: str
) -> subprocess.CompletedProcess[str]:
    stub = tmp_path / "handover"
    stub.write_text(CLI_STUB, encoding="utf-8")
    stub.chmod(0o755)
    (tmp_path / "rows").write_text(_rows() if rows is None else rows, encoding="utf-8")
    env = {
        "PATH": os.environ["PATH"],
        "TMPDIR": str(tmp_path),
        "RELEASE_CLI": str(stub),
        "STUB_LOG": str(tmp_path / "log"),
        "STUB_ROWS": str(tmp_path / "rows"),
        "DISK_REPO": THIS_REPOSITORY,
        "DISK_ROW": COMPONENT,
        **({} if out is None else {"DISK_OUT": str(out)}),
        **changed,
    }

    return subprocess.run(
        [str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60, check=False
    )


def _refused(done: subprocess.CompletedProcess[str], fault: str) -> None:
    assert done.returncode == 1
    assert done.stderr == f"check-disk-files: {fault}\n"


# ---- 1. a good build ---------------------------------------------------------


def test_the_files_of_a_good_build_pass(tmp_path: Path) -> None:
    out = _good_out(tmp_path)

    done = _run(tmp_path, out)

    assert done.returncode == 0, done.stderr
    assert (tmp_path / "log").read_text(encoding="utf-8") == (
        f"disk-assets --repo {THIS_REPOSITORY}\n"
    )
    for name in FILES:
        assert f"check-disk-files: {name}: " in done.stdout
    assert not list(tmp_path.glob("check-disk-files.*"))


def test_the_rows_are_the_real_line_form_of_the_catalog() -> None:
    """The stand-in prints what `handover disk-assets` prints for the row."""
    done = subprocess.run(
        [
            *("uv", "run", "--project", str(REPO / "handover"), "handover"),
            *("disk-assets", "--repo", THIS_REPOSITORY),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
        cwd=REPO,
    )

    assert done.stdout == _rows()


# ---- 2. the limits -----------------------------------------------------------


def test_the_limits_are_the_limits_of_the_row() -> None:
    """2 GiB - 1 for a disk and the kernel, 64 KiB for `digests.json`."""
    for name, limit in LIMIT.items():
        assert limit == (DIGESTS_MAX_BYTES if name == DIGESTS_FILE else DISK_FILE_MAX_BYTES)
    assert DISK_FILE_MAX_BYTES == 2**31 - 1


@pytest.mark.parametrize("name", FILES)
def test_a_file_at_its_limit_passes(tmp_path: Path, name: str) -> None:
    out = _good_out(tmp_path)
    _sized(out / name, LIMIT[name])

    done = _run(tmp_path, out)

    assert done.returncode == 0, done.stderr
    assert f"check-disk-files: {name}: {LIMIT[name]} bytes, at most {LIMIT[name]}\n" in (
        done.stdout
    )


@pytest.mark.parametrize("name", FILES)
def test_a_file_past_its_limit_fails(tmp_path: Path, name: str) -> None:
    """CH78 rule 3: the job fails before the `sign` job signs the file."""
    out = _good_out(tmp_path)
    _sized(out / name, LIMIT[name] + 1)

    done = _run(tmp_path, out)

    _refused(
        done,
        f"{COMPONENT}: {name} has {LIMIT[name] + 1} bytes, more than its limit of {LIMIT[name]}",
    )


# ---- 3. the entries of the directory -----------------------------------------


def test_a_missing_file_fails(tmp_path: Path) -> None:
    out = _good_out(tmp_path)
    (out / FILES[-1]).unlink()

    _refused(_run(tmp_path, out), f"{COMPONENT}: {FILES[-1]} is no regular file in DISK_OUT")


def test_a_link_fails(tmp_path: Path) -> None:
    """A link could name a file that no check read."""
    out = _good_out(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_bytes(b"small\n")
    (out / FILES[0]).unlink()
    (out / FILES[0]).symlink_to(elsewhere)

    _refused(_run(tmp_path, out), f"{COMPONENT}: {FILES[0]} is no regular file in DISK_OUT")


@pytest.mark.parametrize("entry", ["other.tar.gz", ".hidden", "a directory"])
def test_an_entry_that_is_no_file_of_the_row_fails(tmp_path: Path, entry: str) -> None:
    """The `sign` job signs a name that SHA256SUMS lists, an archive too. So
    no entry that this script did not check may reach it."""
    out = _good_out(tmp_path)
    (out / entry).mkdir() if " " in entry else (out / entry).write_bytes(b"x\n")

    _refused(
        _run(tmp_path, out), f"{COMPONENT}: DISK_OUT holds {entry}, which is no file of the row"
    )


# ---- 4. the row and the variables --------------------------------------------


def test_a_component_with_no_row_fails(tmp_path: Path) -> None:
    out = _good_out(tmp_path)

    _refused(_run(tmp_path, out, _rows("other")), f"{COMPONENT} has no disk-image row")


def test_a_row_in_another_form_fails_with_its_reason(tmp_path: Path) -> None:
    out = _good_out(tmp_path)
    rows = _rows().replace("out-env DISK_OUT", "out-env DISK OUT")

    _refused(_run(tmp_path, out, rows), "the out-env of a disk row is not a variable name")


def test_a_command_that_fails_fails(tmp_path: Path) -> None:
    out = _good_out(tmp_path)

    _refused(_run(tmp_path, out, STUB_STATUS="3"), "handover gave no disk rows")


def test_an_out_variable_that_is_not_set_fails(tmp_path: Path) -> None:
    _good_out(tmp_path)

    _refused(_run(tmp_path, None), "the variable DISK_OUT is not set")


@pytest.mark.parametrize("name", ["DISK_REPO", "DISK_ROW"])
def test_a_variable_that_is_not_set_fails(tmp_path: Path, name: str) -> None:
    out = _good_out(tmp_path)

    _refused(_run(tmp_path, out, **{name: ""}), f"the variable {name} is not set")


def test_an_out_variable_that_names_no_directory_fails(tmp_path: Path) -> None:
    out = tmp_path / "a file"
    out.write_bytes(b"x\n")

    _refused(_run(tmp_path, out), "DISK_OUT names no directory")


def test_the_files_of_another_row_are_not_read(tmp_path: Path) -> None:
    """Only the row of DISK_ROW counts. The other row names a file that the
    directory does not hold."""
    out = _good_out(tmp_path)
    other = _rows("other").replace(f"asset {DIGESTS_FILE}", "asset other.json")

    done = _run(tmp_path, out, other + _rows())

    assert done.returncode == 0, done.stderr
