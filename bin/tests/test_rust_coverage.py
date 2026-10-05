"""The coverage rule of the Rust workspace, as `bin/rust-coverage.sh` holds it.

The script runs the tests of the workspace with `cargo llvm-cov` and checks
the JSON report against `rust/coverage-files.txt`. Seven things could go
wrong without one red line, and each gets a check here:

1. **A listed file with code that no test runs.** One region or one line is
   enough. The script compares counts, so a percent that rounds to 100 does
   not pass.
2. **A listed path that binds nothing.** A path with a typing error, or a
   file that no crate compiles, is in no report. The script fails for a
   listed path that the report does not hold.
3. **A listed file that takes code out of the measurement.** The text
   `coverage(off)` in a listed file fails, in each attribute that holds it.
4. **A file of the pure decision crate that the list does not name.** When
   `rust/crates/chaperone-policy/src` exists, each `.rs` file there must be
   in the list.
5. **A list that the script reads in part.** A line is a comment or one
   path. Each other line fails, and so does a path that the list holds two
   times.
6. **A report that the script reads in part.** A count that is no integer,
   a key that is absent and a file that the report holds two times each stop
   the run. No such report passes.
7. **A wrong cargo line.** The run is one cargo step inside `rust/`, word
   for word, and a step that fails stops the script before the check.

Everything runs the real script in a throwaway tree. No test needs cargo.
`--report` gives the script a report. Where a test covers the cargo step,
`cargo` is a fake that writes its argv to a file and copies a report to the
path that the script names. PATH holds only the fakes and links to the few
tools the script calls.
"""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

BIN = Path(__file__).resolve().parents[1]

#: The file under test, copied into the throwaway tree.
SCRIPT = "bin/rust-coverage.sh"

#: The list that the script reads.
LIST = "rust/coverage-files.txt"

#: Every program the script calls, but `cargo`. `cp` is for the fake cargo.
TOOLS = ("bash", "dirname", "mktemp", "rm", "python3", "cp")

#: The coverage run, word for word. The path of the report follows it.
COV_STEP = "llvm-cov --workspace --locked --json --no-cfg-coverage --no-cfg-coverage-nightly"
OUTPUT = "--output-path"

#: The workspace that the report names. The script takes the place of the
#: workspace from the report itself, so the report is the same on each
#: machine.
WORKSPACE = "/work/rust"

#: Two crates of the throwaway workspace, and the files of the first one.
FULL = "rust/crates/one/src/full.rs"
PART = "rust/crates/one/src/part.rs"
OTHER = "rust/crates/two/src/lib.rs"

#: The source directory of the pure decision crate, and two files in it.
PURE = "rust/crates/chaperone-policy/src"
PURE_FILE = f"{PURE}/gates.rs"
PURE_DEEP = f"{PURE}/verbs/schema.rs"

#: The segments of a file in which each region ran, as llvm-cov writes them:
#: line, column, count, has a count, starts a region, is a gap.
RAN = [
    [2, 1, 1, True, True, False],
    [2, 29, 0, False, False, False],
    [3, 5, 4, True, True, False],
    [4, 2, 0, False, False, False],
]

#: The same, with two regions that no test ran. Three more segments have the
#: count 0 and are no such region: one has no count, one starts no region and
#: one is a gap.
MISSED = [
    *RAN,
    [12, 5, 0, True, True, False],
    [12, 6, 0, False, True, False],
    [13, 1, 0, True, False, False],
    [14, 9, 0, True, True, True],
    [20, 14, 0, True, True, False],
]
MISSED_AT = "12:5 20:14"

#: Writes where it ran and its argv, then copies the report to the path that
#: follows `--output-path`. With CARGO_FAIL it exits 1 after the copy, so a
#: script that reads the report of a failed step shows.
FAKE_CARGO = """#!/usr/bin/env bash
printf '%s\\t%s\\n' "$PWD" "$*" >> "$CARGO_LOG"
while [[ $# -gt 1 ]]; do
  if [[ "$1" == "--output-path" ]]; then
    cp "$CARGO_REPORT" "$2"
  fi
  shift
done
if [[ -n "${CARGO_FAIL:-}" ]]; then
  exit 1
fi
"""

