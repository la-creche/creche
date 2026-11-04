#!/usr/bin/env bash
# OPERATOR — run wherever a commit/push happens (Mac or the host, via githooks/).
# Quality gate: everything a change must pass before it lands.
#   ruff check .           lint
#   ruff format --check .  formatting
#   pyright                strict types (pyrightconfig.json)
#   datamodel-codegen      --check: wire/src/creche_wire/models.py is what
#                          the wire schema generates (wire/AGENTS.md)
#   pytest                 --tests: the full suite, in one process, by hand.
#                          CI runs the same suite as shards, with no flag
#                          here for its lint job (gate.yml, release.yml).
#                          --docs: only the tests marked `docs`, for a
#                          change bin/lib/docsrule.sh calls docs only
# The hooks pass no flag: they run no test (githooks/).
# Enforced by githooks/ (`git config core.hooksPath githooks`).
set -euo pipefail
cd "$(dirname -- "${BASH_SOURCE[0]}")/.."

#: One pytest command for the full run and the docs one, so both use the
#: same flags.
PYTEST=(uv run pytest -n auto)

#: The marker on every test that reads a doc or checks one exists
#: (pyproject.toml's markers).
DOCS_MARKER="docs"

MODE="${1:-}"
case "$MODE" in
  "" | --tests | --docs) ;;
  *)
    echo "usage: bin/quality-gate.sh [--tests | --docs]" >&2
    exit 2
    ;;
esac

command -v uv >/dev/null || { echo "quality-gate: uv not on PATH" >&2; exit 1; }

uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run datamodel-codegen --check

case "$MODE" in
  --tests) "${PYTEST[@]}" ;;
  --docs) "${PYTEST[@]}" -m "$DOCS_MARKER" ;;
esac

echo "quality-gate: PASS"
