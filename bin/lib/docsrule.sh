# Sourced only (bin/AGENTS.md, Library): the one copy of "does this change
# touch nothing but prose?", shared by githooks/pre-push (per push),
# .github/workflows/gate.yml (per PR) and release.yml (per merge), so the
# three cannot drift.
#
# A docs-only change runs `bin/quality-gate.sh --docs`: lint, and only the
# tests marked `docs`, the ones that read a doc or check one exists. No
# other test depends on one.
#
# One kind of path passes, and nothing else does:
#
#   a .md outside tests/ and fixtures/   prose. A .md under either is a
#                                        registry fixture's instructions or
#                                        skill, which tests load
#
# What this cannot read counts as code, e.g. a revision git does not have,
# or a file one side lacks: the full suite is the safe answer.
#
# The tag planner holds a second copy of `docs_path`, in Python, because it
# must drop prose before it cuts a path (`handover/src/handover/allocate.py`,
# `is_prose`). `handover/tests/test_handover_bin_path_cut.py` holds the two
# equal: change both, or neither.
#
# Bash 3.2-clean: macOS runs the hook too.

# docs_path PATH: whether PATH is prose, e.g. docs/rework/spec.md or
# chaperone/AGENTS.md, and not a fixture's instructions.md.
docs_path() {
  case "/$1" in
    */tests/* | */fixtures/*)
      return 1
      ;;
    *.md)
      return 0
      ;;
  esac

  return 1
}

# docs_only FROM TO: whether every path FROM..TO changes is prose. An empty
# change is not docs only: the caller says what nothing means.
docs_only() {
  local paths path

  if ! paths="$(git -c core.quotePath=false diff --name-only --no-renames \
    "$1" "$2" 2>/dev/null)"; then
    return 1
  fi

  if [[ -z "$paths" ]]; then
    return 1
  fi

  while IFS= read -r path; do
    if docs_path "$path"; then
      continue
    fi

    return 1
  done <<< "$paths"

  return 0
}