#: The script asks only whether this program is on PATH. cargo starts it, as
#: `cargo llvm-cov`, so a direct call fails the run.
FAKE_COV = """#!/usr/bin/env bash
exit 1
"""


def _counts(total: int, ran: int) -> dict[str, Any]:
    """One block of a summary. The percent is the rounded one of the tool:
    the script must not read it."""
    percent = round(100 * ran / total, 2) if total else 0.0

    return {"count": total, "covered": ran, "notcovered": total - ran, "percent": percent}


def _entry(
    name: str,
    *,
    regions: tuple[int, int] = (3, 3),
    lines: tuple[int, int] = (3, 3),
    branches: tuple[int, int] = (0, 0),
    segments: list[list[Any]] | None = None,
) -> dict[str, Any]:
    """One file of a report, by its path in the repository. Each pair is the
    count of the items and the count of the items that ran."""
    return {
        "filename": f"{WORKSPACE}/{name.removeprefix('rust/')}",
        "segments": RAN if segments is None else segments,
        "branches": [],
        "summary": {
            "regions": _counts(*regions),
            "lines": _counts(*lines),
            "branches": _counts(*branches),
            "functions": {"count": 1, "covered": 1, "percent": 100.0},
        },
    }


def _report(*entries: dict[str, Any]) -> dict[str, Any]:
    """A report in the form of `cargo llvm-cov --json`."""
    return {
        "data": [{"files": list(entries), "functions": [], "totals": {}}],
        "type": "llvm.coverage.json.export",
        "version": "3.0.1",
        "cargo_llvm_cov": {"version": "0.9.1", "manifest_path": f"{WORKSPACE}/Cargo.toml"},
    }


#: A report in which each region and each line of the three files ran.
ALL_RAN = _report(_entry(FULL), _entry(PART), _entry(OTHER))


@dataclass(frozen=True)
class Run:
    """One run of the script: its exit code, its output and what it called."""

    code: int
    out: str
    err: str
    cargo: list[str]
    cargo_dirs: set[str]

    @property
    def lines(self) -> list[str]:
        """The lines of the script on stderr, with no line of another
        program."""
        return [line for line in self.err.splitlines() if line.startswith("rust-coverage: ")]


@dataclass(frozen=True)
class Tree:
    """The throwaway tree, and the three PATHs a run can have."""

    root: Path
    tools: str
    with_cargo: str
    with_cov: str

    @property
    def scratch(self) -> Path:
        """Where the script makes its temporary directory."""
        return self.root.parent / "scratch"

    def write(self, name: str, body: str) -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    def names(self, *paths: str) -> None:
        """Writes the list: one comment, then one line for each path that the
        list names."""
        self.write(LIST, "".join(f"{line}\n" for line in ("# the list", *paths)))

    def _run(self, args: list[str], path: str, env: dict[str, str], cwd: Path) -> Run:
        log = self.root.parent / "cargo.log"
        log.unlink(missing_ok=True)
        done = subprocess.run(
            [str(self.root / SCRIPT), *args],
            env={"PATH": path, "TMPDIR": str(self.scratch), "CARGO_LOG": str(log)} | env,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=60,
        )
        calls = [line.split("\t") for line in _lines(log)]

        return Run(
            code=done.returncode,
            out=done.stdout,
            err=done.stderr,
            cargo=[argv for _where, argv in calls],
            cargo_dirs={where for where, _argv in calls},
        )

    def _saved(self, report: dict[str, Any] | str) -> Path:
        saved = self.root.parent / "report.json"
        text = report if isinstance(report, str) else json.dumps(report)
        saved.write_text(text, encoding="utf-8")

        return saved

    def check(self, report: dict[str, Any] | str, *flags: str) -> Run:
        """Checks `report` with `--report`. PATH holds no cargo."""
        args = [*flags, "--report", str(self._saved(report))]

        return self._run(args, self.tools, {}, self.root.parent)

    def cover(
        self, report: dict[str, Any], *flags: str, path: str | None = None, fail: bool = False
    ) -> Run:
        """Runs the script with no `--report`. The fake cargo writes `report`
        to the path that the script names. With `fail`, the fake exits 1."""
        env = {"CARGO_REPORT": str(self._saved(report))} | ({"CARGO_FAIL": "1"} if fail else {})

        return self._run([*flags], self.with_cov if path is None else path, env, self.root.parent)


