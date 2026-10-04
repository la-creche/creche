# Sourced only (bin/AGENTS.md, Library): the one copy of "does this change
# touch the Rust workspace?", shared by bin/quality-gate.sh (per commit and
# per push), .github/actions/scope (per PR) and release.yml (per merge), so
# the four cannot drift.
#
# A change that touches nothing under rust/ runs no cargo step and needs no
# cargo on PATH: some sessions commit from a sandbox with no Rust toolchain.
# A change that touches rust/ runs bin/rust-gate.sh.
#
# Every path under rust/ counts, a .md too. What this cannot read counts as a
# Rust change: the cargo checks are the safe answer.
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

# rust_touched FROM TO: whether a path FROM..TO changes is under rust/.
rust_touched() {
  local paths path

  if ! paths="$(git -c core.quotePath=false diff --name-only --no-renames \
    "$1" "$2" 2>/dev/null)"; then
    return 0
  fi

  while IFS= read -r path; do
    if rust_path "$path"; then
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
