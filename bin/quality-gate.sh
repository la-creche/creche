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
#                          packages PATH... touch (pre-push)
#                          --docs: only the tests marked `docs`, for a
#                          change bin/lib/docsrule.sh calls docs only
# Enforced by githooks/ (`git config core.hooksPath githooks`).
set -euo pipefail
cd "$(dirname -- "${BASH_SOURCE[0]}")/.."

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

# tally FIRST COUNT: "FIRST", or "FIRST and N more".
tally() {
  if [[ "$2" -le 1 ]]; then
    printf '%s' "$1"
    return 0
  fi

  printf '%s and %d more' "$1" "$(($2 - 1))"
}

# tests_for PATH...: pytest on the suites of the packages PATH... touch, with
# one line per suite saying which path picked it. A path in no package
# (uv.lock, pyproject.toml, docs/, .github/, githooks/) can change what any
# suite sees, so it runs the full suite instead.
tests_for() {
  local suites suite path first count stray="" strays=0
  local -a picked=()

  if [[ $# -eq 0 ]]; then
    echo "quality-gate: no tests: no paths given"
    return 0
  fi

  suites="$(list_suites)"

  # One path outside every package is enough to run everything.
  for path in "$@"; do
    if in_package "$path" "$suites"; then
      continue
    fi

    if [[ -z "$stray" ]]; then
      stray="$path"
    fi
    strays=$((strays + 1))
  done

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
  done <<< "$suites"

  if [[ -f "$ALWAYS" && " ${picked[*]} " != *" ${ALWAYS%/*} "* ]]; then
    picked+=("$ALWAYS")
    echo "quality-gate: $ALWAYS, for every scoped run"
  fi

  "${PYTEST[@]}" "${picked[@]}"
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
  --tests-for) tests_for "$@" ;;
  --docs) "${PYTEST[@]}" -m "$DOCS_MARKER" ;;
esac

echo "quality-gate: PASS"
