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
# Enforced by githooks/ (`git config core.hooksPath githooks`).
set -euo pipefail
cd "$(dirname -- "${BASH_SOURCE[0]}")/.."

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
#: its own, on one worker for each core. The `proc` job of CI runs four
#: workers, one for each vCPU of the runner (.github/workflows/gate.yml).
PROC_DIR="integration/proc"
PROC_PYTEST=(uv run pytest "$PROC_DIR" -m slow -n auto)

#: The variable that makes each skip of that suite a failure. The `proc` job
#: sets it too. A test there skips itself when the facade build is missing,
#: and a run in which every test skips judged nothing.
PROC_NO_SKIP="CRECHE_PROC_NO_SKIP"

# The suites the full run collects, one per line: pyproject.toml's
# testpaths, e.g. "caregiver/tests". A suite missing from disk is left out.
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
# A package is the directory above its suite: caregiver/ for caregiver/tests.
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
# wrote in quotes counts too.
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
# suite sees, so it runs the full suite instead. A path under
# integration/proc/ is proc_for's: it picks no suite and is not a path in no
# package. A path in a product package also picks vectors/tests.
tests_for() {
  local suites suite path first count stray="" strays=0 others=0 product=0
  local -a picked=()

  if [[ $# -eq 0 ]]; then
    echo "quality-gate: no tests: no paths given"
    return 0
  fi

  suites="$(list_suites)"

  # One path outside every package is enough to run everything.
  for path in "$@"; do
    if proc_path "$path"; then
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
# such a path before the push. A skip is a failure here: with no facade
# build the suite would pass and judge nothing.
#
# Prose there (docs_path) picks no suite, because no test reads it. A push of
# a package and one line of integration/proc/AGENTS.md then needs no build.
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

uv run ruff check .
uv run ruff format --check .
uv run pyright

case "$MODE" in
  --tests) "${PYTEST[@]}" ;;
  --tests-for)
    tests_for "$@"
    proc_for "$@"
    ;;
  --docs) "${PYTEST[@]}" -m "$DOCS_MARKER" ;;
esac

echo "quality-gate: PASS"