def _lines(path: Path) -> list[str]:
    if not path.exists():
        return []

    return path.read_text(encoding="utf-8").splitlines()


def _fake(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def tree(tmp_path: Path) -> Tree:
    """A tree with the real script, an empty list and two crates. One PATH
    holds only the tools. A second one adds the fake `cargo`, and a third
    one adds the fake `cargo-llvm-cov` too."""
    root = tmp_path / "repo"
    tools = tmp_path / "tools"
    rusty = tmp_path / "rusty"
    covering = tmp_path / "covering"
    for one in (root, tools, rusty, covering, tmp_path / "scratch"):
        one.mkdir()

    for name in TOOLS:
        found = shutil.which(name)
        assert found is not None, f"{name} is not on PATH"
        (tools / name).symlink_to(found)

    _fake(rusty / "cargo", FAKE_CARGO)
    _fake(covering / "cargo-llvm-cov", FAKE_COV)

    made = Tree(
        root=root,
        tools=str(tools),
        with_cargo=f"{rusty}:{tools}",
        with_cov=f"{covering}:{rusty}:{tools}",
    )
    assert shutil.which("cargo", path=made.tools) is None
    assert shutil.which("cargo-llvm-cov", path=made.with_cargo) is None

    (root / SCRIPT).parent.mkdir(parents=True)
    shutil.copy2(BIN.parent / SCRIPT, root / SCRIPT)

    made.names()
    for crate in ("one", "two"):
        made.write(f"rust/crates/{crate}/Cargo.toml", f'[package]\nname = "{crate}"\n')
    for name in (FULL, PART, OTHER):
        made.write(name, "pub fn double(n: u32) -> u32 {\n    n * 2\n}\n")

    return made


def _passed(done: Run) -> bool:
    return done.code == 0 and done.out.endswith("rust-coverage: PASS\n") and done.err == ""


def _failed(done: Run, *lines: str) -> bool:
    """Whether the run failed with exactly `lines` and the count of them."""
    said = [f"rust-coverage: {line}" for line in (*lines, f"FAIL ({len(lines)})")]

    return done.code == 1 and "PASS" not in done.out and done.lines == said


# --- the counts of each crate -------------------------------------------------


def test_an_empty_list_passes_with_the_counts_of_each_crate(tree: Tree) -> None:
    report = _report(
        _entry(FULL, regions=(7, 7), lines=(6, 6)),
        _entry(PART, regions=(25, 17), lines=(22, 15)),
        _entry(OTHER, regions=(3, 0), lines=(3, 0)),
    )

    done = tree.check(report)

    assert _passed(done), done.out + done.err
    assert done.out.splitlines() == [
        "rust-coverage: crate one: tests ran 24 of 32 regions, 21 of 28 lines",
        "rust-coverage: crate two: tests ran 0 of 3 regions, 0 of 3 lines",
        "rust-coverage: the workspace: tests ran 24 of 35 regions, 21 of 31 lines",
        f"rust-coverage: paths in {LIST}: 0",
        "rust-coverage: PASS",
    ]


def test_a_crate_that_the_report_does_not_hold_has_a_row(tree: Tree) -> None:
    """A crate of types only has no function, so a report holds no file of
    it. Its row shows that the run measured nothing there."""
    done = tree.check(_report(_entry(FULL)))

    assert _passed(done), done.out + done.err
    assert "rust-coverage: crate two: tests ran 0 of 0 regions, 0 of 0 lines" in done.out


def test_a_file_outside_the_crates_is_in_the_sum(tree: Tree) -> None:
    outside = _entry("rust/build.rs") | {"filename": "/elsewhere/build.rs"}

    done = tree.check(_report(_entry(FULL), outside))

    assert _passed(done), done.out + done.err
    assert "rust-coverage: crate (no crate): tests ran 3 of 3 regions, 3 of 3 lines" in done.out
    assert "rust-coverage: the workspace: tests ran 6 of 6 regions, 6 of 6 lines" in done.out


# --- rule 1: no region and no line that no test runs --------------------------


def test_a_listed_file_that_ran_in_full_passes(tree: Tree) -> None:
    tree.names(FULL, PART)

    done = tree.check(ALL_RAN)

    assert _passed(done), done.out + done.err
    assert f"rust-coverage: paths in {LIST}: 2" in done.out


def test_a_region_that_no_test_runs_fails(tree: Tree) -> None:
    tree.names(FULL, PART)
    report = _report(_entry(FULL), _entry(PART, regions=(25, 23), segments=MISSED))

    done = tree.check(report)

    assert _failed(done, f"{PART}: no test ran 2 of 25 regions. They start at {MISSED_AT}"), (
        done.out + done.err
    )


def test_a_line_that_no_test_runs_fails(tree: Tree) -> None:
    tree.names(PART)

    done = tree.check(_report(_entry(PART, lines=(22, 21))))

    assert _failed(done, f"{PART}: no test ran 1 of 22 lines"), done.out + done.err


def test_a_missed_region_fails_when_its_percent_rounds_to_100(tree: Tree) -> None:
    """The script reads the two counts. The percent of this file is 100.0
    after the rounding, with one region of 100,000 that no test ran."""
    tree.names(PART)
    entry = _entry(PART, regions=(100_000, 99_999), segments=MISSED)
    assert entry["summary"]["regions"]["percent"] == 100.0

    done = tree.check(_report(entry))

    assert _failed(done, f"{PART}: no test ran 1 of 100000 regions. They start at {MISSED_AT}"), (
        done.out + done.err
    )


def test_a_failure_line_names_twenty_places_at_most(tree: Tree) -> None:
    tree.names(PART)
    segments = [[line, 1, 0, True, True, False] for line in range(1, 24)]
    places = " ".join(f"{line}:1" for line in range(1, 21))

    done = tree.check(_report(_entry(PART, regions=(23, 0), segments=segments)))

    assert _failed(
        done, f"{PART}: no test ran 23 of 23 regions. They start at {places} and 3 more"
    ), done.out + done.err


def test_each_listed_file_that_fails_has_its_line(tree: Tree) -> None:
    tree.names(FULL, PART)
    report = _report(
        _entry(FULL, lines=(6, 4)),
        _entry(PART, regions=(25, 23), lines=(22, 20), segments=MISSED),
    )

    done = tree.check(report)

    assert _failed(
        done,
        f"{FULL}: no test ran 2 of 6 lines",
        f"{PART}: no test ran 2 of 25 regions. They start at {MISSED_AT}",
        f"{PART}: no test ran 2 of 22 lines",
    ), done.out + done.err


def test_a_file_outside_the_list_has_no_threshold(tree: Tree) -> None:
    tree.names(FULL)
    report = _report(_entry(FULL), _entry(PART, regions=(25, 0), lines=(22, 0)))

    done = tree.check(report)

    assert _passed(done), done.out + done.err


# --- rule 2: the report holds each listed file --------------------------------


def test_a_listed_file_that_the_report_does_not_hold_fails(tree: Tree) -> None:
    tree.names(FULL, PART)

    done = tree.check(_report(_entry(FULL)))

    assert _failed(
        done,
        f"{PART}: the report does not hold this file. A report holds a file only "
        "when the test build compiles a function of it",
    ), done.out + done.err


def test_a_listed_path_that_is_no_file_fails(tree: Tree) -> None:
    absent = "rust/crates/one/src/absent.rs"
    tree.names(absent)

    done = tree.check(ALL_RAN)

    assert _failed(done, f"{absent}: the list names this path, and it is not a file"), (
        done.out + done.err
    )


# --- rule 3: no listed file turns the measurement off -------------------------


@pytest.mark.parametrize(
    "attribute",
    ["#[coverage(off)]", "#![coverage(off)]", "#[cfg_attr(coverage, coverage(off))]"],
)
def test_a_listed_file_that_turns_the_measurement_off_fails(tree: Tree, attribute: str) -> None:
    tree.names(FULL)
    tree.write(FULL, f"{attribute}\npub fn double(n: u32) -> u32 {{\n    n * 2\n}}\n")

    done = tree.check(ALL_RAN)

    assert _failed(done, f"{FULL}: the file holds the text `coverage(off)`"), done.out + done.err


def test_a_file_outside_the_list_can_hold_the_text(tree: Tree) -> None:
    tree.names(FULL)
    tree.write(PART, "#[coverage(off)]\npub fn skipped() {}\n")

    done = tree.check(ALL_RAN)

    assert _passed(done), done.out + done.err


# --- rule 4: the list names each file of the pure decision crate --------------


def test_a_file_of_the_pure_crate_outside_the_list_fails(tree: Tree) -> None:
    for name in (PURE_FILE, PURE_DEEP):
        tree.write(name, "pub fn decide() {}\n")
    tree.write(f"{PURE}/notes.txt", "no Rust file\n")

    done = tree.check(ALL_RAN)

    assert _failed(
        done,
        f"{PURE_FILE}: the list does not name this file of {PURE}",
        f"{PURE_DEEP}: the list does not name this file of {PURE}",
    ), done.out + done.err


def test_the_pure_crate_passes_when_the_list_names_each_file(tree: Tree) -> None:
    for name in (PURE_FILE, PURE_DEEP):
        tree.write(name, "pub fn decide() {}\n")
    tree.names(PURE_FILE, PURE_DEEP)

    done = tree.check(_report(_entry(PURE_FILE), _entry(PURE_DEEP)))

    assert _passed(done), done.out + done.err


# --- the list -----------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "",
        " ",
        f" {FULL}",
        f"{FULL} ",
        f"{FULL}\r",
        f"{FULL} # the first file",
        f"/work/{FULL}",
        "crates/one/src/full.rs",
        "rust/crates/one/src/../src/full.rs",
        "rust/crates/one/src/./full.rs",
        "rust/crates/one//src/full.rs",
        "rust/crates/one/src/",
        "rust/crates/one/src/full.py",
        "rust/crates/one/src/full.rs.bak",
        "rust/.rs",
        "rustic/crates/one/src/full.rs",
        "rust/crates/one/src/füll.rs",
    ],
    ids=repr,
)
def test_a_line_that_is_no_comment_and_no_path_fails(tree: Tree, line: str) -> None:
    tree.write(LIST, f"# the list\n{line}\n")

    done = tree.check(ALL_RAN)

    assert _failed(done, f"{LIST}:2: not a comment and not the path of a `.rs` file under rust/"), (
        done.out + done.err
    )


