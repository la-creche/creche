#!/usr/bin/env bash
# OPERATOR and CI: the Rust half of the quality gate, for the workspace under
# rust/. bin/quality-gate.sh runs it for a change that touches rust/
# (bin/lib/rustrule.sh). The `rust` job of gate.yml and release.yml runs it.
#   bin/rust-gate.sh           the [lints] check, the include check, the
#                              panic check, cargo fmt, cargo clippy and
#                              cargo deny
#   bin/rust-gate.sh --tests   the same, then cargo test
# Needs cargo on PATH. rustup takes the toolchain from
# rust/rust-toolchain.toml, so every cargo step runs inside rust/.
# `cargo deny` needs cargo-deny on PATH. CI installs it. A developer machine
# without it prints one line and runs the other steps.
set -euo pipefail
cd "$(dirname -- "${BASH_SOURCE[0]}")/../rust"

#: The flag that adds the tests.
WITH_TESTS="--tests"

#: The file of the workspace itself. Every other Cargo.toml is a crate.
WORKSPACE_MANIFEST="./Cargo.toml"

#: cargo's own build directory. A directory named target anywhere else is
#: source: crates/target is a workspace member like any other.
BUILD_DIR="./target"

#: A line of Rust that includes a Markdown file, e.g.
#: `#![doc = include_str!("../README.md")]`.
MD_INCLUDE='include(_str|_bytes)?!.*\.md"'

#: The directory of the crates. Each directory in it is one crate.
CRATES_DIR="./crates"

#: A file below a directory `tests` of a crate, as a pattern. Each such file
#: is test code. The directory of the crate itself does not count:
#: crates/tests is a crate like any other.
TESTS_PATH="$CRATES_DIR/*/tests/*"

#: The line that marks a test module. It stands directly above the first
#: line of the module.
TEST_MARK='#[cfg(test)]'

