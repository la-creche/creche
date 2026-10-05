#!/usr/bin/env bash
# OPERATOR — run wherever a commit/push happens (Mac or the host, via githooks/).
# Quality gate: everything a change must pass before it lands.
#   ruff check .           lint
#   ruff format --check .  formatting
#   pyright                strict types (pyrightconfig.json)
#   pytest                 --tests: the full suite (pre-push, for a path in
#                          no package; CI runs the same suite as shards,
#                          with no flag here for its lint job: gate.yml,
#                          release.yml)
#                          --tests-for PATH...: only the suites of the
#                          packages PATH... touch (pre-push), and
#                          vectors/tests for a product package. A path under
#                          integration/proc/ that is not prose picks the
#                          process-level suite, in a pytest process of its own
#                          --docs: only the tests marked `docs`, for a
#                          change bin/lib/docsrule.sh calls docs only
#   bin/rust-gate.sh       only for a change under rust/ (bin/lib/rustrule.sh):
#                          cargo fmt, cargo clippy and cargo deny, and cargo
#                          test where pytest runs. cargo deny runs only where
#                          cargo-deny is on PATH. A push that changes vectors/
#                          runs it too, but only where cargo is on PATH. Any
#                          other change runs no cargo step and needs no cargo
#                          on PATH
# Enforced by githooks/ (`git config core.hooksPath githooks`).
set -euo pipefail
cd "$(dirname -- "${BASH_SOURCE[0]}")/.."

# rust_path, rust_dirty and RUST_DIR: what counts as a Rust change.
# vectors_path: what the Rust tests read outside rust/.
. bin/lib/rustrule.sh

# docs_path: what counts as prose.
. bin/lib/docsrule.sh

#: One pytest command for the full run, a scoped one and the docs one, so
#: all use the same flags.
PYTEST=(uv run pytest -n auto)

#: Every scoped run carries this file. Two packages' tests with one basename
#: break collection only when both are collected, so a run scoped to one
#: package never shows the clash, and CI's full run stops at collection.
ALWAYS="bin/tests/test_unique_test_basenames.py"

#: The marker on every test that reads a doc or checks one exists
#: (pyproject.toml's markers).
DOCS_MARKER="docs"

#: The Rust half of the gate, and the flag that adds cargo test to it.
RUST_GATE="bin/rust-gate.sh"
RUST_TESTS="--tests"

#: The suite that holds vectors/data equal to what the Python code does
#: (vectors/README.md). A change in a product package can move a vector, so a
#: scoped run for such a package carries this suite too. CI would find the
#: moved vector, but only after the push.
VECTORS_SUITE="vectors/tests"

#: The suites of the packages that hold no product code: a change there moves
#: no vector. Every other suite is the suite of a product package, so a new
#: suite in testpaths counts as one until this list names it.
NO_PRODUCT=("bin/tests" "$VECTORS_SUITE")

#: The process-level suite (integration/proc/AGENTS.md). No testpaths entry
#: holds it, so the full run does not hold it. It runs in a pytest process of
#: its own, with the command of that document: four workers. The `proc` job
#: of CI runs the same suite on one worker (.github/workflows/gate.yml).
PROC_DIR="integration/proc"
PROC_PYTEST=(uv run pytest "$PROC_DIR" -m slow -n 4)

#: The variable that makes each skip of that suite a failure. The `proc` job
#: sets it too. A test there skips itself when the playpen bundle is missing,
#: and a run in which every test skips judged nothing.
PROC_NO_SKIP="CRECHE_PROC_NO_SKIP"

# The suites the full run collects, one per line: pyproject.toml's
# testpaths, e.g. "chaperone/tests". A suite missing from disk is left out.
list_suites() {
  local suite

  awk '/^testpaths *=/ { on = 1 } on { print } on && index($0, "]") { exit }' pyproject.toml |
    { grep -o '"[^"]*"' || true; } | tr -d '"' | while IFS= read -r suite; do
    if [[ -d "$suite" ]]; then
      echo "$suite"
    fi
  done
}

# in_package PATH SUITES: whether PATH sits in the package of one of SUITES.
# A package is the directory above its suite: chaperone/ for chaperone/tests.
in_package() {
  local suite

  while IFS= read -r suite; do
    if [[ -n "$suite" && "$1" == "${suite%/*}/"* ]]; then
      return 0
    fi
  done <<< "$2"

  return 1
}

# product_suite SUITE: whether SUITE is the suite of a product package.
product_suite() {
  local other

  for other in "${NO_PRODUCT[@]}"; do
    if [[ "$1" == "$other" ]]; then
      return 1
    fi
  done

  return 0
}