def test_a_path_that_the_list_holds_two_times_fails(tree: Tree) -> None:
    tree.names(FULL, PART, FULL)

    done = tree.check(ALL_RAN)

    assert _failed(done, f"{LIST}:4: the list names {FULL} two times"), done.out + done.err


def test_a_last_line_with_no_newline_fails(tree: Tree) -> None:
    tree.write(LIST, f"# the list\n{FULL}")

    done = tree.check(ALL_RAN)

    assert _failed(done, f"{LIST}: no newline ends the last line"), done.out + done.err


def test_a_list_of_no_line_names_no_path(tree: Tree) -> None:
    tree.write(LIST, "")

    done = tree.check(ALL_RAN)

    assert _passed(done), done.out + done.err


def test_a_tree_with_no_list_fails(tree: Tree) -> None:
    (tree.root / LIST).unlink()

    done = tree.check(ALL_RAN)

    assert done.code == 1, done.out + done.err
    assert done.out == ""
    assert len(done.lines) == 1
    assert done.lines[0].startswith(f"rust-coverage: cannot read {LIST}: ")


# --- the branches -------------------------------------------------------------

#: A report with one side of one branch that no test took.
ONE_SIDE = _report(_entry(FULL, branches=(4, 4)), _entry(PART, branches=(4, 3)))


