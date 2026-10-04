#!/usr/bin/env bash
# OPERATOR and CI: the Rust half of the quality gate, for the workspace under
# rust/. bin/quality-gate.sh runs it for a change that touches rust/
# (bin/lib/rustrule.sh). The `rust` job of gate.yml and release.yml runs it.
#   bin/rust-gate.sh           the [lints] check, cargo fmt and cargo clippy
#   bin/rust-gate.sh --tests   the same, then cargo test
# Needs cargo on PATH. rustup takes the toolchain from
# rust/rust-toolchain.toml, so every cargo step runs inside rust/.
set -euo pipefail
cd "$(dirname -- "${BASH_SOURCE[0]}")/../rust"

#: The flag that adds the tests.
WITH_TESTS="--tests"

#: The file of the workspace itself. Every other Cargo.toml is a crate.
WORKSPACE_MANIFEST="./Cargo.toml"

# inherits_lints MANIFEST: whether the crate takes the lint gate of
# rust/Cargo.toml. Only one spelling passes: a `[lints]` table that holds the
# line `workspace = true`.
inherits_lints() {
  awk '
    { sub(/^[ \t]+/, "") }
    /^\[/ { table = $0; sub(/[ \t]*(#.*)?$/, "", table); next }
    table == "[lints]" && /^workspace[ \t]*=[ \t]*true[ \t]*(#.*)?$/ { found = 1 }
    END { exit found ? 0 : 1 }
  ' "$1"
}

# lints_inherited: fails when a crate does not take the lint gate, with one
# line per crate. A crate with no [lints] table builds with no lint of
# [workspace.lints] at all, and cargo does not say so. A crate is every
# Cargo.toml under rust/ but the workspace file and cargo's own target/.
lints_inherited() {
  local manifest missing=0

  while IFS= read -r manifest; do
    if inherits_lints "$manifest"; then
      continue
    fi

    echo "rust-gate: rust/${manifest#./} has no \`[lints] workspace = true\`" >&2
    missing=$((missing + 1))
  done < <(find . -name Cargo.toml ! -path "$WORKSPACE_MANIFEST" ! -path '*/target/*' |
    LC_ALL=C sort)

  [[ "$missing" -eq 0 ]]
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
cargo fmt --all --check
cargo clippy --workspace --all-targets --locked -- -D warnings

if [[ "$MODE" == "$WITH_TESTS" ]]; then
  cargo test --workspace --locked
fi

echo "rust-gate: PASS"