# has_suite SUITE SUITES: whether SUITES, one per line, holds SUITE.
has_suite() {
  [[ $'\n'"$2"$'\n' == *$'\n'"$1"$'\n'* ]]
}

# tally FIRST COUNT: "FIRST", or "FIRST and N more".
tally() {
  if [[ "$2" -le 1 ]]; then
    printf '%s' "$1"
    return 0
  fi

  printf '%s and %d more' "$1" "$(($2 - 1))"
}

# proc_path PATH: whether PATH is under integration/proc/. A path that git
# wrote in quotes counts, as for rust_path.
proc_path() {
  case "$1" in
    "$PROC_DIR"/* | \""$PROC_DIR"/*)
      return 0
      ;;
  esac

  return 1
}

# tests_for PATH...: pytest on the suites of the packages PATH... touch, with
# one line per suite saying which path picked it. A path in no package
# (uv.lock, pyproject.toml, docs/, .github/, githooks/) can change what any
# suite sees, so it runs the full suite instead. A path under rust/ is cargo's
# to test, not pytest's: it picks no suite and is not a path in no package.
# A path under integration/proc/ is proc_for's: the same two things hold.
# A path in a product package also picks vectors/tests.
tests_for() {
  local suites suite path first count stray="" strays=0 others=0 product=0
  local procs=0
  local -a picked=()

  if [[ $# -eq 0 ]]; then
    echo "quality-gate: no tests: no paths given"
    return 0
  fi

  suites="$(list_suites)"

  # One path outside every package is enough to run everything.
  for path in "$@"; do
    if rust_path "$path"; then
      continue
    fi

    if proc_path "$path"; then
      procs=$((procs + 1))
      continue
    fi
    others=$((others + 1))

    if in_package "$path" "$suites"; then
      continue
    fi

    if [[ -z "$stray" ]]; then
      stray="$path"
    fi
    strays=$((strays + 1))
  done

  if [[ "$others" -eq 0 ]]; then
    if [[ "$procs" -eq 0 ]]; then
      echo "quality-gate: no pytest: every path is under $RUST_DIR/"
    fi

    return 0
  fi

  if [[ -n "$stray" ]]; then
    echo "quality-gate: full suite, for $(tally "$stray" "$strays") in no package"
    "${PYTEST[@]}"
    return 0
  fi

  echo "quality-gate: only the touched packages' tests; CI runs the full suite"
  while IFS= read -r suite; do
    first=""
    count=0
    for path in "$@"; do
      if [[ "$path" != "${suite%/*}/"* ]]; then
        continue
      fi

      if [[ -z "$first" ]]; then
        first="$path"
      fi
      count=$((count + 1))
    done

    if [[ "$count" -eq 0 ]]; then
      continue
    fi

    picked+=("$suite")
    echo "quality-gate: $suite, for $(tally "$first" "$count")"

    if product_suite "$suite"; then
      product=1
    fi
  done <<< "$suites"

  if [[ "$product" -eq 1 && " ${picked[*]} " != *" $VECTORS_SUITE "* ]] &&
    has_suite "$VECTORS_SUITE" "$suites"; then
    picked+=("$VECTORS_SUITE")
    echo "quality-gate: $VECTORS_SUITE, for a change in a product package"
  fi

  if [[ -f "$ALWAYS" && " ${picked[*]} " != *" ${ALWAYS%/*} "* ]]; then
    picked+=("$ALWAYS")
    echo "quality-gate: $ALWAYS, for every scoped run"
  fi

  "${PYTEST[@]}" "${picked[@]}"
}

# proc_for PATH...: the process-level suite, when one PATH or more is under
# integration/proc/ and is not prose, with one line saying which path picked
# it. The full suite holds no test in that directory, so only this run tests
# such a path before the push. A skip is a failure here: with no playpen
# bundle the suite would pass and judge nothing.
#
# Prose there (docs_path) picks no suite, because no test reads it. A push of
# a package and one line of integration/proc/AGENTS.md then needs no bundle.
proc_for() {
  local path first="" count=0 prose=0

  for path in "$@"; do
    if ! proc_path "$path"; then
      continue
    fi

    if docs_path "$path"; then
      prose=$((prose + 1))
      continue
    fi

    if [[ -z "$first" ]]; then
      first="$path"
    fi
    count=$((count + 1))
  done

  if [[ "$count" -eq 0 ]]; then
    if [[ "$prose" -gt 0 ]]; then
      echo "quality-gate: no process suite: each path under $PROC_DIR/ is prose"
    fi

    return 0
  fi

  echo "quality-gate: $PROC_DIR, for $(tally "$first" "$count")"
  env "$PROC_NO_SKIP=1" "${PROC_PYTEST[@]}"
}

# rust_for MODE PATH...: why this run needs the Rust checks, e.g.
# "rust/Cargo.lock and 1 more". Prints nothing when it needs none.
#   no flag       the index or the work tree changes rust/, or git cannot
#                 say whether it does
#   --tests       always: the full run leaves nothing out
#   --tests-for   one PATH or more is under rust/
#   --docs        never
rust_for() {
  local mode="$1" path first="" count=0 state=0
  shift

  case "$mode" in
    "")
      rust_dirty || state=$?
      case "$state" in
        0) printf '%s' "a change under $RUST_DIR/ in the index or the work tree" ;;
        1) ;;
        *) printf '%s' "a state of $RUST_DIR/ that git cannot read" ;;
      esac
      ;;
    --tests)
      printf '%s' "the full run"
      ;;
    --tests-for)
      for path in "$@"; do
        if ! rust_path "$path"; then
          continue
        fi

        if [[ -z "$first" ]]; then
          first="$path"
        fi
        count=$((count + 1))
      done

      if [[ "$count" -gt 0 ]]; then
        tally "$first" "$count"
      fi
      ;;
  esac
}

# vectors_for MODE PATH...: which paths of a scoped run are under vectors/,
# e.g. "vectors/data/index.json and 2 more". Prints nothing in another mode:
# a commit runs no test, the full run runs the Rust checks for everything,
# and a docs run starts no cargo step.
#
# The Rust tests read vectors/data, so these paths run the Rust checks too,
# but only where cargo is on PATH. A Python session with no Rust toolchain
# regenerates the vectors and must still push. CI runs the Rust checks for
# the same change (rust_touched in bin/lib/rustrule.sh).
vectors_for() {
  local mode="$1" path first="" count=0
  shift

  if [[ "$mode" != --tests-for ]]; then
    return 0
  fi

  for path in "$@"; do
    if ! vectors_path "$path"; then
      continue
    fi

    if [[ -z "$first" ]]; then
      first="$path"
    fi
    count=$((count + 1))
  done

  if [[ "$count" -gt 0 ]]; then
    tally "$first" "$count"
  fi
}

MODE="${1:-}"
case "$MODE" in
  "" | --tests | --docs) ;;
  --tests-for) shift ;;
  *)
    echo "usage: bin/quality-gate.sh [--tests | --tests-for PATH... | --docs]" >&2
    exit 2
    ;;
esac

command -v uv >/dev/null || { echo "quality-gate: uv not on PATH" >&2; exit 1; }

# Fail closed, and before the first check: a Rust change with no toolchain
# must not pass on the Python checks alone.
RUST_FOR="$(rust_for "$MODE" "$@")"
if [[ -n "$RUST_FOR" ]] && ! command -v cargo >/dev/null; then
  echo "quality-gate: cargo not on PATH: the Rust checks must run for $RUST_FOR" >&2
  exit 1
fi

# A vector that moves runs the Rust checks where cargo is, and passes with one
# line where it is not. A path under rust/ in the same run already decided.
NO_RUST_FOR=""
if [[ -z "$RUST_FOR" ]]; then
  VECTORS_FOR="$(vectors_for "$MODE" "$@")"
  if command -v cargo >/dev/null; then
    RUST_FOR="$VECTORS_FOR"
  else
    NO_RUST_FOR="$VECTORS_FOR"
  fi
fi

uv run ruff check .
uv run ruff format --check .
uv run pyright

if [[ -n "$RUST_FOR" ]]; then
  echo "quality-gate: $RUST_GATE, for $RUST_FOR"
  case "$MODE" in
    "") "$RUST_GATE" ;;
    *) "$RUST_GATE" "$RUST_TESTS" ;;
  esac
fi

if [[ -n "$NO_RUST_FOR" ]]; then
  echo "quality-gate: cargo not on PATH: no Rust test for $NO_RUST_FOR. CI runs the Rust tests"
fi

case "$MODE" in
  --tests) "${PYTEST[@]}" ;;
  --tests-for)
    tests_for "$@"
    proc_for "$@"
    ;;
  --docs) "${PYTEST[@]}" -m "$DOCS_MARKER" ;;
esac

echo "quality-gate: PASS"
