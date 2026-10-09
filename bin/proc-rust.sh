#!/usr/bin/env bash
# CI and OPERATOR: the process-level suite as the judge of a Rust program
# (integration/proc/AGENTS.md, "Replace a service with another binary"). The
# `proc-rust` job of gate.yml and of release.yml runs it. No hook runs it.
#   bin/proc-rust.sh             builds the program of each file
#                                rust/proc/*.run and runs the scenarios that
#                                the file selects against it
#   bin/proc-rust.sh --dry-run   reads each file and prints its lines.
#                                Starts no cargo and no test
# Needs cargo and uv on PATH. rustup takes the toolchain from
# rust/rust-toolchain.toml, so the cargo step runs inside rust/. Some
# scenarios need more, for example git, or the built playpen.
# integration/proc/AGENTS.md says what a scenario needs.
#
# ONE FILE, ONE PROGRAM. A file rust/proc/<name>.run gives one Rust program
# to the suite, in the place of one service. A line is a comment that starts
# with `#`, or one key, one space and one value:
#   package <name>    the cargo package that holds the program. One line
#   program <name>    the program of that package. One line
#   variable <name>   the variable of that service in the service table of
#                     integration/proc/proc_services.py. One line
#   select <test>     one test file of the suite, or one test of a file as
#                     pytest names it. One line or more
# A file has no empty line. A file that breaks a rule stops the run before
# the first build: the script reads each file first.
#
# FOR EACH FILE, in the order of the names:
#   1. it removes the program that an earlier build left
#   2. it runs `cargo build --release --locked -p <package>` inside rust/
#   3. it runs `uv run pytest <each selected test> -m slow` at the root of the
#      repository, with the variable set to the path of the built program
#      and with CRECHE_PROC_NO_SKIP=1
# Step 1 is the reason that the suite never judges an old program: a build
# that wrote its program to another place leaves no file, and the script
# stops. A test that skips is a failure, as in the `proc` job. The first
# step that fails stops the run.
#
# The suite stops on a variable that starts with CRECHE_PROC_ and that it
# does not read. This script defines no variable with that start. It sets
# only the variable of a file and CRECHE_PROC_NO_SKIP. A variable of the
# caller with that start reaches the suite, for example CRECHE_PROC_KEEP.
#
# bin/tests/test_proc_rust.py holds each file of rust/proc against the
# service table, the crates and the tests of the suite.
set -euo pipefail
cd "$(dirname -- "${BASH_SOURCE[0]}")/.."

#: The root of the repository. The suite starts a service in the directory
#: of its test, so the path of a program is absolute.
ROOT="$PWD"

#: The directory of the Cargo workspace, and where a release build of it
#: writes a program.
RUST_DIR="rust"
BUILD_DIR="$RUST_DIR/target/release"

#: The directory of the files, and the end of the name of a file.
RUN_DIR="$RUST_DIR/proc"
RUN_SUFFIX=".run"

#: The flag that prints the lines of each file and starts nothing.
DRY_RUN="--dry-run"

#: The start of each variable of the suite, and the one switch that this
#: script sets (integration/proc/proc_services.py).
PROC_PREFIX="CRECHE_PROC_"
PROC_NO_SKIP="${PROC_PREFIX}NO_SKIP"

#: The directory of the suite. A file selects only a test below it.
PROC_DIR="integration/proc"

#: The four keys of a file.
KEY_PACKAGE="package"
KEY_PROGRAM="program"
KEY_VARIABLE="variable"
KEY_SELECT="select"

#: The name of a file without RUN_SUFFIX, and the name of a cargo package or
#: of a program.
NAME_FORM='^[a-z0-9][a-z0-9_-]*$'

#: The name of a variable of the service table.
VARIABLE_FORM="^${PROC_PREFIX}[A-Z][A-Z0-9_]*\$"

#: One selected test: a file below PROC_DIR, then for one test of that file
#: `::`, the name of the test and the id of one case in brackets.
SELECT_FORM="^${PROC_DIR}/[A-Za-z0-9_/.-]+\\.py(::[A-Za-z0-9_]+(\\[[A-Za-z0-9_.-]+\\])?)?\$"

