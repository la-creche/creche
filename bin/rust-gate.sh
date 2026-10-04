#!/usr/bin/env bash
# OPERATOR and CI: the Rust half of the quality gate, for the workspace under
# rust/. bin/quality-gate.sh runs it for a change that touches rust/
# (bin/lib/rustrule.sh). The `rust` job of gate.yml and release.yml runs it.
#   bin/rust-gate.sh           the [lints] check, the include check, cargo
#                              fmt and cargo clippy
#   bin/rust-gate.sh --tests   the same, then cargo test
# Needs cargo on PATH. rustup takes the toolchain from
# rust/rust-toolchain.toml, so every cargo step runs inside rust/.
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
cargo fmt --all --check
cargo clippy --workspace --all-targets --locked -- -D warnings

if [[ "$MODE" == "$WITH_TESTS" ]]; then
  cargo test --workspace --locked
fi

echo "rust-gate: PASS"
