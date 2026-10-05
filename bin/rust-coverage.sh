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
# The check reads counts and no percent: a percent can round to 100. It takes
# the counts from the segments of the report, and never from a summary. A
# region thus ran when one copy of its code ran it. Each crate gets one line
# with its counts. The counts of a file outside the list have no threshold.
set -euo pipefail

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

# RUST_DIR: the one directory that holds every Cargo file.
. bin/lib/rustrule.sh

# check_report ROOT RUST_DIR REPORT MEASURE: the four rules over one report,
# and the counts of each crate. The report is JSON, so the check is a Python
# program: python3 has a JSON reader and bash has none. `-I` keeps the
# program apart from the caller: it reads no variable of the environment, and
# it imports no module from the directory of the caller. It reads no file but
# the report, the list and the files that the list names.
# bin/tests/test_rust_coverage.py gives the program to ruff: no other check
# reads Python inside a shell script.
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

#: The file that holds the rule text, under the Rust directory, and the name
#: of its section there.
RULE_FILE = "AGENTS.md"
RULE_SECTION = "The coverage rule"

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

#: The types of one branch of a file: nine numbers. The first four are the
#: place. The next two are the counts of the two sides.
BRANCH_FORM = (int, int, int, int, int, int, int, int, int)

#: How many numbers of a branch are its place. The counts of the two sides
#: follow them.
BRANCH_PLACE = 4

#: The two sides of a branch, in the order of their counts.
SIDES = ("true", "false")


class Measure(enum.Enum):
    """What a listed file must have in full. `BRANCHES` adds the branches to
    the regions and the lines."""

    REGIONS = "regions"
    BRANCHES = "branches"


class Refused(Exception):
    """The list, the report or the tree is not in the form that this script
    reads."""


class Counts(NamedTuple):
    """How many items some files have, and how many of them a test ran."""

    total: int
    ran: int

    def plus(self, other: Counts) -> Counts:
        return Counts(self.total + other.total, self.ran + other.ran)

    def of(self, noun: str) -> str:
        return f"{self.ran} of {self.total} {noun}"


NOTHING = Counts(0, 0)


class Sums(NamedTuple):
    """The counts of one file of the report, or of some files together."""

    regions: Counts
    lines: Counts
    sides: Counts

    def plus(self, other: Sums) -> Sums:
        return Sums(
            self.regions.plus(other.regions),
            self.lines.plus(other.lines),
            self.sides.plus(other.sides),
        )


NO_SUMS = Sums(NOTHING, NOTHING, NOTHING)


class Part(NamedTuple):
    """The items of one kind in one file: how many the file has, how many of
    them no test ran, and where those are."""

    total: int
    missed: int
    places: tuple[str, ...]

    def counts(self) -> Counts:
        return Counts(self.total, self.total - self.missed)

    def shown(self) -> str:
        """The first places, and how many more the file has."""
        more = len(self.places) - MOST_PLACES

        return ", ".join(self.places[:MOST_PLACES]) + (f" and {more} more" if more > 0 else "")


class File(NamedTuple):
    """One file of the report: its regions, its lines and the sides of its
    branches."""

    regions: Part
    lines: Part
    sides: Part

    def sums(self) -> Sums:
        return Sums(self.regions.counts(), self.lines.counts(), self.sides.counts())


class Segment(NamedTuple):
    """One segment of a file, as llvm-cov writes it. `runs` is how many
    times the code ran, from this place to the next segment. It is the sum
    of each copy of the code: a generic function has one copy for each type,
    and cargo builds a crate one time with its unit tests and one time
    without them."""

    line: int
    column: int
    runs: int
    counted: bool
    starts: bool
    gap: bool

    def is_region(self) -> bool:
        """Whether a region with a count starts here."""
        return self.counted and self.starts and not self.gap


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


def _row(one: object, form: tuple[type, ...], what: str) -> list[object]:
    """One list of the report whose items have exactly the types of `form`.
    `True` is no number here, and a number is zero or more."""
    items = cast("list[object]", one) if isinstance(one, list) else []
    if tuple(type(item) for item in items) != form:
        raise Refused(f"{what} is not in the form of the tool")

    if any(type(item) is int and item < 0 for item in items):
        raise Refused(f"{what} holds a number below zero")

    return items