#: What read_run found in one file.
RUN_PACKAGE=""
RUN_PROGRAM=""
RUN_VARIABLE=""
RUN_SELECT=()

# refuse FILE LINE REASON: one line on stderr for a file that breaks a rule.
# LINE is 0 for a rule of the whole file.
refuse() {
  if [[ "$2" -eq 0 ]]; then
    echo "proc-rust: $1: $3" >&2
  else
    echo "proc-rust: $1: line $2: $3" >&2
  fi

  return 1
}

# selected TEST: whether RUN_SELECT holds TEST.
selected() {
  local one

  if [[ "${#RUN_SELECT[@]}" -eq 0 ]]; then
    return 1
  fi

  for one in "${RUN_SELECT[@]}"; do
    if [[ "$one" == "$1" ]]; then
      return 0
    fi
  done

  return 1
}

# set_once FILE LINE KEY OLD VALUE FORM: prints VALUE when the file did not
# give KEY before and VALUE has the form FORM.
set_once() {
  if [[ -n "$4" ]]; then
    refuse "$1" "$2" "the key \`$3\` is there two times"
    return 1
  fi

  if [[ ! "$5" =~ $6 ]]; then
    refuse "$1" "$2" "the value of \`$3\` has not the form of this key"
    return 1
  fi

  printf '%s' "$5"
}

# read_run FILE: reads one file into RUN_PACKAGE, RUN_PROGRAM, RUN_VARIABLE
# and RUN_SELECT. Fails, with one line, for the first rule that the file
# breaks.
read_run() {
  local file="$1" line key value number=0

  RUN_PACKAGE=""
  RUN_PROGRAM=""
  RUN_VARIABLE=""
  RUN_SELECT=()

  while IFS= read -r line || [[ -n "$line" ]]; do
    number=$((number + 1))

    if [[ "$line" == "#"* ]]; then
      continue
    fi

    key="${line%% *}"
    value="${line#* }"
    if [[ "$line" != "$key $value" || -z "$key" || -z "$value" ]]; then
      refuse "$file" "$number" "the line is not a comment and not one key, one space and one value"
      return 1
    fi

    case "$key" in
      "$KEY_PACKAGE")
        RUN_PACKAGE="$(set_once "$file" "$number" "$key" "$RUN_PACKAGE" "$value" "$NAME_FORM")" || return 1
        ;;
      "$KEY_PROGRAM")
        RUN_PROGRAM="$(set_once "$file" "$number" "$key" "$RUN_PROGRAM" "$value" "$NAME_FORM")" || return 1
        ;;
      "$KEY_VARIABLE")
        RUN_VARIABLE="$(set_once "$file" "$number" "$key" "$RUN_VARIABLE" "$value" "$VARIABLE_FORM")" || return 1
        ;;
      "$KEY_SELECT")
        if [[ ! "$value" =~ $SELECT_FORM || "$value" == *..* ]]; then
          refuse "$file" "$number" "the value of \`$key\` is not a test below $PROC_DIR"
          return 1
        fi

        if selected "$value"; then
          refuse "$file" "$number" "the test is there two times"
          return 1
        fi

        RUN_SELECT+=("$value")
        ;;
      *)
        refuse "$file" "$number" "the file has no key \`$key\`"
        return 1
        ;;
    esac
  done < "$file"

  if [[ -z "$RUN_PACKAGE" ]]; then
    refuse "$file" 0 "the file has no line \`$KEY_PACKAGE\`"
    return 1
  fi

  if [[ -z "$RUN_PROGRAM" ]]; then
    refuse "$file" 0 "the file has no line \`$KEY_PROGRAM\`"
    return 1
  fi

  if [[ -z "$RUN_VARIABLE" ]]; then
    refuse "$file" 0 "the file has no line \`$KEY_VARIABLE\`"
    return 1
  fi

  if [[ "$RUN_VARIABLE" == "$PROC_NO_SKIP" ]]; then
    refuse "$file" 0 "$PROC_NO_SKIP is a switch of the suite and starts no service"
    return 1
  fi

  if [[ "${#RUN_SELECT[@]}" -eq 0 ]]; then
    refuse "$file" 0 "the file has no line \`$KEY_SELECT\`"
    return 1
  fi
}

