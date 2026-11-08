"""What `bin/build-artifacts.sh` builds, walks, packs and refuses.

The job `artifacts` of `release.yml` runs the script. Root on the host
later refuses an archive that is not in the form of its row. No pull
request runs `release.yml`, so these tests are the first run that the
script gets.

The tests run the real script in a throwaway tree. Each program that only a
runner has is a stand-in that writes its words to a log: `rustup`, `cargo`,
`tar`, `gzip` and the `handover` command. The rows are the real line form of
`handover.artifacts.ArtifactRow.lines`, so a change of that form fails here.

Cases:

1. No row: one line, an empty `SHA256SUMS`, an empty step output, and no
   call of `rustup` or `cargo`.
2. One row: the words and the variables of each call, the needles of the
   walk, the member names of the pack and the digest file.
3. Each refusal: a tree with no program, a tree with another thing, a walk
   fault, a build fault, each of the three limits, a row that is not in the
   line form, and a path of a variable that is not absolute.
4. The plan: the script builds only the rows that `ARTIFACT_PLANNED` names.
   No planned row builds nothing, and a planned name with no row stops the
   run. A planned name of a disk-image row is skipped (decision CH76 rule
   5). The script reads `handover disk-assets` only for a planned name with
   no binary row, with the real line form of `handover.artifacts.DiskRow`.
5. `bin/lib/artifactrows.sh` is the one reader of the two line forms. Its
   own test, `test_artifactrows.py`, holds each text that it refuses.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from typing import Final

import pytest
from handover.artifacts import DiskRow, crate_row
from handover.catalog import (
    ARCHIVE_MAX_BYTES,
    ARCHIVE_MAX_MEMBERS,
    ARCHIVE_MAX_UNPACKED_BYTES,
    ARCHIVE_SUFFIX,
    BINARY_INSTALL_ENV,
    BINARY_TARGET,
    BUNDLE_SUFFIX,
    Repo,
)

REPO: Final = Path(__file__).resolve().parents[2]
SCRIPT: Final = REPO / "bin" / "build-artifacts.sh"

#: The one reader of the line form, and each script that sources it.
READER: Final = REPO / "bin" / "lib" / "artifactrows.sh"
READER_USERS: Final = [SCRIPT]

#: The catalog name of the repository, as the workflow gives it.
THIS_REPOSITORY: Final = str(Repo.CRECHE)

#: The crate of the row of each case, and its build words.
COMPONENT: Final = "agent-family"
CRATE_DIR: Final = "rust/crates/agent-family"
INSTALL_WORDS: Final = ["cargo", "install", "--locked", "--no-track", "--path", CRATE_DIR]
ARCHIVE: Final = f"{COMPONENT}-{BINARY_TARGET}{ARCHIVE_SUFFIX}"

#: The toolchain that the stand-in `rustup` names.
TOOLCHAIN: Final = "1.92.0-x86_64-unknown-linux-gnu"

#: The separator of the words of one logged call.
UNIT_SEPARATOR: Final = "\x1f"

#: The first line that the script prints with no row.
NO_ROW: Final = "build-artifacts: no binary component"
NO_PLANNED_ROW: Final = "build-artifacts: no planned binary component"

#: The step output with no row, and its form with a text.
OUTPUT_END: Final = "END_OF_DIGESTS"
EMPTY_OUTPUT: Final = f"digests<<{OUTPUT_END}\n{OUTPUT_END}\n"

#: Whether this is a run of CI. A case that needs `sha256sum` skips on a
#: development machine without it, and fails in CI.
IN_CI: Final = bool(os.environ.get("CI"))
needs_sha256sum = pytest.mark.skipif(
    not IN_CI and shutil.which("sha256sum") is None, reason="the script uses sha256sum"
)

STUB_MODE: Final = 0o755

#: `rustup`: it writes its directory and its words, and it names one
#: toolchain.
RUSTUP_STUB: Final = f"""#!/usr/bin/env bash
printf '%s\\037' rustup "$PWD" "$@" >> "$STUB_LOG"
printf '\\n' >> "$STUB_LOG"
if [[ "$1 $2" == "show active-toolchain" ]]; then
  echo "{TOOLCHAIN} (overridden by '$PWD/rust-toolchain.toml')"
