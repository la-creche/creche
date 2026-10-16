"""What `bin/build-artifacts.sh` builds, walks, packs and refuses.

The job `artifacts` of `gate.yml` and of `release.yml` runs the script. The
job `sign` of `release.yml` signs each archive that it writes, and root on
the host later refuses an archive that is not in the form of its row. No
pull request runs `release.yml`, so these tests are the first run that the
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
   fault, a build fault, each of the three limits, and a row that is not in
   the line form.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Final

import pytest
from handover.artifacts import crate_row
from handover.catalog import (
    ARCHIVE_MAX_BYTES,
    ARCHIVE_MAX_MEMBERS,
    ARCHIVE_MAX_UNPACKED_BYTES,
    BINARY_INSTALL_ENV,
    BINARY_TARGET,
)

REPO: Final = Path(__file__).resolve().parents[2]
SCRIPT: Final = REPO / "bin" / "build-artifacts.sh"

#: The catalog name of the repository, as the two workflows give it.
THIS_REPOSITORY: Final = "agent-control"

#: The crate of the row of each case, and its build words.
COMPONENT: Final = "agent-family"
CRATE_DIR: Final = "rust/crates/agent-family"
INSTALL_WORDS: Final = ["cargo", "install", "--locked", "--no-track", "--path", CRATE_DIR]
ARCHIVE: Final = f"{COMPONENT}-{BINARY_TARGET}.tar.gz"

#: The toolchain that the stand-in `rustup` names.
TOOLCHAIN: Final = "1.92.0-x86_64-unknown-linux-gnu"

#: The separator of the words of one logged call.
UNIT_SEPARATOR: Final = "\x1f"

#: The first line that the script prints with no row.
NO_ROW: Final = "build-artifacts: no binary component"

#: The step output with no row, and its form with a text.
OUTPUT_END: Final = "END_OF_DIGESTS"
EMPTY_OUTPUT: Final = f"digests<<{OUTPUT_END}\n{OUTPUT_END}\n"

#: Whether this is a run of CI, as `bin/tests/test_artifact_probe_workflow.py`
#: reads it. A case that needs `sha256sum` skips on a development machine
#: without it, and fails in CI.
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
#: each with STUB_BYTES and the mode STUB_PROGRAM_MODE. STUB_RECORD leaves
#: the record file that cargo writes without `--no-track`. STUB_CARGO_STATUS
#: is its exit status.
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
#: STUB_ROWS. `check-tree` prints STUB_WALK_TEXT and ends with
#: STUB_WALK_STATUS.
CLI_STUB: Final = """#!/usr/bin/env bash
printf '%s\\037' handover "$PWD" "$@" >> "$STUB_LOG"
printf '\\n' >> "$STUB_LOG"
case "$1" in
  artifacts)
    cat "$STUB_ROWS"
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


class Build:
    """One run of the script against the stand-ins. `rows` is the text that
    the `handover` command prints for `artifacts`."""

    def __init__(self, tmp_path: Path, rows: str, **overrides: str | None) -> None:
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
        env: dict[str, str | None] = {
            "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.home),
            "GITHUB_WORKSPACE": str(self.workspace),
            "RUNNER_TEMP": str(self.temp),
            "GITHUB_OUTPUT": str(self.output),
            "ARTIFACT_REPO": THIS_REPOSITORY,
            "ARTIFACT_OUT": str(self.out),
            "RELEASE_CLI": str(cli),
            "STUB_LOG": str(self.log),
            "STUB_ROWS": str(self.rows),
        }
        given = env | overrides
        self.done = subprocess.run(
            ["bash", str(SCRIPT)],
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
    run = Build(tmp_path, "")

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


def test_the_rows_can_come_from_a_file(tmp_path: Path) -> None:
    """The probe and the process judge give one row of a crate in a file.
    The script then asks the command for no row."""
    rows = tmp_path / "given-rows"
    rows.write_text("", encoding="utf-8")
    run = Build(tmp_path, "", ARTIFACT_REPO=None, ARTIFACT_ROWS_FILE=str(rows))

    assert run.done.returncode == 0, run.said
    assert run.calls("handover") == []
    assert run.done.stdout == f"{NO_ROW}\n"


@pytest.mark.parametrize(
    ("overrides", "fault"),
    [
        ({"ARTIFACT_ROWS_FILE": "/dev/null"}, "not both"),
        ({"ARTIFACT_REPO": ""}, "ARTIFACT_REPO is not set"),
        ({"ARTIFACT_OUT": ""}, "ARTIFACT_OUT is not set"),
        ({"RUNNER_TEMP": ""}, "RUNNER_TEMP is not set"),
        ({"GITHUB_OUTPUT": ""}, "GITHUB_OUTPUT is not set"),
    ],
    ids=["two-sources", "no-source", "no-output", "no-temp", "no-step-output"],
)
def test_a_run_with_no_clear_input_stops_first(
    tmp_path: Path, overrides: dict[str, str], fault: str
) -> None:
    run = Build(tmp_path, _row(), **overrides)

    assert run.done.returncode == 1, run.said
    assert fault in run.done.stderr
    assert not run.log.exists()


def test_an_output_directory_that_holds_a_file_stops_the_run(tmp_path: Path) -> None:
    """The upload takes the whole directory. A file of another run would
    reach the job `sign`."""
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
    second = _row(component="other", archive=f"other-{BINARY_TARGET}.tar.gz")
    run = Build(tmp_path, _row() + second)
    adds = [call for call in run.calls("rustup") if call[2:4] == ["target", "add"]]

    assert run.done.returncode == 0, run.said
    assert len(adds) == 1
    assert len(run.calls("cargo")) == 2
    assert [line.split("  ")[1] for line in run.digests.splitlines()] == [
        ARCHIVE,
        f"other-{BINARY_TARGET}.tar.gz",
    ]


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


#: Rows that are not in the line form. Each one stops the run before the
#: first call of `rustup`.
BROKEN_ROWS: Final = {
    "a-key-out-of-order": _row().replace("component agent-family\ntarget", "target"),
    "a-path-as-a-name": _row(archive="../agent-family.tar.gz"),
    "an-option-as-a-name": _row(component="-rf"),
    "a-limit-of-zero": _row(max_members="0"),
    "a-limit-that-is-no-count": _row(max_archive_bytes="12k"),
    "no-command": re.sub(r"argv 6\n(.*\n){6}", "", _row()),
    "no-end": _row().removesuffix("end\n"),
    "a-short-command": _row().replace("argv 6\n", "argv 9\n"),
    "an-empty-first-word": _row().replace("argv 6\ncargo\n", "argv 6\n\n"),
    "a-variable-that-is-no-name": _row(install_env="CARGO INSTALL"),
    "two-rows-with-one-component": _row() + _row(archive="other.tar.gz"),
    "two-rows-with-one-archive": _row() + _row(component="other"),
    "the-name-of-the-digest-file": _row(archive="SHA256SUMS"),
    "a-blank-line": f"{_row()}\n",
}


@pytest.mark.parametrize("rows", BROKEN_ROWS.values(), ids=BROKEN_ROWS)
def test_a_row_that_is_not_in_the_line_form_stops_the_run(tmp_path: Path, rows: str) -> None:
    run = Build(tmp_path, rows)

    _refused(run)
    assert len(run.done.stderr.splitlines()) == 1
    assert run.calls("rustup") == []


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
    assert (
        head[2].strip()
        == "#   ARTIFACT_REPO=agent-control ARTIFACT_OUT=<dir> bin/build-artifacts.sh"
    )