# print_run NAME: the lines of the file that read_run read last, one fact on
# each line: the name of the file, the key and the value, with a tab between
# two parts.
print_run() {
  local test

  printf '%s\t%s\t%s\n' "$1" "$KEY_PACKAGE" "$RUN_PACKAGE"
  printf '%s\t%s\t%s\n' "$1" "$KEY_PROGRAM" "$RUN_PROGRAM"
  printf '%s\t%s\t%s\n' "$1" "$KEY_VARIABLE" "$RUN_VARIABLE"
  for test in "${RUN_SELECT[@]}"; do
    printf '%s\t%s\t%s\n' "$1" "$KEY_SELECT" "$test"
  done
}

# judge NAME: builds the program of the file that read_run read last, and
# runs the selected tests against it.
judge() {
  local name="$1" built="$BUILD_DIR/$RUN_PROGRAM"

  echo "proc-rust: $name: cargo build --release --locked -p $RUN_PACKAGE"
  # A program of an earlier build must not stand in for a build that wrote
  # its program to another place.
  rm -f -- "$built"
  (cd "$RUST_DIR" && cargo build --release --locked -p "$RUN_PACKAGE")

  if [[ ! -f "$built" || ! -x "$built" ]]; then
    echo "proc-rust: $name: the build of the package $RUN_PACKAGE wrote no program $built" >&2
    return 1
  fi

  echo "proc-rust: $name: $RUN_VARIABLE=$ROOT/$built, ${#RUN_SELECT[@]} selected"
  env "$RUN_VARIABLE=$ROOT/$built" "$PROC_NO_SKIP=1" \
    uv run pytest "${RUN_SELECT[@]}" -m slow
}

MODE="${1:-}"
case "$MODE" in
  "" | "$DRY_RUN") ;;
  *)
    echo "usage: bin/proc-rust.sh [$DRY_RUN]" >&2
    exit 2
    ;;
esac

if [[ $# -gt 1 ]]; then
  echo "usage: bin/proc-rust.sh [$DRY_RUN]" >&2
  exit 2
fi

FILES=()
for file in "$RUN_DIR"/*"$RUN_SUFFIX"; do
  # With no file, the pattern itself is the one word of the loop.
  if [[ ! -f "$file" ]]; then
    continue
  fi

  name="${file##*/}"
  name="${name%"$RUN_SUFFIX"}"
  if [[ ! "$name" =~ $NAME_FORM ]]; then
    refuse "$file" 0 "the name of the file has not the form of a program name"
    exit 1
  fi

  FILES+=("$file")
done

# A run with no file judged nothing, and it must not pass.
if [[ "${#FILES[@]}" -eq 0 ]]; then
  echo "proc-rust: $RUN_DIR holds no file with a name that ends in $RUN_SUFFIX" >&2
  exit 1
fi

# Each file is read before the first build, so a file that breaks a rule
# costs no build.
for file in "${FILES[@]}"; do
  read_run "$file"
done

if [[ "$MODE" == "$DRY_RUN" ]]; then
  for file in "${FILES[@]}"; do
    name="${file##*/}"
    read_run "$file"
    print_run "${name%"$RUN_SUFFIX"}"
  done

  exit 0
fi

command -v cargo >/dev/null || {
  echo "proc-rust: cargo not on PATH: the build of a program needs the Rust toolchain" >&2
  exit 1
}

command -v uv >/dev/null || {
  echo "proc-rust: uv not on PATH: the suite runs in the venv of the workspace" >&2
  exit 1
}

for file in "${FILES[@]}"; do
  name="${file##*/}"
  read_run "$file"
  judge "${name%"$RUN_SUFFIX"}"
done

echo "proc-rust: PASS (${#FILES[@]} of ${#FILES[@]} files)"
