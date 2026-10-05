#!/usr/bin/env bash
# CI and OPERATOR: the coverage rule of the Rust workspace under rust/
# (rust/AGENTS.md, "The coverage rule"). The `rust-coverage` job of gate.yml
# and release.yml runs it. No hook runs it.
#   bin/rust-coverage.sh                 runs the tests of the workspace with
#                                        cargo-llvm-cov, then checks the report
#   bin/rust-coverage.sh --branch        the same, and each branch of a listed
#                                        file. Only a nightly toolchain takes
#                                        this flag
#   bin/rust-coverage.sh --report FILE   checks FILE, a report that an earlier
#                                        run wrote. Starts no cargo
# Needs python3 on PATH. A run with no --report also needs cargo,
# cargo-llvm-cov and the component llvm-tools-preview of the toolchain.
# rustup takes the toolchain from rust/rust-toolchain.toml, so the cargo step
# runs inside rust/.
#
# rust/coverage-files.txt is the list. The check fails in four cases:
#   1. a region or a line of a listed file ran in no test
#   2. a listed file is not in the report
#   3. a listed file holds the text `coverage(off)`
#   4. rust/crates/chaperone-policy/src holds a .rs file that is not in the
#      list. Before that directory exists, this case cannot occur
# The check reads counts and no percent: a percent can round to 100. Each
# crate gets one line with its counts. The counts of a file outside the list
# have no threshold.
set -euo pipefail

#: The one directory that holds every Cargo file (rust/AGENTS.md).
RUST_DIR="rust"

#: The flag that adds the branches to the rule, and what the check then
#: measures. Without the flag it measures the regions and the lines.
#: `cargo llvm-cov` takes the same flag, and the script gives it to that step.
WITH_BRANCHES="--branch"
MEASURE="regions"
MEASURE_BRANCHES="branches"

#: The flag that names a report of an earlier run.
FROM_REPORT="--report"
REPORT=""

#: The program behind `cargo llvm-cov`. cargo finds a subcommand on PATH by
#: this name.
COV_PROGRAM="cargo-llvm-cov"

#: The coverage run, word for word: each test of each crate, with the locked
#: crates, and the report as JSON. The two `--no-cfg` flags keep `coverage`
#: and `coverage_nightly` unset, so the run compiles the code that a test
#: build compiles. Code behind `cfg(not(coverage))` is then in the report.
COV_STEP=(llvm-cov --workspace --locked --json --no-cfg-coverage --no-cfg-coverage-nightly)