def test_the_branch_flag_fails_a_side_that_no_test_took(tree: Tree) -> None:
    tree.names(FULL, PART)

    done = tree.check(ONE_SIDE, "--branch")

    assert _failed(done, f"{PART}: no test took 1 of 4 sides of a branch"), done.out + done.err
    assert (
        "rust-coverage: crate one: tests ran 6 of 6 regions, 6 of 6 lines, 7 of 8 sides of a branch"
    ) in done.out


def test_a_run_with_no_branch_flag_reads_no_branch(tree: Tree) -> None:
    tree.names(FULL, PART)

    done = tree.check(ONE_SIDE)

    assert _passed(done), done.out + done.err
    assert "branch" not in done.out


# --- a report that is not in the form of the tool -----------------------------


def _changed(change: Any) -> dict[str, Any]:
    """`ALL_RAN` with one change. `change` gets the report and its first
    file."""
    report = copy.deepcopy(ALL_RAN)
    change(report, report["data"][0]["files"][0])

    return report


def _set(holder: dict[str, Any], key: str, value: Any) -> None:
    holder[key] = value


#: Each report here differs from `ALL_RAN` in one place.
BAD_REPORTS: dict[str, dict[str, Any] | str] = {
    "not JSON": "{",
    "not an object": "[]",
    "a number that is no JSON": json.dumps(ALL_RAN).replace("100.0", "NaN", 1),
    "a key two times": json.dumps(ALL_RAN).replace('"count": 3', '"count": 3, "count": 3', 1),
    "no workspace": _changed(lambda report, _file: report.pop("cargo_llvm_cov")),
    "a workspace with no full path": _changed(
        lambda report, _file: _set(report["cargo_llvm_cov"], "manifest_path", "rust/Cargo.toml")
    ),
    "a workspace file of another name": _changed(
        lambda report, _file: _set(report["cargo_llvm_cov"], "manifest_path", f"{WORKSPACE}/x.toml")
    ),
    "no data": _changed(lambda report, _file: report.pop("data")),
    "data of no entry": _changed(lambda report, _file: _set(report, "data", [])),
    "data of two entries": _changed(lambda report, _file: report["data"].append(report["data"][0])),
    "no files": _changed(lambda report, _file: report["data"][0].pop("files")),
    "files that are no list": _changed(lambda report, _file: _set(report["data"][0], "files", {})),
    "a file that is no object": _changed(
        lambda report, _file: report["data"][0]["files"].append(7)
    ),
    "a file two times": _changed(lambda report, file: report["data"][0]["files"].append(file)),
    "no filename": _changed(lambda _report, file: file.pop("filename")),
    "a filename that is no text": _changed(lambda _report, file: _set(file, "filename", 7)),
    "no summary": _changed(lambda _report, file: file.pop("summary")),
    "no regions": _changed(lambda _report, file: file["summary"].pop("regions")),
    "no lines": _changed(lambda _report, file: file["summary"].pop("lines")),
    "no branches": _changed(lambda _report, file: file["summary"].pop("branches")),
    "no count": _changed(lambda _report, file: file["summary"]["regions"].pop("count")),
    "no covered": _changed(lambda _report, file: file["summary"]["lines"].pop("covered")),
    "a count with a fraction": _changed(
        lambda _report, file: _set(file["summary"]["regions"], "count", 3.0)
    ),
    "a count that is a flag": _changed(
        lambda _report, file: _set(file["summary"]["regions"], "covered", True)
    ),
    "a count that is a text": _changed(
        lambda _report, file: _set(file["summary"]["lines"], "count", "3")
    ),
    "a count below zero": _changed(
        lambda _report, file: _set(file["summary"]["lines"], "covered", -1)
    ),
    "more that ran than exist": _changed(
        lambda _report, file: _set(file["summary"]["regions"], "covered", 4)
    ),
}