fi
"""

#: `cargo`: it writes its directory, its words and the build variables.
#: Then it makes each program of STUB_PROGRAMS in bin/ of the install root,
#: each with STUB_BYTES and the mode STUB_PROGRAM_MODE. A name with a slash
#: makes a directory in bin/ too. STUB_RECORD leaves the record file that
#: cargo writes without `--no-track`. STUB_CARGO_STATUS is its exit status.
CARGO_STUB: Final = """#!/usr/bin/env bash
{
  printf '%s\\037' cargo "$PWD" "$@"
  printf 'CARGO_INSTALL_ROOT=%s\\037' "${CARGO_INSTALL_ROOT-<unset>}"
  printf 'CARGO_BUILD_TARGET=%s\\037' "${CARGO_BUILD_TARGET-<unset>}"
  printf 'RUSTUP_TOOLCHAIN=%s\\037' "${RUSTUP_TOOLCHAIN-<unset>}"
  printf 'CARGO_INCREMENTAL=%s\\037' "${CARGO_INCREMENTAL-<unset>}"
  printf 'RUSTFLAGS=%s\\037' "${RUSTFLAGS-<unset>}"
  printf 'CARGO_ENCODED_RUSTFLAGS=%s\\037' "${CARGO_ENCODED_RUSTFLAGS-<unset>}"
  printf '\\n'
} >> "$STUB_LOG"

if [[ -n "${STUB_CARGO_STATUS:-}" ]]; then
  exit "$STUB_CARGO_STATUS"
fi

mkdir -p "$CARGO_INSTALL_ROOT/bin"
for program in ${STUB_PROGRAMS-agent-family}; do
  mkdir -p "$(dirname "$CARGO_INSTALL_ROOT/bin/$program")"
  printf '%s' "${STUB_BYTES:-a program}" > "$CARGO_INSTALL_ROOT/bin/$program"
  chmod "${STUB_PROGRAM_MODE:-0755}" "$CARGO_INSTALL_ROOT/bin/$program"
done
if [[ -n "${STUB_RECORD:-}" ]]; then
  touch "$CARGO_INSTALL_ROOT/.crates.toml"
fi
"""

#: `tar`: it writes its directory and its words, and prints one text for
#: the archive. `tar` and `gzip` run at the same time, so each one writes
#: its line to the log in one write.
TAR_STUB: Final = """#!/usr/bin/env bash
line="$(printf '%s\\037' tar "$PWD" "$@")"
printf '%s\\n' "$line" >> "$STUB_LOG"
printf 'the members'
"""

#: `gzip`: it writes its words, then gives its input on, then STUB_PAD.
GZIP_STUB: Final = """#!/usr/bin/env bash
line="$(printf '%s\\037' gzip "$@")"
printf '%s\\n' "$line" >> "$STUB_LOG"
cat
printf '%s' "${STUB_PAD:-}"
"""

#: The `handover` command: it writes its words. `artifacts` prints the file
#: STUB_ROWS, and `disk-assets` the file STUB_DISKS and ends with
#: STUB_DISKS_STATUS. `check-tree` prints STUB_WALK_TEXT and ends with
#: STUB_WALK_STATUS.
CLI_STUB: Final = """#!/usr/bin/env bash
printf '%s\\037' handover "$PWD" "$@" >> "$STUB_LOG"
printf '\\n' >> "$STUB_LOG"
case "$1" in
  artifacts)
    cat "$STUB_ROWS"
    ;;
  disk-assets)
    cat "$STUB_DISKS"
    exit "${STUB_DISKS_STATUS:-0}"
    ;;
  check-tree)
    if [[ -n "${STUB_WALK_TEXT:-}" ]]; then
      echo "$STUB_WALK_TEXT"
    fi
    exit "${STUB_WALK_STATUS:-0}"
    ;;