#: awk rules that leave out the test code of a source file. The text scan
#: below starts with them, and gives them TEST_MARK as `mark`.
#:
#: Test code is each module with a body that has a TEST_MARK line directly
#: above it, from its first line to the line that closes it. That line holds
#: only `}`, at the indent of the first line: `cargo fmt` writes a module in
#: that form. A scan reads the code after that line again.
#:
#: A TEST_MARK line above another item starts no test code, e.g. above
#: `mod python;`. A scan reads that item and the code after it.
TEST_CODE_AWK='
  function trimmed(text) {
    gsub(/^[ \t\r]+|[ \t\r]+$/, "", text)
    return text
  }

  FNR == 1 { in_tests = 0; marked = 0 }

  in_tests {
    if ($0 == module_end) in_tests = 0
    next
  }

  marked && /^[ \t]*(pub(\((crate|super)\))? +)?mod +[A-Za-z0-9_]+ *[{]$/ {
    module_end = $0
    sub(/[^ \t].*$/, "}", module_end)
    in_tests = 1
    marked = 0
    next
  }

  { marked = (trimmed($0) == mark) }
'

#: A character that is no part of a Rust name. A word has one on each side,
#: or the start or the end of its line.
NAME_EDGE='[^A-Za-z0-9_]'

#: The call that catches a panic, and the pattern of that word in a line.
CATCH_NAME="catch_unwind"
CATCH_WORD="(^|$NAME_EDGE)$CATCH_NAME(\$|$NAME_EDGE)"

#: The crate that holds each panic boundary of a service, and a crate file
#: that names it: the name with no letter, no digit, no `_` and no `-` beside
#: it. Each line of the file counts, a comment too.
RUNTIME_CRATE="creche-runtime"
RUNTIME_NAMED="(^|[^A-Za-z0-9_-])$RUNTIME_CRATE(\$|[^A-Za-z0-9_-])"

#: The file of a crate that holds the entry function of a program with no
#: runtime, below the directory of the crate.
ENTRY_FILE="src/entry.rs"

#: The program behind `cargo deny`. cargo finds a subcommand on PATH by this
#: name.
DENY_PROGRAM="cargo-deny"

# inherits_lints MANIFEST: whether the crate takes the lint gate of
# rust/Cargo.toml. Only one spelling passes: a `[lints]` table that holds the
# line `workspace = true`.
inherits_lints() {
  awk '
    { sub(/^[ \t]+/, "") }
    /^\[/ { in_lints = ($0 ~ /^\[lints\][ \t]*(#.*)?$/); next }
    in_lints && /^workspace[ \t]*=[ \t]*true[ \t]*(#.*)?$/ { found = 1 }
    END { exit found ? 0 : 1 }
  ' "$1"
}

# lints_inherited: fails when a crate does not take the lint gate, with one
# line per crate. A crate with no [lints] table builds with no lint of
# [workspace.lints] at all, and cargo does not say so. A crate is every
# Cargo.toml under rust/ but the workspace file and cargo's own target/. A
# search that finds no crate has checked nothing, and fails too.
lints_inherited() {
  local manifest crates=0 missing=0

  while IFS= read -r manifest; do
    crates=$((crates + 1))

    if inherits_lints "$manifest"; then
      continue
    fi

    echo "rust-gate: rust/${manifest#./} has no \`[lints] workspace = true\`" >&2
    missing=$((missing + 1))
  done < <(find . -name Cargo.toml ! -path "$WORKSPACE_MANIFEST" ! -path "$BUILD_DIR/*" |
    LC_ALL=C sort)

  if [[ "$crates" -eq 0 ]]; then
    echo "rust-gate: the \`[lints]\` check read no crate under rust/" >&2
    return 1
  fi

  [[ "$missing" -eq 0 ]]
}

# no_md_included: fails when a Rust source file includes a Markdown file,
# with one line per file. A change of nothing but Markdown runs no cargo step
# (bin/lib/docsrule.sh), so a doc test that reads a .md could break with no
# cargo run. The check reads the text line by line: an include macro and a
# name that ends in .md on one line.
no_md_included() {
  local source found=0

  while IFS= read -r source; do
    echo "rust-gate: rust/${source#./} includes a Markdown file" >&2
    found=$((found + 1))
  done < <(find . -name '*.rs' ! -path "$BUILD_DIR/*" -exec grep -lE "$MD_INCLUDE" {} + |
    LC_ALL=C sort)

  [[ "$found" -eq 0 ]]
}

# names_runtime MANIFEST: whether the crate file names the runtime crate. A
# file that grep cannot read counts as a file that does.
names_runtime() {
  local status=0

  grep -qE "$RUNTIME_NAMED" "$1" 2>/dev/null || status=$?

  [[ "$status" -ne 1 ]]
}

# catch_permitted SOURCE: whether SOURCE is one of the three places that can
# hold CATCH_NAME (rust/AGENTS.md, "The panic rule", clause 8):
#   1. a file of the runtime crate
#   2. the entry file of a crate whose crate file does not name the runtime
#      crate
#   3. test code: a file below a directory `tests` of a crate, or a file
#      that holds the word only in its test code
catch_permitted() {
  local crate

  # TESTS_PATH is a pattern, so it has no quotes.
  case "$1" in
    "$CRATES_DIR/$RUNTIME_CRATE"/* | $TESTS_PATH)
      return 0
      ;;
  esac

  crate="${1#"$CRATES_DIR"/}"
  crate="${crate%%/*}"

  if [[ "$1" == "$CRATES_DIR/$crate/$ENTRY_FILE" ]] &&
    ! names_runtime "$CRATES_DIR/$crate/Cargo.toml"; then
    return 0
  fi

  LC_ALL=C awk -v mark="$TEST_MARK" -v word="$NAME_EDGE$CATCH_NAME$NAME_EDGE" "$TEST_CODE_AWK"'
    (" " $0 " ") ~ word { stray = 1; exit }
    END { exit stray ? 1 : 0 }
  ' "$1"
}

# no_stray_catch: fails when a Rust source file under rust/crates holds
# CATCH_NAME outside the three places of catch_permitted, with one line per
# file. A reviewer then knows where each panic boundary is. The check reads
# the text, so the word in a comment counts too.
no_stray_catch() {
  local source found=0

  while IFS= read -r source; do
    if catch_permitted "$source"; then
      continue
    fi

    echo "rust-gate: rust/${source#./} holds \`$CATCH_NAME\` outside the three places of the panic rule" >&2
    found=$((found + 1))
  done < <(find "$CRATES_DIR" -name '*.rs' -exec grep -lE "$CATCH_WORD" {} + |
    LC_ALL=C sort)

  [[ "$found" -eq 0 ]]
}

# deny_checked: the supply-chain check of the locked crates, against
# rust/deny.toml: the advisories, the bans, the licenses and the sources.
# `--locked` refuses a Cargo.lock that the manifests no longer match. The
# advisory check reads its database from the network.
#
# The check needs cargo-deny, which rustup does not install. Without it on
# PATH, a developer machine prints one line and passes: CI runs the check for
# the same change. In CI a missing cargo-deny fails. A runner sets CI, and a
# `rust` job that lost its install step must not pass with no check.
deny_checked() {
  if command -v "$DENY_PROGRAM" >/dev/null; then
    cargo deny --locked check
    return
  fi

  if [[ -n "${CI:-}" ]]; then
    echo "rust-gate: $DENY_PROGRAM not on PATH: CI must run the \`cargo deny\` check" >&2
    return 1
  fi

  echo "rust-gate: $DENY_PROGRAM not on PATH: no \`cargo deny\` check. CI runs the check"
}

MODE="${1:-}"
case "$MODE" in
  "" | "$WITH_TESTS") ;;
  *)
    echo "usage: bin/rust-gate.sh [$WITH_TESTS]" >&2
    exit 2
    ;;
esac

command -v cargo >/dev/null || {
  echo "rust-gate: cargo not on PATH: a change under rust/ needs the Rust toolchain" >&2
  exit 1
}

lints_inherited
no_md_included
no_stray_catch
cargo fmt --all --check
cargo clippy --workspace --all-targets --locked -- -D warnings
deny_checked

if [[ "$MODE" == "$WITH_TESTS" ]]; then
  cargo test --workspace --locked
fi

echo "rust-gate: PASS"
