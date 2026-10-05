"""The coverage rule of the Rust workspace, as `bin/rust-coverage.sh` holds it.

The script runs the tests of the workspace with `cargo llvm-cov` and checks
the JSON report against `rust/coverage-files.txt`. Nine things could go
wrong without one red line, and each gets a check here:

1. **A listed file with code that no test runs.** One region or one line is
   enough. The script compares counts, so a percent that rounds to 100 does
   not pass. It takes the counts from the segments of the report, where the
   count of a region is the sum of each copy of its code. The summary of the
   tool takes the best copy only, and the script does not read it.
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
6. **A report that the script reads in part.** A segment in another form,
   a key that is absent and a file that the report holds two times each stop
   the run. No such report passes.
7. **A wrong cargo line.** The run is one cargo step inside `rust/`, word
   for word, and a step that fails stops the script before the check.
8. **A check that the caller can change.** The check of the report is a
   Python program inside the script. It imports no module from the
   directory of the caller and none from `PYTHONPATH`.
9. **A Python program that no check reads.** ruff reads no `.sh` file, so a
   test here gives the program to ruff.

Everything runs the real script in a throwaway tree. No test needs cargo.
`--report` gives the script a report. Where a test covers the cargo step,
`cargo` is a fake that writes its argv to a file and copies a report to the
path that the script names. PATH holds only the fakes and links to the few
tools the script calls.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

BIN = Path(__file__).resolve().parents[1]

#: The file under test, copied into the throwaway tree, and the library that
#: it sources for the name of the Rust directory.
SCRIPT = "bin/rust-coverage.sh"
RULE = "bin/lib/rustrule.sh"

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
#: line, column, count, has a count, starts a region, is a gap. The file has
#: two regions and three lines with code.
RAN = [
    [2, 1, 1, True, True, False],
    [2, 29, 0, False, False, False],
    [3, 5, 4, True, True, False],
    [4, 2, 0, False, False, False],
]
RAN_ROW = "2 of 2 regions, 3 of 3 lines"

#: The same, with two regions that no test ran. Three more segments have the
#: count 0 and are no such region: one has no count, one starts no region and
#: one is a gap. The file then has 4 regions and 11 lines with code.
MISSED = [
    *RAN,
    [12, 5, 0, True, True, False],
    [12, 6, 0, False, True, False],
    [13, 1, 0, True, False, False],
    [14, 9, 0, True, True, True],
    [20, 14, 0, True, True, False],
]
MISSED_REGIONS = "no test ran 2 of 4 regions. They start at 12:5, 20:14"
MISSED_LINES = "no test ran 8 of 11 lines. They are 12, 14-20"

#: What the tool writes as the summary of a file. It takes the best copy of
#: each function, so it can say that a region did not run although another
#: copy ran it. The script must not read it. In each report of this file it
#: says that one region and one line did not run.
SUMMARY = {
    "regions": {"count": 9, "covered": 8, "notcovered": 1, "percent": 88.89},
    "lines": {"count": 8, "covered": 7, "percent": 87.5},
    "branches": {"count": 0, "covered": 0, "notcovered": 0, "percent": 0},
    "functions": {"count": 2, "covered": 2, "percent": 100},
}

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

#: A module with the name of one that the check imports. It ends the program
#: with the status 0 and no output, so a check that imports it passes each
#: report.
HOSTILE = "import sys\n\nsys.exit(0)\n"


def _code(ran: int, missed: int = 0) -> list[list[Any]]:
    """The segments of a file with one region on each line: `ran` lines that
    ran, then `missed` lines that no test ran."""
    made: list[list[Any]] = []
    for line in range(1, ran + missed + 1):
        made.append([line, 1, 1 if line <= ran else 0, True, True, False])
        made.append([line, 9, 0, False, False, False])

    return made


def _branch(line: int, column: int, when_true: int, when_false: int) -> list[int]:
    """One branch, as llvm-cov writes it: where it starts and ends, the count
    of each side, two file numbers and the kind."""
    return [line, column, line, column + 6, when_true, when_false, 0, 0, 4]


def _entry(
    name: str,
    *,
    segments: list[list[Any]] | None = None,
    branches: list[list[int]] | None = None,
) -> dict[str, Any]:
    """One file of a report, by its path in the repository."""
    return {
        "filename": f"{WORKSPACE}/{name.removeprefix('rust/')}",
        "segments": RAN if segments is None else segments,
        "branches": [] if branches is None else branches,
        "expansions": [],
        "mcdc_records": [],
        "summary": SUMMARY,
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

    def run(self, args: list[str], path: str, env: dict[str, str], cwd: Path) -> Run:
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

    def check(
        self,
        report: dict[str, Any] | str,
        *flags: str,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
    ) -> Run:
        """Checks `report` with `--report`. PATH holds no cargo. `env` adds
        variables, and `cwd` is the directory of the caller."""
        args = [*flags, "--report", str(self._saved(report))]

        return self.run(args, self.tools, env or {}, cwd or self.root.parent)

    def cover(
        self, report: dict[str, Any], *flags: str, path: str | None = None, fail: bool = False
    ) -> Run:
        """Runs the script with no `--report`. The fake cargo writes `report`
        to the path that the script names. With `fail`, the fake exits 1."""
        env = {"CARGO_REPORT": str(self._saved(report))} | ({"CARGO_FAIL": "1"} if fail else {})

        return self.run([*flags], self.with_cov if path is None else path, env, self.root.parent)


def _lines(path: Path) -> list[str]:
    if not path.exists():
        return []

    return path.read_text(encoding="utf-8").splitlines()


def _fake(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def tree(tmp_path: Path) -> Tree:
    """A tree with the real script, its library, an empty list and two
    crates. One PATH holds only the tools. A second one adds the fake
    `cargo`, and a third one adds the fake `cargo-llvm-cov` too."""
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

    for name in (SCRIPT, RULE):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(BIN.parent / name, root / name)

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


def _stopped(done: Run, start: str) -> bool:
    """Whether the run stopped before its verdict, with one line that starts
    with `start`."""
    return (
        done.code == 1
        and "PASS" not in done.out
        and "Traceback" not in done.err
        and len(done.lines) == 1
        and done.lines[0].startswith(f"rust-coverage: {start}")
    )


# --- the counts of each crate -------------------------------------------------


def test_an_empty_list_passes_with_the_counts_of_each_crate(tree: Tree) -> None:
    """`RAN` has 2 regions and 3 lines, so a script that takes one count for
    the other shows."""
    report = _report(
        _entry(FULL),
        _entry(PART, segments=_code(17, 8)),
        _entry(OTHER, segments=_code(0, 3)),
    )

    done = tree.check(report)

    assert _passed(done), done.out + done.err
    assert done.out.splitlines() == [
        "rust-coverage: crate one: tests ran 19 of 27 regions, 20 of 28 lines",
        "rust-coverage: crate two: tests ran 0 of 3 regions, 0 of 3 lines",
        "rust-coverage: the workspace: tests ran 19 of 30 regions, 20 of 31 lines",
        f"rust-coverage: paths in {LIST}: 0",
        "rust-coverage: PASS",
    ]


def test_a_crate_that_the_report_does_not_hold_has_a_row(tree: Tree) -> None:
    """A crate of types only has no function, so a report holds no file of
    it. Its row shows that the run measured nothing there."""
    tree.write("rust/crates/notes/readme.txt", "a directory with no Cargo.toml is no crate\n")

    done = tree.check(_report(_entry(FULL)))

    assert _passed(done), done.out + done.err
    assert "rust-coverage: crate two: tests ran 0 of 0 regions, 0 of 0 lines" in done.out
    assert "notes" not in done.out


def test_a_file_outside_the_crates_is_in_the_sum(tree: Tree) -> None:
    outside = _entry("rust/build.rs") | {"filename": "/elsewhere/build.rs"}

    done = tree.check(_report(_entry(FULL), outside))

    assert _passed(done), done.out + done.err
    assert f"rust-coverage: crate (no crate): tests ran {RAN_ROW}" in done.out
    assert "rust-coverage: the workspace: tests ran 4 of 4 regions, 6 of 6 lines" in done.out


# --- rule 1: no region and no line that no test runs --------------------------


def test_a_listed_file_that_ran_in_full_passes(tree: Tree) -> None:
    """The summary of each file says that one region and one line did not
    run. The segments say that each one ran, in one copy of the code or in
    another. The script reads the segments."""
    tree.names(FULL, PART)

    done = tree.check(ALL_RAN)

    assert _passed(done), done.out + done.err
    assert f"rust-coverage: paths in {LIST}: 2" in done.out
    assert "rust-coverage: crate one: tests ran 4 of 4 regions, 6 of 6 lines" in done.out


def test_a_region_that_no_test_runs_fails(tree: Tree) -> None:
    tree.names(FULL, PART)

    done = tree.check(_report(_entry(FULL), _entry(PART, segments=MISSED)))

    assert _failed(done, f"{PART}: {MISSED_REGIONS}", f"{PART}: {MISSED_LINES}"), (
        done.out + done.err
    )


def test_one_missed_region_of_100000_fails(tree: Tree) -> None:
    """The script compares two counts. A percent of this file rounds to
    100.0, for the regions and for the lines."""
    tree.names(PART)
    assert round(100 * 99_999 / 100_000, 2) == 100.0

    done = tree.check(_report(_entry(PART, segments=_code(99_999, 1))))

    assert _failed(
        done,
        f"{PART}: no test ran 1 of 100000 regions. They start at 100000:1",
        f"{PART}: no test ran 1 of 100000 lines. They are 100000",
    ), done.out + done.err


def test_a_failure_line_names_twenty_places_at_most(tree: Tree) -> None:
    """Each second line has a region that no test ran, so each one is a
    place of its own."""
    tree.names(PART)
    segments = [
        segment
        for line in range(1, 48)
        for segment in ([line, 1, line % 2, True, True, False], [line, 9, 0, False, False, False])
    ]
    regions = ", ".join(f"{line}:1" for line in range(2, 42, 2))
    lines = ", ".join(str(line) for line in range(2, 42, 2))

    done = tree.check(_report(_entry(PART, segments=segments)))

    assert _failed(
        done,
        f"{PART}: no test ran 23 of 47 regions. They start at {regions} and 3 more",
        f"{PART}: no test ran 23 of 47 lines. They are {lines} and 3 more",
    ), done.out + done.err


def test_each_listed_file_that_fails_has_its_line(tree: Tree) -> None:
    tree.names(FULL, PART)
    report = _report(_entry(FULL, segments=_code(4, 2)), _entry(PART, segments=MISSED))

    done = tree.check(report)

    assert _failed(
        done,
        f"{FULL}: no test ran 2 of 6 regions. They start at 5:1, 6:1",
        f"{FULL}: no test ran 2 of 6 lines. They are 5-6",
        f"{PART}: {MISSED_REGIONS}",
        f"{PART}: {MISSED_LINES}",
    ), done.out + done.err


def test_a_file_outside_the_list_has_no_threshold(tree: Tree) -> None:
    tree.names(FULL)

    done = tree.check(_report(_entry(FULL), _entry(PART, segments=_code(0, 25))))

    assert _passed(done), done.out + done.err


#: The segments that llvm-cov wrote for one Rust file of 37 lines, with the
#: compiler 1.92.0. A function of the file ran in no test, and so did one arm
#: of a `match`, one side of an `if` and a closure. The lcov export of the
#: same run gives 24 lines with code, and the count 0 for the lines 9, 10,
#: 12, 13, 18 and 19.
MEASURED: list[list[Any]] = json.loads(
    """[
[1,1,2,true,true,false],[1,41,0,false,false,false],[2,11,2,true,true,false],
[2,16,0,false,false,false],[3,14,1,true,true,false],[3,20,0,false,false,false],
[4,9,1,true,true,false],[4,14,0,false,false,false],[4,18,0,true,true,false],
[4,23,0,false,false,false],[5,14,1,true,true,false],[5,20,0,false,false,false],
[7,1,2,true,true,false],[7,2,0,false,false,false],[9,1,0,true,true,false],
[9,35,0,false,false,false],[10,9,0,true,true,false],[10,16,0,false,false,false],
[10,19,0,true,true,false],[10,24,0,false,false,false],[12,5,0,true,true,false],
[12,16,0,false,false,false],[13,1,0,true,true,false],[13,2,0,false,false,false],
[15,1,1,true,true,false],[15,49,0,false,false,false],[16,9,1,true,true,false],
[16,14,0,false,false,false],[16,20,1,true,true,false],[16,26,0,false,false,false],
[16,29,0,true,true,false],[16,36,0,false,false,false],[16,46,1,true,true,false],
[16,50,0,false,false,false],[17,9,1,true,true,false],[17,13,0,false,false,false],
[17,16,1,true,true,false],[17,21,0,false,false,false],[17,22,1,true,true,false],
[17,25,0,false,false,false],[17,32,0,true,true,false],[17,33,0,false,false,false],
[18,9,0,true,true,false],[18,16,0,false,false,false],[19,5,0,true,true,false],
[19,6,0,false,false,false],[21,5,1,true,true,false],[21,9,0,false,false,false],
[22,1,1,true,true,false],[22,2,0,false,false,false],[29,5,1,true,true,false],
[29,20,0,false,false,false],[30,9,1,true,true,false],[30,19,0,false,false,false],
[30,20,1,true,true,false],[30,25,0,false,false,false],[31,9,1,true,true,false],
[31,19,0,false,false,false],[31,20,1,true,true,false],[31,25,0,false,false,false],
[32,5,1,true,true,false],[32,6,0,false,false,false],[35,5,1,true,true,false],
[35,17,0,false,false,false],[36,9,1,true,true,false],[36,19,0,false,false,false],
[36,20,1,true,true,false],[36,24,0,false,false,false],[37,5,1,true,true,false],
[37,6,0,false,false,false]
]"""
)

#: (the segments of one file, its row, its failure lines when the list names
#: it)
SEGMENT_CASES: dict[str, tuple[list[list[Any]], str, list[str]]] = {
    "what the tool wrote for a real file": (
        MEASURED,
        "25 of 35 regions, 18 of 24 lines",
        [
            "no test ran 10 of 35 regions. They start at 4:18, 9:1, 10:9, 10:19, 12:5, 13:1, "
            "16:29, 17:32, 18:9, 19:5",
            "no test ran 6 of 24 lines. They are 9-10, 12-13, 18-19",
        ],
    ),
    # A line ran when one region on it ran.
    "a line with a region that ran and one that did not": (
        [
            [5, 1, 1, True, True, False],
            [5, 10, 0, True, True, False],
            [5, 20, 0, False, False, False],
        ],
        "1 of 2 regions, 1 of 1 lines",
        ["no test ran 1 of 2 regions. They start at 5:10"],
    ),
    # The segment at 7:9 starts no region. Its count holds for the lines 8,
    # 9 and 10.
    "lines that a segment with the count 0 reaches": (
        [
            [7, 1, 1, True, True, False],
            [7, 9, 0, True, False, False],
            [10, 1, 0, False, False, False],
        ],
        "1 of 1 regions, 1 of 4 lines",
        ["no test ran 3 of 4 lines. They are 8-10"],
    ),
    "a region of four lines that ran": (
        [[3, 1, 2, True, True, False], [6, 2, 0, False, False, False]],
        "1 of 1 regions, 4 of 4 lines",
        [],
    ),
    "a region of four lines that no test ran": (
        [[3, 1, 0, True, True, False], [6, 2, 0, False, False, False]],
        "0 of 1 regions, 0 of 4 lines",
        ["no test ran 1 of 1 regions. They start at 3:1", "no test ran 4 of 4 lines. They are 3-6"],
    ),
    # The segment at 4:1 starts a region with no count: the tool skipped
    # that code. Its line and the next one hold no code.
    "a region that the tool skipped": (
        [
            [3, 1, 1, True, True, False],
            [4, 1, 0, False, True, False],
            [5, 1, 0, False, False, False],
        ],
        "1 of 1 regions, 1 of 1 lines",
        [],
    ),
    # A region with a count starts on the line, after the skipped one. The
    # line then holds code.
    "a region after one that the tool skipped, on one line": (
        [
            [4, 1, 0, False, True, False],
            [4, 9, 0, True, True, False],
            [4, 20, 0, False, False, False],
        ],
        "0 of 1 regions, 0 of 1 lines",
        ["no test ran 1 of 1 regions. They start at 4:9", "no test ran 1 of 1 lines. They are 4"],
    ),
    # Two segments can start at one place.
    "two segments at one place": (
        [
            [3, 1, 1, True, True, False],
            [3, 1, 1, True, True, False],
            [3, 9, 0, False, False, False],
        ],
        "2 of 2 regions, 1 of 1 lines",
        [],
    ),
}


@pytest.mark.parametrize("name", SEGMENT_CASES)
def test_the_regions_and_the_lines_of_a_file_come_from_its_segments(tree: Tree, name: str) -> None:
    """The row is that of a file outside the list. The failure lines are
    those of the same file in the list."""
    segments, row, lines = SEGMENT_CASES[name]
    report = _report(_entry(PART, segments=segments))

    outside = tree.check(report)
    tree.names(PART)
    listed = tree.check(report)

    assert _passed(outside), outside.out + outside.err
    assert f"rust-coverage: crate one: tests ran {row}\n" in outside.out
    if lines:
        assert _failed(listed, *(f"{PART}: {line}" for line in lines)), listed.out + listed.err
    else:
        assert _passed(listed), listed.out + listed.err


# --- rule 2: the report holds each listed file --------------------------------


def test_a_listed_file_that_the_report_does_not_hold_fails(tree: Tree) -> None:
    tree.names(FULL, PART)

    done = tree.check(_report(_entry(FULL)))

    assert _failed(
        done,
        f"{PART}: the report does not hold this file. "
        'rust/AGENTS.md, "The coverage rule", lists each cause',
    ), done.out + done.err


def test_a_file_outside_the_workspace_is_not_a_listed_file(tree: Tree) -> None:
    """The path of this file starts with the text of the workspace path, and
    it is under another directory: `/work/rust_crates`. A compare with no
    `/` would take it for the listed file."""
    tree.names(PART)
    outside = _entry(PART) | {"filename": f"{WORKSPACE}_{PART.removeprefix('rust/')}"}

    done = tree.check(_report(_entry(FULL), outside))

    assert _failed(
        done,
        f"{PART}: the report does not hold this file. "
        'rust/AGENTS.md, "The coverage rule", lists each cause',
    ), done.out + done.err
    assert f"rust-coverage: crate (no crate): tests ran {RAN_ROW}" in done.out


def test_a_listed_file_with_no_region_in_the_report_fails(tree: Tree) -> None:
    """A file that the report holds with no region binds nothing, as a file
    that the report does not hold."""
    tree.names(PART)

    done = tree.check(_report(_entry(FULL), _entry(PART, segments=[])))

    assert _failed(done, f"{PART}: the report holds no region of this file"), done.out + done.err


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


def test_a_pure_source_path_that_is_a_file_stops_the_run(tree: Tree) -> None:
    """A walk of a path that is no directory finds no file. The check then
    passes with nothing read."""
    tree.write(PURE, "no directory\n")

    done = tree.check(ALL_RAN)

    assert _stopped(done, f"{PURE} is not a directory"), done.out + done.err


@pytest.mark.parametrize("link", [PURE, f"{PURE}/verbs"], ids=["the directory", "below it"])
def test_a_link_to_a_directory_of_the_pure_crate_stops_the_run(tree: Tree, link: str) -> None:
    """A walk does not follow a symbolic link to a directory. A `.rs` file
    behind the link is then in no check."""
    tree.write(PURE_FILE, "pub fn decide() {}\n")
    tree.write("rust/elsewhere/schema.rs", "pub fn decide() {}\n")
    tree.names(PURE_FILE)
    if link == PURE:
        shutil.rmtree(tree.root / PURE)
    (tree.root / link).symlink_to(tree.root / "rust/elsewhere", target_is_directory=True)

    done = tree.check(_report(_entry(PURE_FILE)))

    said = f"{PURE} is not a directory" if link == PURE else f"{link} is a symbolic link"
    assert _stopped(done, said), done.out + done.err


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a directory of each mode")
def test_a_directory_of_the_pure_crate_that_gives_an_error_stops_the_run(tree: Tree) -> None:
    tree.write(PURE_FILE, "pub fn decide() {}\n")
    tree.write(PURE_DEEP, "pub fn decide() {}\n")
    tree.names(PURE_FILE)
    closed = (tree.root / PURE_DEEP).parent
    closed.chmod(0)

    try:
        done = tree.check(_report(_entry(PURE_FILE)))
    finally:
        closed.chmod(0o755)

    assert _stopped(done, f"cannot read a directory of {PURE}: "), done.out + done.err


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
        " # a comment",
        "\t# a comment",
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

    assert _stopped(done, f"cannot read {LIST}: "), done.out + done.err
    assert done.out == ""


# --- the branches -------------------------------------------------------------

#: A report with six sides of a branch. No test took two of them, both in
#: the second file.
ONE_SIDE = _report(
    _entry(FULL, branches=[_branch(2, 8, 3, 1)]),
    _entry(PART, branches=[_branch(3, 8, 0, 2), _branch(2, 8, 3, 0)]),
)
ONE_SIDE_LINE = f"{PART}: no test took 2 of 4 sides of a branch. They are at 2:8 false, 3:8 true"


def test_the_branch_flag_fails_a_side_that_no_test_took(tree: Tree) -> None:
    tree.names(FULL, PART)

    done = tree.check(ONE_SIDE, "--branch")

    assert _failed(done, ONE_SIDE_LINE), done.out + done.err
    assert (
        "rust-coverage: crate one: tests ran 4 of 4 regions, 6 of 6 lines, 4 of 6 sides of a branch"
    ) in done.out


def test_two_copies_of_a_branch_add_their_counts(tree: Tree) -> None:
    """The report holds a branch one time for each copy of its code. One
    copy took the first side here, and the other copy took the second one."""
    tree.names(PART)
    branches = [_branch(2, 8, 1, 0), _branch(2, 8, 0, 1)]

    done = tree.check(_report(_entry(PART, branches=branches)), "--branch")

    assert _passed(done), done.out + done.err
    assert f"rust-coverage: crate one: tests ran {RAN_ROW}, 2 of 2 sides of a branch" in done.out


def test_a_run_with_no_branch_flag_reads_no_branch(tree: Tree) -> None:
    tree.names(FULL, PART)

    done = tree.check(ONE_SIDE)

    assert _passed(done), done.out + done.err
    assert "branch" not in done.out


def test_the_branch_flag_stops_a_report_with_no_branch(tree: Tree) -> None:
    """The stable toolchain writes no branch. A check of such a report would
    pass with nothing measured."""
    tree.names(FULL)

    done = tree.check(ALL_RAN, "--branch")

    assert _stopped(done, "the report holds no branch, so the run measured none"), (
        done.out + done.err
    )


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
    "a number that is no JSON": json.dumps(ALL_RAN).replace("88.89", "NaN", 1),
    "a key two times": json.dumps(ALL_RAN).replace(
        '"branches": []', '"branches": [], "branches": []', 1
    ),
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
    "no segments": _changed(lambda _report, file: file.pop("segments")),
    "segments that are no list": _changed(lambda _report, file: _set(file, "segments", {})),
    "no branches": _changed(lambda _report, file: file.pop("branches")),
    "branches that are no list": _changed(lambda _report, file: _set(file, "branches", {})),
}


def test_each_bad_report_differs_from_the_good_one() -> None:
    """A change that changes nothing would make its case pass for another
    reason."""
    good = json.dumps(ALL_RAN)

    for name, report in BAD_REPORTS.items():
        assert (report if isinstance(report, str) else json.dumps(report)) != good, name


def test_a_report_that_is_no_file_stops_the_run(tree: Tree) -> None:
    tree.names(FULL)

    done = tree.run(["--report", "absent.json"], tree.tools, {}, tree.root.parent)

    assert _stopped(done, "cannot read the report: "), done.out + done.err
    assert done.out == ""


@pytest.mark.parametrize("name", BAD_REPORTS)
def test_a_report_in_another_form_stops_the_run(tree: Tree, name: str) -> None:
    """The list names no file. The reader still takes each file of the
    report in full, so a report that it reads in part gives no row."""
    done = tree.check(BAD_REPORTS[name])

    assert _stopped(done, ""), done.out + done.err
    assert done.out == ""


@pytest.mark.parametrize(
    "segments",
    [
        [7],
        [[12, 5, 0, True, True]],
        [[12, 5, 0, True, True, False, 0]],
        [[12, 5, 0, 1, True, False]],
        [[12, 5, False, True, True, False]],
        [["12", 5, 0, True, True, False]],
        [[12, 5, 1.0, True, True, False]],
        [[12, 5, -1, True, True, False]],
        [[-12, 5, 1, True, True, False]],
        [[12, 5, 1, True, True, False], [12, 4, 1, True, True, False]],
        [[12, 5, 1, True, True, False], [11, 9, 1, True, True, False]],
    ],
    ids=repr,
)
def test_segments_in_another_form_stop_the_run(tree: Tree, segments: Any) -> None:
    """The file is outside the list. The counts of a crate come from the
    segments too, so the run names no count that it did not read."""
    done = tree.check(_report(_entry(FULL), _entry(PART, segments=segments)))

    assert _stopped(done, ""), done.out + done.err
    assert done.out == ""


@pytest.mark.parametrize(
    "branches",
    [
        [7],
        [[2, 8, 2, 14, 1, 0, 0, 0]],
        [[2, 8, 2, 14, 1, 0, 0, 0, 4, 0]],
        [[2, 8, 2, 14, True, 0, 0, 0, 4]],
        [[2, 8, 2, 14, "1", 0, 0, 0, 4]],
        [[2, 8, 2, 14, 1.0, 0, 0, 0, 4]],
        [[2, 8, 2, 14, 1, -1, 0, 0, 4]],
    ],
    ids=repr,
)
@pytest.mark.parametrize("flags", [[], ["--branch"]], ids=["no flag", "--branch"])
def test_branches_in_another_form_stop_the_run(tree: Tree, branches: Any, flags: list[str]) -> None:
    """The reader has one form for a report. It does not depend on the
    flag."""
    report = _report(_entry(FULL, branches=[_branch(2, 8, 1, 1)]), _entry(PART, branches=branches))

    done = tree.check(report, *flags)

    assert _stopped(done, ""), done.out + done.err
    assert done.out == ""


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

    assert _failed(done, ONE_SIDE_LINE), done.out + done.err
    (step,) = done.cargo
    assert step.rsplit(" ", 1)[0] == f"{COV_STEP} --branch {OUTPUT}"


def test_the_check_reads_the_report_of_its_own_run(tree: Tree) -> None:
    tree.names(PART)

    done = tree.cover(_report(_entry(PART, segments=MISSED)))

    assert _failed(done, f"{PART}: {MISSED_REGIONS}", f"{PART}: {MISSED_LINES}"), (
        done.out + done.err
    )


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

    done = tree.run(["--report", "report.json"], tree.with_cov, {}, saved.parent)

    assert _passed(done), done.out + done.err
    assert done.cargo == []


# --- the script and the caller ------------------------------------------------


def test_the_rust_directory_has_its_one_home_in_the_rule_library(tree: Tree) -> None:
    """The script sources the library for `RUST_DIR` and holds no copy of the
    value. Without the library, the script reads no report."""
    assert "RUST_DIR=" not in (tree.root / SCRIPT).read_text(encoding="utf-8")
    (tree.root / RULE).unlink()

    done = tree.check(ALL_RAN)

    assert done.code == 1, done.out + done.err
    assert done.out == ""


@pytest.mark.parametrize("through", ["PYTHONPATH", "the directory of the caller"])
def test_the_check_imports_no_module_of_the_caller(
    tree: Tree, tmp_path: Path, through: str
) -> None:
    """The check imports `json`. A file `json.py` of the caller must not take
    its place: this one ends the program with the status 0."""
    theirs = tmp_path / "theirs"
    theirs.mkdir()
    (theirs / "json.py").write_text(HOSTILE, encoding="utf-8")
    tree.names(PART)
    report = _report(_entry(PART, segments=MISSED))

    if through == "PYTHONPATH":
        done = tree.check(report, env={"PYTHONPATH": str(theirs)})
    else:
        done = tree.check(report, cwd=theirs)

    assert _failed(done, f"{PART}: {MISSED_REGIONS}", f"{PART}: {MISSED_LINES}"), (
        done.out + done.err
    )


def _program() -> str:
    """The Python program of the script: the text between its two `PY`
    lines."""
    text = (BIN.parent / SCRIPT).read_text(encoding="utf-8")
    _before, rest = text.split("<<'PY'\n", 1)
    program, _after = rest.split("\nPY\n", 1)

    return program + "\n"


@pytest.mark.parametrize("step", [["check"], ["format", "--check"]], ids=" ".join)
def test_ruff_passes_the_python_program_of_the_script(step: list[str]) -> None:
    """The quality gate gives each `.py` file to ruff, and no `.sh` file.
    The name is that of a file under `bin/`, so the rules of this repository
    apply to the program."""
    done = subprocess.run(
        [sys.executable, "-m", "ruff", *step, "--no-cache", "--stdin-filename", SCRIPT + ".py"],
        input=_program(),
        cwd=BIN.parent,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert done.returncode == 0, done.stdout + done.stderr


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
    done = tree.run(args, tree.with_cov, {}, tree.root.parent)

    assert done.code == 2, done.out + done.err
    assert done.out == ""
    assert done.err == "usage: bin/rust-coverage.sh [--branch] [--report FILE]\n"
    assert done.cargo == []