def _segments(entry: object, name: str) -> list[Segment]:
    """The segments of one file, in the order of the file."""
    made: list[Segment] = []
    for one in _items(entry, "segments", f"the entry of {name}"):
        row = _row(one, SEGMENT_FORM, f"a segment of {name}")
        segment = Segment(*cast("tuple[int, int, int, bool, bool, bool]", tuple(row)))
        if made and (segment.line, segment.column) < (made[-1].line, made[-1].column):
            raise Refused(f"the segments of {name} are not in the order of the file")

        made.append(segment)

    return made


def _regions(segments: list[Segment]) -> Part:
    """Each region of a file, and each one that no test ran: no copy of the
    code ran it."""
    starts = [one for one in segments if one.is_region()]
    places = tuple(f"{one.line}:{one.column}" for one in starts if one.runs == 0)

    return Part(len(starts), len(places), places)


def _lines(segments: list[Segment]) -> Part:
    """Each line of a file that holds code, and each one that no test ran.
    The count of a line is the count that `llvm-cov show` prints for it: the
    largest count of the regions that start on the line and of the segment
    that reaches the line from an earlier one."""
    total = 0
    missed = 0
    runs: list[tuple[int, int]] = []
    reaching: Segment | None = None
    at = 0
    while at < len(segments):
        line = segments[at].line
        here: list[Segment] = []
        while at < len(segments) and segments[at].line == line:
            here.append(segments[at])
            at += 1

        counts = [one.runs for one in here if one.is_region()]
        skipped = here[0].starts and not here[0].counted
        reached = reaching is not None and reaching.counted
        has_code = (not skipped and (reached or bool(counts))) or any(
            one.starts and one.counted for one in here
        )
        if has_code:
            total += 1
            if max([reaching.runs if reaching is not None else 0, *counts]) == 0:
                missed += 1
                runs.append((line, line))

        # No segment starts on the lines from here to the next segment. The
        # last segment of this line reaches each of them.
        reaching = here[-1]
        after = segments[at].line - line - 1 if at < len(segments) else 0
        if after > 0 and reaching.counted:
            total += after
            if reaching.runs == 0:
                missed += after
                runs.append((line + 1, line + after))

    return Part(total, missed, _ranges(runs))


def _ranges(runs: list[tuple[int, int]]) -> tuple[str, ...]:
    """`runs` as texts, in the order of the file. Two runs that touch are one
    range: `12-15`."""
    joined: list[tuple[int, int]] = []
    for first, last in runs:
        if joined and joined[-1][1] + 1 == first:
            joined[-1] = (joined[-1][0], last)
        else:
            joined.append((first, last))

    return tuple(str(first) if first == last else f"{first}-{last}" for first, last in joined)


def _sides(entry: object, name: str) -> Part:
    """Each side of each branch of a file, and each one that no test took.
    The report holds one entry for each copy of the code, so the counts of
    one place add up. Only a nightly toolchain writes a branch."""
    taken: dict[tuple[int, ...], list[int]] = {}
    for one in _items(entry, "branches", f"the entry of {name}"):
        numbers = cast("list[int]", _row(one, BRANCH_FORM, f"a branch of {name}"))
        counts = taken.setdefault(tuple(numbers[:BRANCH_PLACE]), [0] * len(SIDES))
        for side in range(len(SIDES)):
            counts[side] += numbers[BRANCH_PLACE + side]

    places = tuple(
        f"{place[0]}:{place[1]} {SIDES[side]}"
        for place in sorted(taken)
        for side in range(len(SIDES))
        if taken[place][side] == 0
    )

    return Part(len(SIDES) * len(taken), len(places), places)


def read_report(path: str, rust_dir: str) -> dict[str, File]:
    """Each file of the report, by its path from the root of the repository.
    The report names a file by its full path, and it names the workspace file
    too. A file outside that workspace keeps its full path.

    The reader takes the segments and the branches of a file, and never its
    summary. The summary takes the best copy of each function. It thus counts
    a region as code that no test ran although another copy ran it."""
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

        segments = _segments(one, name)
        files[name] = File(_regions(segments), _lines(segments), _sides(one, name))

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