esac
"""


def _stub(directory: Path, name: str, text: str) -> Path:
    directory.mkdir(exist_ok=True)
    stub = directory / name
    stub.write_text(text, encoding="utf-8")
    stub.chmod(STUB_MODE)

    return stub


def _row(**changed: str) -> str:
    """The real row of the crate, with each key of `changed` given another
    value. A key is the key of the line form with `_` for `-`."""
    lines = list(crate_row(COMPONENT, CRATE_DIR).lines())
    for key, value in changed.items():
        name = key.replace("_", "-")
        (index,) = [at for at, line in enumerate(lines) if line.startswith(f"{name} ")]
        lines[index] = f"{name} {value}"

    return "".join(f"{line}\n" for line in lines)


#: The disk-image row of each case: the component and the build of H4b.
DISK_COMPONENT: Final = "disks"
DISK_BUILD: Final = ("/usr/bin/env", "bash", "disks/bin/playpen-release-disks")


def _disk_row(component: str = DISK_COMPONENT) -> str:
    """The real line form of one disk-image row."""
    return "".join(f"{line}\n" for line in DiskRow(component, (DISK_BUILD,)).lines())


class Build:
    """One run of the script against the stand-ins. `rows` is the text that
    the `handover` command prints for `artifacts`, and `disks` the text that
    it prints for `disk-assets`."""

    def __init__(
        self, tmp_path: Path, rows: str, disks: str = "", /, **overrides: str | None
    ) -> None:
        stubs = tmp_path / "stubs"
        _stub(stubs, "rustup", RUSTUP_STUB)
        _stub(stubs, "cargo", CARGO_STUB)
        _stub(stubs, "tar", TAR_STUB)
        _stub(stubs, "gzip", GZIP_STUB)
        cli = _stub(stubs, "handover", CLI_STUB)

        self.workspace = tmp_path / "work-space"
        (self.workspace / "rust").mkdir(parents=True)
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.temp = tmp_path / "temp"
        self.temp.mkdir()
        self.out = tmp_path / "out"
        self.log = tmp_path / "calls.log"
        self.output = tmp_path / "output"
        self.output.touch()
        self.rows = tmp_path / "rows"
        self.rows.write_text(rows, encoding="utf-8")
        self.disks = tmp_path / "disks"
        self.disks.write_text(disks, encoding="utf-8")
        env: dict[str, str | None] = {
            "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.home),
            "GITHUB_WORKSPACE": str(self.workspace),
            "RUNNER_TEMP": str(self.temp),
            "GITHUB_OUTPUT": str(self.output),
            "ARTIFACT_REPO": THIS_REPOSITORY,
            "ARTIFACT_PLANNED": COMPONENT,
            "ARTIFACT_OUT": str(self.out),
            "RELEASE_CLI": str(cli),
            "STUB_LOG": str(self.log),
            "STUB_ROWS": str(self.rows),
            "STUB_DISKS": str(self.disks),
        }
        given = env | overrides
        # The run starts in its own directory: a relative path that the
        # script took would write there and not into the repository.
        self.done = subprocess.run(
            ["bash", str(SCRIPT)],
            cwd=tmp_path,
            env={name: value for name, value in given.items() if value is not None},
            capture_output=True,
            text=True,
            timeout=120,
        )

    @property
    def said(self) -> str:
        return self.done.stdout + self.done.stderr

    def calls(self, first: str) -> list[list[str]]:
        """The words of each logged call of `first`."""
        if not self.log.exists():
            return []

        lines = self.log.read_text(encoding="utf-8").splitlines()
        calls = [line.split(UNIT_SEPARATOR)[:-1] for line in lines]

        return [call for call in calls if call[0] == first]

    @property
    def digests(self) -> str:
        return (self.out / "SHA256SUMS").read_text(encoding="utf-8")

    @property
    def remap_flags(self) -> str:
        return (
            f"--remap-path-prefix={self.workspace}=/build/workspace "
            f"--remap-path-prefix={self.home}/.cargo=/build/cargo-home "
            f"--remap-path-prefix={self.temp}=/build/temp"
        )

    def tree(self) -> str:
        """The install root of the one cargo call."""
        (call,) = self.calls("cargo")
        (root,) = [word for word in call if word.startswith(f"{BINARY_INSTALL_ENV}=")]

        return root.split("=", 1)[1]


def test_no_row_builds_nothing_and_gives_an_empty_digest_list(tmp_path: Path) -> None:
    """The catalog of today holds no binary component. The job then prints
    one line and passes. Each upload holds `SHA256SUMS`, because the next
    job cannot download an artifact that does not exist."""
    run = Build(tmp_path, "", ARTIFACT_PLANNED="")

    assert run.done.returncode == 0, run.said
    assert run.done.stdout == f"{NO_ROW}\n"
    assert run.calls("handover") == [
        ["handover", str(run.workspace), "artifacts", "--repo", THIS_REPOSITORY]
    ]
    assert run.calls("rustup") == []
    assert run.calls("cargo") == []
    assert sorted(path.name for path in run.out.iterdir()) == ["SHA256SUMS"]
    assert run.digests == ""
    assert run.output.read_text(encoding="utf-8") == EMPTY_OUTPUT


@pytest.mark.parametrize(
    ("overrides", "fault"),
    [
        ({"ARTIFACT_REPO": ""}, "ARTIFACT_REPO is not set"),
        ({"ARTIFACT_OUT": ""}, "ARTIFACT_OUT is not set"),
        ({"RUNNER_TEMP": ""}, "RUNNER_TEMP is not set"),
        ({"GITHUB_OUTPUT": ""}, "GITHUB_OUTPUT is not set"),
    ],
    ids=["no-repository", "no-output", "no-temp", "no-step-output"],
)
def test_a_run_with_no_clear_input_stops_first(
    tmp_path: Path, overrides: dict[str, str], fault: str
) -> None:
    run = Build(tmp_path, _row(), **overrides)

    assert run.done.returncode == 1, run.said
    assert fault in run.done.stderr
    assert not run.log.exists()


@pytest.mark.parametrize(
    ("overrides", "variable"),
    [
        ({"ARTIFACT_OUT": "out"}, "ARTIFACT_OUT"),
        ({"GITHUB_WORKSPACE": "work-space"}, "GITHUB_WORKSPACE"),
        ({"RUNNER_TEMP": "temp"}, "RUNNER_TEMP"),
        ({"HOME": "home"}, "HOME"),
        ({"GITHUB_OUTPUT": "output"}, "GITHUB_OUTPUT"),
        ({"CARGO_HOME": "cargo-home"}, "CARGO_HOME"),
    ],
    ids=lambda one: one if isinstance(one, str) else None,
)
def test_a_relative_path_stops_the_run_first(
    tmp_path: Path, overrides: dict[str, str | None], variable: str
) -> None:
    """The script changes its directory to the workspace. A relative path
    would then name another file than the path of the caller."""
    run = Build(tmp_path, _row(), **overrides)

    assert run.done.returncode == 1, run.said
    assert run.done.stderr == f"build-artifacts: the variable {variable} is not an absolute path\n"
    assert not run.log.exists()
    assert not (run.out / "SHA256SUMS").exists()


def test_an_output_directory_that_holds_a_file_stops_the_run(tmp_path: Path) -> None:
    """The upload takes the whole directory. A file of another run would
    reach the next job."""
    out = tmp_path / "old-out"
    out.mkdir()
    (out / "left.tar.gz").write_bytes(b"")
    run = Build(tmp_path, _row(), ARTIFACT_OUT=str(out))

    assert run.done.returncode == 1, run.said
    assert "ARTIFACT_OUT is not empty" in run.done.stderr
    assert not run.log.exists()


@needs_sha256sum
def test_one_row_builds_walks_packs_and_writes_its_digest(tmp_path: Path) -> None:
    """The words and the variables of each call. The build runs in the
    repository root, and the toolchain comes from `rust/`."""
    run = Build(tmp_path, _row())
    tree = run.tree()
    work = str(Path(tree).parent)
    archive = run.out / ARCHIVE

    assert run.done.returncode == 0, run.said
    assert run.calls("rustup") == [
        ["rustup", str(run.workspace / "rust"), "toolchain", "install", "--no-self-update"],
        ["rustup", str(run.workspace / "rust"), "show", "active-toolchain"],
        [
            "rustup",
            str(run.workspace / "rust"),
            "target",
            "add",
            "--toolchain",
            TOOLCHAIN,
            BINARY_TARGET,
        ],
    ]
    assert run.calls("cargo") == [
        [
            "cargo",
            str(run.workspace),
            *INSTALL_WORDS[1:],
            f"{BINARY_INSTALL_ENV}={tree}",
            f"CARGO_BUILD_TARGET={BINARY_TARGET}",
            f"RUSTUP_TOOLCHAIN={TOOLCHAIN}",
            "CARGO_INCREMENTAL=0",
            f"RUSTFLAGS={run.remap_flags}",
            "CARGO_ENCODED_RUSTFLAGS=<unset>",
        ]
    ]
    assert Path(work).parent == run.temp
    assert Path(tree).name == COMPONENT
    assert run.calls("handover")[1:] == [
        [
            "handover",
            str(run.workspace),
            *("check-tree", "--kind", "binary", "--tree", tree),
            *("--needle", str(run.workspace)),
            *("--needle", str(run.home)),
            *("--needle", f"{run.home}/.cargo"),
            *("--needle", str(run.temp)),
        ]
    ]
    assert run.calls("tar") == [
        [
            "tar",
            str(run.workspace),
            *("--create", "--file", "-", "--directory", work),
            *("--sort=name", "--owner=0", "--group=0", "--numeric-owner"),
            *("--mtime=@0", "--mode=0755"),
            "--",
            f"{COMPONENT}/bin/{COMPONENT}",
        ]
    ]
    assert run.calls("gzip") == [["gzip", "--no-name"]]
    assert archive.read_bytes() == b"the members"

    line = f"{hashlib.sha256(b'the members').hexdigest()}  {ARCHIVE}"
    assert run.digests == f"{line}\n"
    assert (
        run.output.read_text(encoding="utf-8") == f"digests<<{OUTPUT_END}\n{line}\n{OUTPUT_END}\n"
    )
    assert sorted(path.name for path in run.out.iterdir()) == sorted([ARCHIVE, "SHA256SUMS"])
    assert not Path(work).exists(), "the work directory stays after the run"


@needs_sha256sum
def test_the_members_are_sorted_and_each_one_names_its_component(tmp_path: Path) -> None:
    run = Build(tmp_path, _row(), STUB_PROGRAMS="zeta alpha mid")
    (call,) = run.calls("tar")

    assert run.done.returncode == 0, run.said
    assert call[call.index("--") + 1 :] == [
        f"{COMPONENT}/bin/alpha",
        f"{COMPONENT}/bin/mid",
        f"{COMPONENT}/bin/zeta",
    ]


@needs_sha256sum
def test_no_word_of_a_command_passes_through_a_shell(tmp_path: Path) -> None:
    """A word with a quote, a `$(...)`, a `;` or a space reaches cargo as
    one word."""
    words = ["cargo", "install", "--path", "rust/crates/one two", "$(touch made)", "a;b", "'q'"]
    rows = _row().replace(
        "".join(f"{line}\n" for line in (f"argv {len(INSTALL_WORDS)}", *INSTALL_WORDS)),
        "".join(f"{line}\n" for line in (f"argv {len(words)}", *words)),
    )
    run = Build(tmp_path, rows)
    (call,) = run.calls("cargo")

    assert run.done.returncode == 0, run.said
    assert call[2 : 2 + len(words) - 1] == words[1:]
    assert not (run.workspace / "made").exists()


@needs_sha256sum
def test_two_rows_add_one_target_and_write_two_lines(tmp_path: Path) -> None:
    other = f"other-{BINARY_TARGET}{ARCHIVE_SUFFIX}"
    second = _row(component="other", archive=other, bundle=f"{other}{BUNDLE_SUFFIX}")
    run = Build(tmp_path, _row() + second, ARTIFACT_PLANNED=f"other {COMPONENT}")
    adds = [call for call in run.calls("rustup") if call[2:4] == ["target", "add"]]

    assert run.done.returncode == 0, run.said
    assert len(adds) == 1
    assert len(run.calls("cargo")) == 2
    assert [line.split("  ")[1] for line in run.digests.splitlines()] == [ARCHIVE, other]


@pytest.mark.parametrize("planned", [None, "", " "], ids=["unset", "empty", "a-space"])
def test_no_planned_row_builds_nothing(tmp_path: Path, planned: str | None) -> None:
    """The job `plan` names no binary component with a tag in this run. The
    script then reads the rows, builds nothing and writes an empty digest
    list, as with no row."""
    run = Build(tmp_path, _row(), ARTIFACT_PLANNED=planned)

    assert run.done.returncode == 0, run.said
    assert run.done.stdout == f"{NO_PLANNED_ROW}\n"
    assert len(run.calls("handover")) == 1
    assert run.calls("rustup") == []
    assert run.calls("cargo") == []
    assert sorted(path.name for path in run.out.iterdir()) == ["SHA256SUMS"]
    assert run.digests == ""
    assert run.output.read_text(encoding="utf-8") == EMPTY_OUTPUT


@needs_sha256sum
def test_only_the_planned_rows_are_built(tmp_path: Path) -> None:
    """Two rows, and the plan names one. Only its target is added, and only
    its archive is in the digest list."""
    other = f"other-{BINARY_TARGET}{ARCHIVE_SUFFIX}"
    second = _row(component="other", archive=other, bundle=f"{other}{BUNDLE_SUFFIX}")
    run = Build(tmp_path, _row() + second, ARTIFACT_PLANNED="other")
    adds = [call[-1] for call in run.calls("rustup") if call[2:4] == ["target", "add"]]

    assert run.done.returncode == 0, run.said
    assert Path(run.tree()).name == "other"
    assert adds == [BINARY_TARGET]
    assert [line.split("  ")[1] for line in run.digests.splitlines()] == [other]
    assert sorted(path.name for path in run.out.iterdir()) == sorted([other, "SHA256SUMS"])


@pytest.mark.parametrize("planned", ["vmm", f"{COMPONENT} vmm", "*", "agent-*"])
def test_a_planned_name_with_no_row_stops_the_run(tmp_path: Path, planned: str) -> None:
    """The plan and the catalog do not agree. A name is a word and no
    pattern: `*` names no row."""
    run = Build(tmp_path, _row(), ARTIFACT_PLANNED=planned)

    _refused(run)
    assert run.done.stderr.startswith("build-artifacts: the planned component ")
    assert run.done.stderr.endswith(" has no row\n")
    assert run.calls("rustup") == []


def test_a_planned_disk_row_is_skipped(tmp_path: Path) -> None:
    """The plan names the disk-image row alone. The script reads the names
    of `handover disk-assets`, says it skips the row, builds nothing and
    writes an empty digest list: another job builds the files of the row
    (decision CH76 rule 5)."""
    run = Build(tmp_path, _row(), _disk_row(), ARTIFACT_PLANNED=DISK_COMPONENT)

    assert run.done.returncode == 0, run.said
    assert run.done.stdout == (
        f"build-artifacts: {DISK_COMPONENT}: a disk-image row, which this script does not build\n"
        f"{NO_PLANNED_ROW}\n"
    )
    assert run.calls("handover") == [
        ["handover", str(run.workspace), "artifacts", "--repo", THIS_REPOSITORY],
        ["handover", str(run.workspace), "disk-assets", "--repo", THIS_REPOSITORY],
    ]
    assert run.calls("rustup") == []
    assert run.calls("cargo") == []
    assert run.digests == ""
    assert run.output.read_text(encoding="utf-8") == EMPTY_OUTPUT


@needs_sha256sum
def test_a_planned_disk_row_beside_a_binary_row_builds_the_binary_row(tmp_path: Path) -> None:
    """Only the archive of the binary row is in the digest list."""
    planned = f"{DISK_COMPONENT} {COMPONENT}"
    run = Build(tmp_path, _row(), _disk_row(), ARTIFACT_PLANNED=planned)

    assert run.done.returncode == 0, run.said
    assert len(run.calls("cargo")) == 1
    assert [line.split("  ")[1] for line in run.digests.splitlines()] == [ARCHIVE]
    assert len([call for call in run.calls("handover") if call[2] == "disk-assets"]) == 1


def test_a_plan_of_binary_rows_reads_no_disk_row(tmp_path: Path) -> None:
    """Each planned name has a binary row, so the script never asks for the
    disk-image rows."""
    run = Build(tmp_path, _row(), "not the line form\n")

    assert run.done.returncode == 0, run.said
    assert [call[2] for call in run.calls("handover")] == ["artifacts", "check-tree"]


@pytest.mark.parametrize(
    ("planned", "name"),
    [("vmm", "vmm"), (f"{DISK_COMPONENT} vmm", "vmm"), ("disk*", "disk*")],
)
def test_a_planned_name_with_no_row_still_stops(tmp_path: Path, planned: str, name: str) -> None:
    """A disk-image row skips its own name only. Each other planned name
    with no row stops the run, before any build. A name is a word and no
    pattern."""
    run = Build(tmp_path, _row(), _disk_row(), ARTIFACT_PLANNED=planned)

    _refused(run)
    assert run.done.stderr == f"build-artifacts: the planned component {name} has no row\n"
    assert run.calls("rustup") == []


@pytest.mark.parametrize(
    ("disks", "status", "fault"),
    [
        ("component disks\nend\n", "0", "a row has no key out-env"),
        (_disk_row() + _disk_row(), "0", "two disk rows name the component disks"),
        (_disk_row(), "1", "handover gave no disk rows"),
    ],
    ids=["not-the-line-form", "two-rows-of-one-name", "a-failed-answer"],
)
def test_a_disk_answer_that_does_not_read_stops_the_run(
    tmp_path: Path, disks: str, status: str, fault: str
) -> None:
    """The reader refuses the answer of `handover disk-assets` as it
    refuses a binary row: the run stops before any build."""
    run = Build(tmp_path, _row(), disks, ARTIFACT_PLANNED=DISK_COMPONENT, STUB_DISKS_STATUS=status)

    _refused(run)
    assert fault in run.done.stderr
    assert run.calls("rustup") == []


def test_the_build_takes_no_rustflags_of_its_caller(tmp_path: Path) -> None:
    """cargo reads CARGO_ENCODED_RUSTFLAGS before RUSTFLAGS. A value of the
    caller would replace the remap flags."""
    run = Build(tmp_path, _row(), RUSTFLAGS="--cfg other", CARGO_ENCODED_RUSTFLAGS="--cfg=other")
    (call,) = run.calls("cargo")

    assert call[-2:] == [f"RUSTFLAGS={run.remap_flags}", "CARGO_ENCODED_RUSTFLAGS=<unset>"]


def test_the_cargo_home_of_the_caller_is_a_remap_flag_and_a_needle(tmp_path: Path) -> None:
    cargo_home = tmp_path / "cargo-home"
    run = Build(tmp_path, _row(), CARGO_HOME=str(cargo_home))
    (cargo,) = run.calls("cargo")
    walk = run.calls("handover")[1]

    assert f"--remap-path-prefix={cargo_home}=/build/cargo-home " in cargo[-2]
    assert walk[walk.index(str(run.home)) + 2] == str(cargo_home)


def test_a_path_that_rustflags_cannot_carry_stops_the_build(tmp_path: Path) -> None:
    temp = tmp_path / "a temp"
    temp.mkdir()
    run = Build(tmp_path, _row(), RUNNER_TEMP=str(temp))

    assert run.done.returncode == 1, run.said
    assert "RUSTFLAGS cannot carry it" in run.done.stderr
    assert run.calls("cargo") == []


def _refused(run: Build) -> None:
    """A refused run writes no digest list and no step output."""
    assert run.done.returncode == 1, run.said
    assert not (run.out / "SHA256SUMS").exists()
    assert run.output.read_text(encoding="utf-8") == ""


def test_a_tree_with_no_program_stops_the_build(tmp_path: Path) -> None:
    run = Build(tmp_path, _row(), STUB_PROGRAMS="")

    _refused(run)
    assert "build-artifacts: agent-family: the build put no program in bin/" in run.done.stderr
    assert len(run.done.stderr.splitlines()) == 1
    assert run.calls("handover")[1:] == []


def test_a_tree_with_another_thing_beside_its_programs_stops_the_build(tmp_path: Path) -> None:
    """A record file of cargo means that the build lost `--no-track`. The
    archive holds programs only."""
    run = Build(tmp_path, _row(), STUB_RECORD="1")

    _refused(run)
    assert "the tree holds .crates.toml" in run.done.stderr


def test_a_file_in_bin_that_is_no_program_stops_the_build(tmp_path: Path) -> None:
    """The pack gives each member the mode 0755. A file that the build did
    not make a program must not become one there."""
    run = Build(tmp_path, _row(), STUB_PROGRAM_MODE="0644")

    _refused(run)
    assert "the tree holds bin/agent-family, which is no program" in run.done.stderr


@pytest.mark.parametrize("program", [".hidden", "-x"])
def test_a_program_with_a_name_that_is_not_plain_stops_the_build(
    tmp_path: Path, program: str
) -> None:
    """A member name goes onto the command line of tar. A name that starts
    with a period or with `-` is not the name of a program of a row."""
    run = Build(tmp_path, _row(), STUB_PROGRAMS=program)

    _refused(run)
    assert "the tree holds a program with a name that is not plain" in run.done.stderr
    assert run.calls("handover")[1:] == []


def test_a_directory_in_bin_stops_the_build(tmp_path: Path) -> None:
    """The archive holds programs directly in `bin/`. The sorted list of the
    tree names the directory before the file in it, so the directory is the
    refusal."""
    run = Build(tmp_path, _row(), STUB_PROGRAMS="sub/agent-family")

    _refused(run)
    assert "the tree holds bin/sub, which is no regular file" in run.done.stderr
    assert len(run.done.stderr.splitlines()) == 1
    assert run.calls("handover")[1:] == []


def test_a_build_command_that_fails_stops_the_build(tmp_path: Path) -> None:
    run = Build(tmp_path, _row(), STUB_CARGO_STATUS="101")

    _refused(run)
    assert "agent-family: a build command failed" in run.done.stderr
    assert run.calls("tar") == []


def test_a_walk_fault_stops_the_build_before_the_pack(tmp_path: Path) -> None:
    """The walk prints the fault. The archive of such a tree gets no
    signature."""
    run = Build(
        tmp_path,
        _row(),
        STUB_WALK_STATUS="1",
        STUB_WALK_TEXT="bin/agent-family holds a path of the build",
    )

    _refused(run)
    assert "bin/agent-family holds a path of the build" in run.done.stdout
    assert "agent-family: the walk found a fault in the tree" in run.done.stderr
    assert run.calls("tar") == []


def test_a_tree_with_more_programs_than_its_limit_stops_the_build(tmp_path: Path) -> None:
    run = Build(tmp_path, _row(max_members="1"), STUB_PROGRAMS="one two")

    _refused(run)
    assert "the tree has 2 programs, more than 1" in run.done.stderr
    assert run.calls("tar") == []


def test_a_tree_with_more_bytes_than_its_limit_stops_the_build(tmp_path: Path) -> None:
    run = Build(tmp_path, _row(max_unpacked_bytes="5"), STUB_BYTES="123456")

    _refused(run)
    assert "the tree has 6 bytes, more than 5" in run.done.stderr
    assert run.calls("tar") == []


def test_an_archive_with_more_bytes_than_its_limit_stops_the_build(tmp_path: Path) -> None:
    """The stand-in tar gives 11 bytes and gzip adds the pad. The archive
    is removed: no later step can upload it."""
    run = Build(tmp_path, _row(max_archive_bytes="12"), STUB_PAD="xx")

    _refused(run)
    assert "the archive has 13 bytes, more than 12" in run.done.stderr
    assert not (run.out / ARCHIVE).exists()


@needs_sha256sum
def test_an_archive_at_each_limit_passes(tmp_path: Path) -> None:
    run = Build(
        tmp_path,
        _row(max_members="2", max_unpacked_bytes="12", max_archive_bytes="11"),
        STUB_PROGRAMS="one two",
        STUB_BYTES="123456",
    )

    assert run.done.returncode == 0, run.said


def test_the_limits_of_a_row_are_the_constants_of_the_catalog() -> None:
    """The script reads each limit from the row. The rows of the tests are
    the real form, so each limit of a real row is the constant."""
    row = _row()

    assert f"max-archive-bytes {ARCHIVE_MAX_BYTES}\n" in row
    assert f"max-members {ARCHIVE_MAX_MEMBERS}\n" in row
    assert f"max-unpacked-bytes {ARCHIVE_MAX_UNPACKED_BYTES}\n" in row
    assert f"install-env {BINARY_INSTALL_ENV}\n" in row


#: Rows that are not in the line form, each with the reason of its refusal.
#: Each one stops the run before the first call of `rustup`.
#: `test_artifactrows.py` holds each reason of the reader. These cases show
#: that the run stops with that reason, and with the one check of the
#: script itself: an archive with the name of the digest file.
BROKEN_ROWS: Final = {
    "a-path-as-a-name": (
        _row(archive="../agent-family.tar.gz"),
        "the archive of a row is not a plain name",
    ),
    "no-end": (_row().removesuffix("end\n"), "the rows end inside a row"),
    "two-rows-with-one-component": (
        _row() + _row(archive="other.tar.gz", bundle="other.json"),
        f"two rows name the component {COMPONENT}",
    ),
    "the-name-of-the-digest-file": (
        _row(archive="SHA256SUMS"),
        f"the archive of {COMPONENT} has the name of the digest file",
    ),
}


@pytest.mark.parametrize(("rows", "fault"), BROKEN_ROWS.values(), ids=BROKEN_ROWS)
def test_a_row_that_is_not_in_the_line_form_stops_the_run(
    tmp_path: Path, rows: str, fault: str
) -> None:
    """The one line of the run is the reason of the reader. Another check
    that refuses the same text gives another line and fails the case."""
    run = Build(tmp_path, rows)

    _refused(run)
    assert run.done.stderr == f"build-artifacts: {fault}\n"
    assert run.calls("rustup") == []


@needs_sha256sum
def test_a_last_row_with_no_line_feed_is_a_whole_row(tmp_path: Path) -> None:
    """The reader reads the last line also when no line feed ends it."""
    run = Build(tmp_path, _row().removesuffix("\n"))

    assert run.done.returncode == 0, run.said
    assert len(run.calls("cargo")) == 1


def test_the_line_form_has_one_reader() -> None:
    """`bin/lib/artifactrows.sh` reads the rows for the build script, and
    the script sources it. No other file under `bin/` outside `tests/`
    defines `read_rows` or `read_disk_names`, or names the key
    `max-unpacked-bytes` or `out-env`. The test reads those texts only."""
    sourced = '. "$SELF_ROOT/bin/lib/artifactrows.sh"\n'
    others = [
        path
        for path in sorted((REPO / "bin").rglob("*"))
        if path.is_file() and "tests" not in path.relative_to(REPO / "bin").parts
        if path != READER and path.suffix != ".md"
    ]

    for user in READER_USERS:
        assert sourced in user.read_text(encoding="utf-8"), f"{user.name} reads no row"
    for path in others:
        text = path.read_text(encoding="utf-8", errors="replace")
        assert "read_rows()" not in text, f"{path.name} holds a second reader"
        assert "max-unpacked-bytes" not in text, f"{path.name} holds a key of the form"
        assert "read_disk_names()" not in text, f"{path.name} holds a second reader"
        assert "out-env" not in text, f"{path.name} holds a key of the disk form"
    assert "read_rows() {" in READER.read_text(encoding="utf-8")
    assert "read_disk_names() {" in READER.read_text(encoding="utf-8")


def test_a_command_that_gives_no_rows_stops_the_run(tmp_path: Path) -> None:
    run = Build(tmp_path, "", RELEASE_CLI="false")

    _refused(run)
    assert "handover gave no rows" in run.done.stderr


def test_the_script_is_a_ci_script_of_bash_3_2() -> None:
    """`bin/AGENTS.md`: the header names who runs it, with the exact
    command, in the first three lines."""
    head = SCRIPT.read_text(encoding="utf-8").splitlines()[:3]

    assert head[0] == "#!/usr/bin/env bash"
    assert head[1].startswith("# CI:")
    assert head[2].strip() == (
        "#   ARTIFACT_REPO=creche ARTIFACT_PLANNED=<names> ARTIFACT_OUT=<dir>"
        " bin/build-artifacts.sh"
    )