usage() {
  echo "usage: bin/rust-coverage.sh [$WITH_BRANCHES] [$FROM_REPORT FILE]" >&2
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    "$WITH_BRANCHES")
      [[ "$MEASURE" != "$MEASURE_BRANCHES" ]] || usage
      MEASURE="$MEASURE_BRANCHES"
      shift
      ;;
    "$FROM_REPORT")
      [[ $# -ge 2 && -n "$2" && -z "$REPORT" ]] || usage
      REPORT="$2"
      shift 2
      ;;
    *)
      usage
      ;;
  esac
done

# A relative FILE is a path from the directory of the caller.
if [[ -n "$REPORT" && "$REPORT" != /* ]]; then
  REPORT="$PWD/$REPORT"
fi

cd "$(dirname -- "${BASH_SOURCE[0]}")/.."
ROOT="$PWD"

# check_report ROOT RUST_DIR REPORT MEASURE: the four rules over one report,
# and the counts of each crate. The report is JSON, so the check is a Python
# program: python3 has a JSON reader and bash has none. The program reads no
# variable of the environment and no file but the report, the list and the
# files that the list names.
check_report() {
  python3 -I - "$@" <<'PY'
from __future__ import annotations

import enum
import json
import os
import re
import sys
from typing import NamedTuple, NoReturn, cast

#: The first word of each line of output.
PROGRAM = "rust-coverage"

#: The list, under the Rust directory. Each file that it names has the rule.
LIST_NAME = "coverage-files.txt"

#: The directory of the crates, under the Rust directory.
CRATES = "crates"

#: The file that makes a directory there a crate.
CRATE_FILE = "Cargo.toml"

#: The source directory of the pure decision crate, under the Rust directory.
#: When it exists, the list names each `.rs` file in it.
PURE_SRC = "crates/chaperone-policy/src"

#: The ending of a Rust source file.
RUST_FILE = ".rs"

#: The text that takes an item out of the measurement, in each attribute
#: that holds it: `#[coverage(off)]`, `#![coverage(off)]` and `cfg_attr`.
NO_COVERAGE = "coverage(off)"

#: What follows the Rust directory in a path of the list: one segment or
#: more, then the name of a `.rs` file. A segment starts with a letter, a
#: digit or `_`, so `.` and `..` are no segment.
SEGMENT = r"[A-Za-z0-9_][A-Za-z0-9_.-]*"
TAIL = re.compile(rf"(?:{SEGMENT}/)*{SEGMENT}\.rs")

#: The row of a file that is under no crate directory.
NO_CRATE = "(no crate)"

#: How many places of one file a failure line names.
MOST_PLACES = 20

#: The types of one segment of a file: three numbers, then three flags.
SEGMENT_FORM = (int, int, int, bool, bool, bool)


class Measure(enum.Enum):
    """What a listed file must have in full. `BRANCHES` adds the branches to
    the regions and the lines."""

    REGIONS = "regions"
    BRANCHES = "branches"


class Refused(Exception):
    """The list or the report is not in the form that this script reads."""


class Counts(NamedTuple):
    """How many items a file has, and how many of them a test ran."""

    total: int
    ran: int

    def plus(self, other: Counts) -> Counts:
        return Counts(self.total + other.total, self.ran + other.ran)

    def missed(self) -> int:
        return self.total - self.ran

    def of(self, noun: str) -> str:
        return f"{self.ran} of {self.total} {noun}"


NOTHING = Counts(0, 0)


class File(NamedTuple):
    """The counts of one file of the report, or the sum of some files.
    `entry` is the raw entry of one file: only a failure line reads it."""

    regions: Counts
    lines: Counts
    branches: Counts
    entry: object

    def plus(self, other: File) -> File:
        return File(
            self.regions.plus(other.regions),
            self.lines.plus(other.lines),
            self.branches.plus(other.branches),
            None,
        )


NO_FILE = File(NOTHING, NOTHING, NOTHING, None)


def _no_key_twice(pairs: list[tuple[str, object]]) -> dict[str, object]:
    made: dict[str, object] = {}
    for key, value in pairs:
        if key in made:
            raise Refused(f"the report holds the key `{key}` two times in one object")
        made[key] = value

    return made


def _no_constant(word: str) -> NoReturn:
    raise Refused(f"the report holds `{word}`, which is no JSON number")


def _member(holder: object, key: str, where: str) -> object:
    if not isinstance(holder, dict) or key not in holder:
        raise Refused(f"{where} has no `{key}`")

    return cast("dict[str, object]", holder)[key]


def _text(holder: object, key: str, where: str) -> str:
    value = _member(holder, key, where)
    if not isinstance(value, str):
        raise Refused(f"`{key}` in {where} is not a text")

    return value


def _items(holder: object, key: str, where: str) -> list[object]:
    value = _member(holder, key, where)
    if not isinstance(value, list):
        raise Refused(f"`{key}` in {where} is not a list")

    return cast("list[object]", value)


def _count(holder: object, key: str, where: str) -> int:
    """A count is an integer of zero or more. `True` is no integer here."""
    value = _member(holder, key, where)
    if type(value) is not int or value < 0:
        raise Refused(f"`{key}` in {where} is not a count")

    return value


def _counts(summary: object, key: str, where: str) -> Counts:
    block = _member(summary, key, where)
    inside = f"`{key}` of {where}"
    made = Counts(_count(block, "count", inside), _count(block, "covered", inside))
    if made.ran > made.total:
        raise Refused(f"{inside} has more items that ran than items")

    return made


def read_report(path: str, rust_dir: str) -> dict[str, File]:
    """Each file of the report, by its path from the root of the repository.
    The report names a file by its full path, and it names the workspace file
    too. A file outside that workspace keeps its full path."""
    try:
        with open(path, encoding="utf-8") as file:
            raw = json.load(file, object_pairs_hook=_no_key_twice, parse_constant=_no_constant)
    except (OSError, ValueError, RecursionError) as why:
        raise Refused(f"cannot read the report: {why}") from why

    manifest = _text(_member(raw, "cargo_llvm_cov", "the report"), "manifest_path", "the report")
    workspace, last = os.path.split(manifest)
    if last != CRATE_FILE or not os.path.isabs(workspace):
        raise Refused("`manifest_path` of the report is not the full path of a workspace file")

    exports = _items(raw, "data", "the report")
    if len(exports) != 1:
        raise Refused(f"the report has {len(exports)} entries in `data`, not 1")

    files: dict[str, File] = {}
    inside = workspace + "/"
    for one in _items(exports[0], "files", "`data` of the report"):
        full = _text(one, "filename", "a file of the report")
        name = rust_dir + "/" + full[len(inside) :] if full.startswith(inside) else full
        if name in files:
            raise Refused(f"the report holds {name} two times")

        where = f"the summary of {name}"
        summary = _member(one, "summary", f"the entry of {name}")
        files[name] = File(
            _counts(summary, "regions", where),
            _counts(summary, "lines", where),
            _counts(summary, "branches", where),
            one,
        )

    return files


def read_list(root: str, rust_dir: str) -> tuple[list[str], list[str]]:
    """The paths of the list, and one text for each line that is not a
    comment and not a path. A line has no other form: no empty line, no space
    around a path, and a newline ends the last line."""
    shown = f"{rust_dir}/{LIST_NAME}"
    try:
        with open(os.path.join(root, rust_dir, LIST_NAME), encoding="utf-8", newline="") as file:
            lines = file.read().split("\n")
    except (OSError, ValueError) as why:
        raise Refused(f"cannot read {shown}: {why}") from why

    problems: list[str] = []
    if lines[-1] == "":
        lines.pop()
    else:
        problems.append(f"{shown}: no newline ends the last line")

    paths: list[str] = []
    start = rust_dir + "/"
    for number, line in enumerate(lines, start=1):
        if line.startswith("#"):
            continue

        if not line.startswith(start) or TAIL.fullmatch(line[len(start) :]) is None:
            problems.append(
                f"{shown}:{number}: not a comment and not the path of a `{RUST_FILE}` "
                f"file under {start}"
            )
            continue

        if line in paths:
            problems.append(f"{shown}:{number}: the list names {line} two times")
            continue

        paths.append(line)

    return paths, problems


def _places(entry: object, name: str) -> str:
    """Where a region of the file starts that no test ran, as `line:column`.
    A segment of the report is `[line, column, count, has a count, starts a
    region, is a gap]`."""
    found: list[str] = []
    for one in _items(entry, "segments", f"the entry of {name}"):
        items = cast("list[object]", one) if isinstance(one, list) else []
        if tuple(type(item) for item in items) != SEGMENT_FORM:
            raise Refused(f"a segment of {name} is not three numbers and three flags")

        line, column, count, counted, starts, gap = items
        if counted and starts and not gap and count == 0:
            found.append(f"{line}:{column}")

    shown = " ".join(found[:MOST_PLACES])
    more = len(found) - MOST_PLACES

    return shown + (f" and {more} more" if more > 0 else "")


def _holds_text(path: str, text: str) -> bool:
    try:
        with open(path, "rb") as file:
            return text.encode("ascii") in file.read()
    except OSError as why:
        raise Refused(f"cannot read a file of the list: {why}") from why


def check_listed(root: str, name: str, files: dict[str, File], measure: Measure) -> list[str]:
    """Each rule that one listed file breaks."""
    on_disk = os.path.join(root, name)
    if not os.path.isfile(on_disk):
        return [f"{name}: the list names this path, and it is not a file"]

    problems: list[str] = []
    if _holds_text(on_disk, NO_COVERAGE):
        problems.append(f"{name}: the file holds the text `{NO_COVERAGE}`")

    # CONTRACT-QUESTION: rust/AGENTS.md, "The coverage rule", check 2. The
    # rule does not say what a listed file with no function is. A report
    # holds no entry for such a file, for example a file with `mod` lines
    # only or with types only. The reading here is the strict one: such a
    # file fails. A change costs this one check.
    found = files.get(name)
    if found is None:
        problems.append(
            f"{name}: the report does not hold this file. A report holds a file "
            "only when the test build compiles a function of it"
        )

        return problems

    if found.regions.missed():
        problems.append(
            f"{name}: no test ran {found.regions.missed()} of {found.regions.total} "
            f"regions. They start at {_places(found.entry, name)}"
        )

    if found.lines.missed():
        problems.append(f"{name}: no test ran {found.lines.missed()} of {found.lines.total} lines")

    if measure is Measure.BRANCHES and found.branches.missed():
        problems.append(
            f"{name}: no test took {found.branches.missed()} of {found.branches.total} "
            "sides of a branch"
        )

    return problems


def check_pure(root: str, rust_dir: str, paths: list[str]) -> list[str]:
    """Each `.rs` file of the pure decision crate that the list does not
    name. Before that crate exists, the rule checks nothing."""
    shown = f"{rust_dir}/{PURE_SRC}"
    problems: list[str] = []
    for where, _dirs, names in os.walk(os.path.join(root, rust_dir, PURE_SRC)):
        for one in names:
            name = os.path.relpath(os.path.join(where, one), root)
            if one.endswith(RUST_FILE) and name not in paths:
                problems.append(f"{name}: the list does not name this file of {shown}")

    return sorted(problems)


def crate_of(name: str, rust_dir: str) -> str:
    parts = name.split("/")
    if len(parts) > 3 and parts[:2] == [rust_dir, CRATES]:
        return parts[2]

    return NO_CRATE


def crate_rows(root: str, rust_dir: str, files: dict[str, File]) -> dict[str, File]:
    """The sum of each crate. A crate of the workspace that the report does
    not hold has a row too, so a crate with no measured code shows."""
    rows: dict[str, File] = {}
    crates = os.path.join(root, rust_dir, CRATES)
    if os.path.isdir(crates):
        for one in os.listdir(crates):
            if os.path.isfile(os.path.join(crates, one, CRATE_FILE)):
                rows[one] = NO_FILE

    for name, found in files.items():
        crate = crate_of(name, rust_dir)
        rows[crate] = rows.get(crate, NO_FILE).plus(found)

    return rows


def row(label: str, sums: File, measure: Measure) -> str:
    text = f"{label}: tests ran {sums.regions.of('regions')}, {sums.lines.of('lines')}"
    if measure is Measure.BRANCHES:
        text += f", {sums.branches.of('sides of a branch')}"

    return text


def say(text: str) -> None:
    print(f"{PROGRAM}: {text}", flush=True)


def fail(text: str) -> None:
    print(f"{PROGRAM}: {text}", file=sys.stderr, flush=True)


def main(root: str, rust_dir: str, report: str, wanted: str) -> int:
    measure = Measure(wanted)
    try:
        paths, problems = read_list(root, rust_dir)
        files = read_report(report, rust_dir)
        rows = crate_rows(root, rust_dir, files)
        total = NO_FILE
        for crate in sorted(rows):
            say(row(f"crate {crate}", rows[crate], measure))
            total = total.plus(rows[crate])
        say(row("the workspace", total, measure))
        say(f"paths in {rust_dir}/{LIST_NAME}: {len(paths)}")

        for name in paths:
            problems += check_listed(root, name, files, measure)
        problems += check_pure(root, rust_dir, paths)
    except Refused as why:
        fail(str(why))
        return 1

    for one in problems:
        fail(one)
    if problems:
        fail(f"FAIL ({len(problems)})")
        return 1

    say("PASS")
    return 0


sys.exit(main(*sys.argv[1:]))
PY
}

command -v python3 >/dev/null || {
  echo "rust-coverage: python3 not on PATH: the check of the report needs it" >&2
  exit 1
}

if [[ -z "$REPORT" ]]; then
  for program in cargo "$COV_PROGRAM"; do
    command -v "$program" >/dev/null || {
      echo "rust-coverage: $program not on PATH: the coverage run needs it" >&2
      exit 1
    }
  done

  # The report goes into a directory of its own, and the script removes it
  # at its end. With a template, mktemp reads TMPDIR on each system. With
  # none, the mktemp of macOS does not. macOS ends TMPDIR with a slash.
  MADE_IN="${TMPDIR:-/tmp}"
  MADE="$(mktemp -d "${MADE_IN%/}/rust-coverage.XXXXXX")"
  trap 'rm -rf "$MADE"' EXIT
  REPORT="$MADE/coverage.json"

  if [[ "$MEASURE" == "$MEASURE_BRANCHES" ]]; then
    COV_STEP+=("$WITH_BRANCHES")
  fi

  (cd "$RUST_DIR" && cargo "${COV_STEP[@]}" --output-path "$REPORT")
fi

check_report "$ROOT" "$RUST_DIR" "$REPORT" "$MEASURE"