def _holds_text(path: str, text: str) -> bool:
    try:
        with open(path, "rb") as file:
            return text.encode("ascii") in file.read()
    except OSError as why:
        raise Refused(f"cannot read a file of the list: {why}") from why


def check_listed(
    root: str, rust_dir: str, name: str, files: dict[str, File], measure: Measure
) -> list[str]:
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
    # file fails, and so does a file of which the report holds no region. A
    # change costs this one check.
    found = files.get(name)
    if found is None:
        problems.append(
            f"{name}: the report does not hold this file. "
            f'{rust_dir}/{RULE_FILE}, "{RULE_SECTION}", lists each cause'
        )

        return problems

    regions, lines, sides = found
    if regions.total == 0:
        problems.append(f"{name}: the report holds no region of this file")

    if regions.missed:
        problems.append(
            f"{name}: no test ran {regions.missed} of {regions.total} regions. "
            f"They start at {regions.shown()}"
        )

    if lines.missed:
        problems.append(
            f"{name}: no test ran {lines.missed} of {lines.total} lines. They are {lines.shown()}"
        )

    if measure is Measure.BRANCHES and sides.missed:
        problems.append(
            f"{name}: no test took {sides.missed} of {sides.total} sides of a branch. "
            f"They are at {sides.shown()}"
        )

    return problems


def check_pure(root: str, rust_dir: str, paths: list[str]) -> list[str]:
    """Each `.rs` file of the pure decision crate that the list does not
    name. Before that crate exists, the rule checks nothing. A tree that the
    walk cannot read in full stops the run: a directory that is a symbolic
    link, and a directory that gives an error."""
    shown = f"{rust_dir}/{PURE_SRC}"
    top = os.path.join(root, rust_dir, PURE_SRC)
    if not os.path.lexists(top):
        return []

    if os.path.islink(top) or not os.path.isdir(top):
        raise Refused(f"{shown} is not a directory")

    def stop(why: OSError) -> NoReturn:
        raise Refused(f"cannot read a directory of {shown}: {why}") from why

    problems: list[str] = []
    for where, dirs, names in os.walk(top, onerror=stop):
        for one in dirs:
            if os.path.islink(os.path.join(where, one)):
                link = os.path.relpath(os.path.join(where, one), root)
                raise Refused(f"{link} is a symbolic link to a directory")

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


def crate_rows(root: str, rust_dir: str, files: dict[str, File]) -> dict[str, Sums]:
    """The sum of each crate. A crate of the workspace that the report does
    not hold has a row too, so a crate with no measured code shows."""
    rows: dict[str, Sums] = {}
    crates = os.path.join(root, rust_dir, CRATES)
    if os.path.isdir(crates):
        for one in os.listdir(crates):
            if os.path.isfile(os.path.join(crates, one, CRATE_FILE)):
                rows[one] = NO_SUMS

    for name, found in files.items():
        crate = crate_of(name, rust_dir)
        rows[crate] = rows.get(crate, NO_SUMS).plus(found.sums())

    return rows


def row(label: str, sums: Sums, measure: Measure) -> str:
    text = f"{label}: tests ran {sums.regions.of('regions')}, {sums.lines.of('lines')}"
    if measure is Measure.BRANCHES:
        text += f", {sums.sides.of('sides of a branch')}"

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
        total = NO_SUMS
        for crate in sorted(rows):
            say(row(f"crate {crate}", rows[crate], measure))
            total = total.plus(rows[crate])
        say(row("the workspace", total, measure))
        say(f"paths in {rust_dir}/{LIST_NAME}: {len(paths)}")

        if measure is Measure.BRANCHES and total.sides.total == 0:
            raise Refused(
                "the report holds no branch, so the run measured none. "
                "Only a nightly toolchain measures the branches"
            )

        for name in paths:
            problems += check_listed(root, rust_dir, name, files, measure)
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