@pytest.mark.parametrize("name", BAD_REPORTS)
def test_a_report_in_another_form_stops_the_run(tree: Tree, name: str) -> None:
    """The first file of each report is the listed one. A reader that takes
    an absent count as zero would pass the file."""
    tree.names(FULL)

    done = tree.check(BAD_REPORTS[name])

    assert done.code == 1, done.out + done.err
    assert "PASS" not in done.out
    assert len(done.lines) == 1, done.err
    assert "Traceback" not in done.err


@pytest.mark.parametrize(
    "segments",
    [
        {},
        [7],
        [[12, 5, 0, True, True]],
        [[12, 5, 0, True, True, False, 0]],
        [[12, 5, 0, 1, True, False]],
        [[12, 5, False, True, True, False]],
        [["12", 5, 0, True, True, False]],
    ],
    ids=repr,
)
def test_segments_in_another_form_stop_the_run(tree: Tree, segments: Any) -> None:
    """Only a failure line reads the segments. The run still fails, and it
    names no place that it did not read."""
    tree.names(PART)

    done = tree.check(_report(_entry(PART, regions=(25, 23), segments=segments)))

    assert done.code == 1, done.out + done.err
    assert len(done.lines) == 1, done.err
    assert "They start at" not in done.err
    assert "Traceback" not in done.err


# --- the cargo step -----------------------------------------------------------


