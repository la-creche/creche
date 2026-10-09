# Sourced only (bin/AGENTS.md, Library): the one copy of "does this change
# touch the Rust workspace?", shared by bin/quality-gate.sh (per commit and
# per push), .github/actions/scope (per PR) and release.yml (per merge), so
# the three cannot drift.
#
# A change that touches nothing under rust/ runs no cargo step and needs no
# cargo on PATH: some sessions commit from a sandbox with no Rust toolchain.
# A change that touches rust/ runs bin/rust-gate.sh.
#
# Every path under rust/ counts, a .md too. What this cannot read counts as a
# Rust change: the cargo checks are the safe answer.
#
# CI takes a wider answer than the hooks: rust_touched also counts a file of
# the Rust checks themselves (rust_gate_path). A runner always has cargo, so
# the wider answer costs no session its commit.
#
# The Rust tests read vectors/data (vectors/README.md), so a vector that moves
# can break one. rust_touched counts a path under vectors/ too (vectors_path).
# A push asks vectors_path itself and runs the Rust checks only where cargo
# is: a Python session with no Rust toolchain regenerates the vectors and
# must still push.
#
# Bash 3.2-clean: macOS runs the hook too.

#: The one directory that holds every Cargo file (rust/AGENTS.md).
RUST_DIR="rust"

# rust_path PATH: whether PATH is under rust/, e.g. rust/Cargo.lock. git
# writes a path with an unusual byte inside double quotes, and that one
# counts too.
rust_path() {
  case "$1" in
    "$RUST_DIR"/* | \""$RUST_DIR"/*)
      return 0
      ;;
  esac

  return 1
}

#: The directory that holds the vectors, the one input of the Rust tests
#: outside rust/ (rust/AGENTS.md, Tests).
VECTORS_DIR="vectors"

# vectors_path PATH: whether PATH is under vectors/, e.g.
# vectors/data/index.json. The generator and its tests count too: one rule
# for the whole directory. A path that git wrote in quotes counts, as for
# rust_path.
vectors_path() {
  case "$1" in
    "$VECTORS_DIR"/* | \""$VECTORS_DIR"/*)
      return 0
      ;;
  esac

  return 1
}

# rust_gate_path PATH: whether PATH is a file of the Rust checks themselves:
# the two scripts, this rule, and the CI files that hold the `rust` job and
# the `rust-coverage` job and ask this rule. bin/tests/test_rust_gate.py and
# bin/tests/test_rust_coverage.py run the scripts with a fake cargo, so a
# wrong cargo flag passes every test. Only a run with the real cargo proves a
# change to one of these files.
rust_gate_path() {
  case "$1" in
    bin/rust-gate.sh | bin/rust-coverage.sh | bin/lib/rustrule.sh | \
      .github/workflows/gate.yml | .github/workflows/release.yml | \
      .github/actions/scope/*)
      return 0
      ;;
  esac

  return 1
}

# rust_touched FROM TO: whether CI runs the Rust checks for FROM..TO: a path
# it changes is under rust/, is a file of the Rust checks, or is under
# vectors/.
rust_touched() {
  local paths path

  if ! paths="$(git -c core.quotePath=false diff --name-only --no-renames \
    "$1" "$2" 2>/dev/null)"; then
    return 0
  fi

  while IFS= read -r path; do
    if rust_path "$path" || rust_gate_path "$path" || vectors_path "$path"; then
      return 0
    fi
  done <<< "$paths"

  return 1
}

# rust_dirty: whether the index or the work tree differs from HEAD under
# rust/. A commit carries the index, and cargo reads the work tree, as ruff
# does. An untracked file is in no commit and does not count.
#   0  it differs
#   1  it does not
#   2  git cannot read the state. The caller counts that as a Rust change
#
# The commit that concludes a merge differs from HEAD by everything the other
# side brings. Those files passed the checks where they were made. So while
# the index and the work tree hold exactly the other side's rust/, nothing
# here is this session's change, and a session with no cargo can still merge
# main. An own edit on top, or a Rust change on both sides, still counts.
rust_dirty() {
  local changed theirs

  if ! changed="$(git --no-optional-locks status --porcelain \
    --untracked-files=no -- "$RUST_DIR" 2>/dev/null)"; then
    return 2
  fi

  if [[ -z "$changed" ]]; then
    return 1
  fi

  if theirs="$(git rev-parse -q --verify MERGE_HEAD 2>/dev/null)" &&
    git diff --quiet "$theirs" -- "$RUST_DIR" 2>/dev/null &&
    git diff --quiet --cached "$theirs" -- "$RUST_DIR" 2>/dev/null; then
    return 1
  fi

  return 0
}