def test_the_coverage_run_is_one_cargo_step_inside_rust(tree: Tree) -> None:
    tree.names(FULL)

    done = tree.cover(ALL_RAN)

    assert _passed(done), done.out + done.err
    (step,) = done.cargo
    words, report = step.rsplit(" ", 1)
    assert words == f"{COV_STEP} {OUTPUT}"
    assert Path(report).is_relative_to(tree.scratch)
    assert done.cargo_dirs == {str(tree.root / "rust")}


def test_the_branch_flag_reaches_the_cargo_step(tree: Tree) -> None:
    tree.names(FULL, PART)

    done = tree.cover(ONE_SIDE, "--branch")

    assert _failed(done, f"{PART}: no test took 1 of 4 sides of a branch"), done.out + done.err
    (step,) = done.cargo
    assert step.rsplit(" ", 1)[0] == f"{COV_STEP} --branch {OUTPUT}"


def test_the_check_reads_the_report_of_its_own_run(tree: Tree) -> None:
    tree.names(PART)

    done = tree.cover(_report(_entry(PART, lines=(22, 21))))

    assert _failed(done, f"{PART}: no test ran 1 of 22 lines"), done.out + done.err


@pytest.mark.parametrize("fail", [False, True], ids=["a run that passes", "a run that fails"])
def test_a_run_leaves_no_report_behind(tree: Tree, fail: bool) -> None:
    done = tree.cover(ALL_RAN, fail=fail)

    assert done.cargo, done.out + done.err
    assert list(tree.scratch.iterdir()) == []


def test_a_cargo_step_that_fails_stops_the_script_before_the_check(tree: Tree) -> None:
    """A test that fails under coverage fails the cargo step. No count of a
    run with a failed test is a result, so the script reads no report then."""
    done = tree.cover(ALL_RAN, fail=True)

    assert done.code == 1, done.out + done.err
    assert done.out == ""
    assert done.err == ""
    assert len(done.cargo) == 1


@pytest.mark.parametrize(
    ("path", "program"),
    [("tools", "cargo"), ("with_cargo", "cargo-llvm-cov")],
    ids=["no cargo", "no cargo-llvm-cov"],
)
def test_a_run_with_no_program_for_the_step_fails_closed(
    tree: Tree, path: str, program: str
) -> None:
    done = tree.cover(ALL_RAN, path=getattr(tree, path))

    assert done.code == 1, done.out + done.err
    assert done.out == ""
    assert done.err == f"rust-coverage: {program} not on PATH: the coverage run needs it\n"
    assert done.cargo == []


def test_a_run_with_no_python_fails_closed(tree: Tree, tmp_path: Path) -> None:
    """The check of the report is a Python program. With no `python3`, the
    script stops before the cargo step: a coverage run that no check reads
    is no result."""
    bare = tmp_path / "bare"
    bare.mkdir()
    for name in TOOLS:
        if name != "python3":
            (bare / name).symlink_to(Path(tree.tools) / name)

    done = tree.cover(ALL_RAN, path=f"{tmp_path / 'covering'}:{tmp_path / 'rusty'}:{bare}")

    assert done.code == 1, done.out + done.err
    assert done.out == ""
    assert done.err == "rust-coverage: python3 not on PATH: the check of the report needs it\n"
    assert done.cargo == []


def test_a_report_of_an_earlier_run_starts_no_cargo(tree: Tree) -> None:
    saved = tree.root.parent / "report.json"
    saved.write_text(json.dumps(ALL_RAN), encoding="utf-8")

    done = tree._run(["--report", "report.json"], tree.with_cov, {}, saved.parent)

    assert _passed(done), done.out + done.err
    assert done.cargo == []


# --- the command line ---------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        ["--tests"],
        ["report.json"],
        ["--report"],
        ["--report", ""],
        ["--report", "one.json", "--report", "two.json"],
        ["--branch", "--branch"],
        ["--branch", "--bench"],
    ],
    ids=" ".join,
)
def test_a_command_line_in_another_form_is_a_usage_error(tree: Tree, args: list[str]) -> None:
    done = tree._run(args, tree.with_cov, {}, tree.root.parent)

    assert done.code == 2, done.out + done.err
    assert done.out == ""
    assert done.err == "usage: bin/rust-coverage.sh [--branch] [--report FILE]\n"
    assert done.cargo == []
