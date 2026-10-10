#!/usr/bin/env bash
# Pins what a push tests. githooks/pre-push turns the pushed refs into the
# paths they change, and `bin/quality-gate.sh --tests-for` turns those paths
# into the suites of the packages they touch. A path in no package runs the
# full suite, and a deleted branch runs nothing. A push that merged main in
# counts from main's fork point, so main's own changes do not count. A push
# of nothing but docs runs only the tests marked docs (bin/lib/docsrule.sh).
# A path under rust/ runs the cargo tests and picks no suite
# (bin/lib/rustrule.sh, with every other Rust case in test_rust_gate.py).
# A path in a product package also picks vectors/tests, so a change that
# moves a vector fails before the push. A path under vectors/ also runs the
# cargo tests, which read vectors/data. A path under integration/proc/ picks
# the process-level suite, which no testpaths entry holds, and does not start
# the full suite. Prose under integration/proc/ picks no suite.
# CI runs the full suite for any other change and is the merge gate: the
# scope decides whether a regression in the package just changed is caught
# before the push or only in CI.
#
# Runs the real gate and the real hook. `uv` and `cargo` are fakes on PATH
# that write their argv to a file, and the hook runs in a throwaway repository whose
# bin/quality-gate.sh is a fake that writes its own. Every exit code comes
# straight from `$?`, never through a pipe. A fake `cargo-deny` is on PATH
# too, so the cargo steps are the same on each machine.
#
# Run: bash bin/tests/test_pre_push_select.sh
set -uo pipefail
HERE="$(cd "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
GATE="$HERE/../quality-gate.sh"
HOOK="$HERE/../../githooks/pre-push"

# A GIT_DIR leaked from the caller would point the fixture's commits at the
# caller's repository (githooks/pre-commit). No personal git config
# (hooks, signing, default branch) reaches the fixture either.
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR GIT_OBJECT_DIRECTORY
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1

FAILS=0
pass() { printf 'PASS: %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*"; FAILS=$((FAILS + 1)); }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/pre_push_select_test.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

#: What every scoped run adds when bin/tests is not already in it.
ALWAYS="bin/tests/test_unique_test_basenames.py"

#: What a scoped run for a product package adds: the suite that holds
#: vectors/data equal to what the Python code does.
VECTORS="vectors/tests"

# said TEXT: whether the last gate or hook run printed TEXT.
said() {
  grep -qF -- "$1" "$OUT"
}

# words TEXT: TEXT's words, sorted, so two lists compare without their order.
words() {
  printf '%s\n' $1 | LC_ALL=C sort | tr '\n' ' '
}

# --- the gate: changed paths to suites -------------------------------------

mkdir -p "$WORK/bin"
cat > "$WORK/bin/uv" <<'FAKE'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$UV_LOG"
if [[ "${2:-}" == "pytest" ]]; then
  printf '%s\n' "${CRECHE_PROC_NO_SKIP:-unset}" >> "$NO_SKIP_LOG"
  exit "${PYTEST_RC:-0}"
fi
FAKE
chmod 0755 "$WORK/bin/uv"

cat > "$WORK/bin/cargo" <<'FAKE'
#!/usr/bin/env bash
printf '%s\n' "${1:-}" >> "$CARGO_LOG"
FAKE
chmod 0755 "$WORK/bin/cargo"

# bin/rust-gate.sh runs `cargo deny` only where cargo-deny is on PATH. The
# rest of PATH is the PATH of the caller, which can hold that program or not.
# Without it, the gate passes with one line, and where CI is set the gate
# fails. A runner of the `tests` job sets CI and has no cargo-deny. With this
# fake, each of those machines runs the same steps. The gate asks only
# whether the program is there. cargo starts it, so a direct call fails.
cat > "$WORK/bin/cargo-deny" <<'FAKE'
#!/usr/bin/env bash
exit 1
FAKE
chmod 0755 "$WORK/bin/cargo-deny"

#: The cargo subcommands of a run that tests Rust, in order. clippy runs two
#: times: for the workspace, and for the one crate with a default cargo
#: feature, with that feature off.
RUST_STEPS="fmt clippy clippy deny test"

# gate ARG...: the real gate, with the fake uv and the fake cargo. Sets RC,
# PYTEST to the pytest lines uv was asked for, one per pytest process (""
# when pytest never ran), NO_SKIP to what CRECHE_PROC_NO_SKIP held for each
# of those processes, and CARGO to the subcommands cargo was asked for (""
# when cargo never ran). A CRECHE_PROC_NO_SKIP of the caller does not reach
# the gate.
gate() {
  OUT="$WORK/gate.out"
  : > "$WORK/uv.log"
  : > "$WORK/cargo.log"
  : > "$WORK/no_skip.log"
  env -u CRECHE_PROC_NO_SKIP PATH="$WORK/bin:$PATH" UV_LOG="$WORK/uv.log" \
    CARGO_LOG="$WORK/cargo.log" NO_SKIP_LOG="$WORK/no_skip.log" \
    "$GATE" "$@" > "$OUT" 2>&1
  RC=$?
  PYTEST="$(grep '^run pytest' "$WORK/uv.log")"
  NO_SKIP="$(tr '\n' ' ' < "$WORK/no_skip.log")"
  NO_SKIP="${NO_SKIP% }"
  CARGO="$(tr '\n' ' ' < "$WORK/cargo.log")"
  CARGO="${CARGO% }"
}

# The full run is the baseline: a scoped run must use the same flags.
gate --tests
FULL="$PYTEST"
[[ "$RC" == "0" && -n "$FULL" ]] \
  && pass "--tests runs pytest ($FULL)" \
  || fail "--tests: rc=$RC, pytest line '$FULL'"
[[ "$CARGO" == "$RUST_STEPS" ]] \
  && pass "--tests runs the cargo tests too" \
  || fail "--tests: cargo ran '$CARGO'"

gate
[[ "$RC" == "0" && -z "$PYTEST" ]] \
  && pass "no flag (pre-commit) runs no tests" \
  || fail "no flag: rc=$RC, pytest line '$PYTEST'"

gate --tests-for chaperone/src/chaperone/app.py chaperone/README.md
[[ "$RC" == "0" && "$PYTEST" == "$FULL chaperone/tests $VECTORS $ALWAYS" ]] \
  && pass "one product package runs its suite and the vectors, with the full run's flags" \
  || fail "one package: rc=$RC, pytest line '$PYTEST'"
said "chaperone/tests, for chaperone/src/chaperone/app.py and 1 more" \
  && pass "the gate says which path picked the suite" \
  || fail "no reason line for chaperone/tests: $(cat "$OUT")"
said "$VECTORS, for a change in a product package" \
  && pass "the gate says why the vectors suite runs" \
  || fail "no reason line for $VECTORS: $(cat "$OUT")"
[[ -z "$CARGO" ]] \
  && pass "a push with no path under rust/ runs no cargo step" \
  || fail "one package: cargo ran '$CARGO'"

gate --tests-for caregiver/tests/caregiver_mws_registry/registry.yaml
[[ "$PYTEST" == "$FULL caregiver/tests $VECTORS $ALWAYS" ]] \
  && pass "a test fixture directory counts as its package" \
  || fail "a fixture path: pytest line '$PYTEST'"

gate --tests-for chaperone/pyproject.toml caregiver/AGENTS.md bin/lib/docsrule.sh
SCOPE="${PYTEST#"$FULL"}"
[[ "$RC" == "0" && "$PYTEST" == "$FULL "* ]] \
  && [[ "$(words "$SCOPE")" == "$(words "bin/tests caregiver/tests chaperone/tests $VECTORS")" ]] \
  && pass "three packages run three suites and the vectors, bin/tests already holding $ALWAYS" \
  || fail "three packages: rc=$RC, pytest line '$PYTEST'"

# bin/ and vectors/ hold no product code: a change there moves no vector.
gate --tests-for bin/quality-gate.sh bin/AGENTS.md
[[ "$RC" == "0" && "$PYTEST" == "$FULL bin/tests" && -z "$CARGO" ]] \
  && pass "a push of bin/ alone runs no vectors suite and no cargo step" \
  || fail "bin alone: rc=$RC, pytest line '$PYTEST', cargo ran '$CARGO'"

# The Rust tests read vectors/data. The fake cargo is on PATH here, and
# test_rust_gate.py holds the run with no cargo.
gate --tests-for vectors/data/index.json vectors/generate.py
[[ "$RC" == "0" && "$PYTEST" == "$FULL $VECTORS $ALWAYS" && "$CARGO" == "$RUST_STEPS" ]] \
  && pass "a path under vectors/ runs its suite one time, and the cargo tests" \
  || fail "vectors paths: rc=$RC, pytest line '$PYTEST', cargo ran '$CARGO'"

gate --tests-for chaperone/src/chaperone/app.py vectors/data/index.json
[[ "$RC" == "0" && "$PYTEST" == "$FULL chaperone/tests $VECTORS $ALWAYS" ]] \
  && pass "a product package and a moved vector run the vectors suite one time" \
  || fail "a package and a vector: rc=$RC, pytest line '$PYTEST'"

for stray in uv.lock pyproject.toml docs/host-release.md .github/workflows/gate.yml \
  githooks/pre-push playpen/package.json README.md chaperone-old/src/x.py; do
  gate --tests-for chaperone/src/chaperone/app.py "$stray"
  [[ "$RC" == "0" && "$PYTEST" == "$FULL" ]] && said "full suite, for $stray" \
    && pass "$stray is in no package: full suite" \
    || fail "$stray: rc=$RC, pytest line '$PYTEST'"
done

gate --tests-for rust/Cargo.lock rust/crates/creche-contracts/src/lib.rs
[[ "$RC" == "0" && -z "$PYTEST" && "$CARGO" == "$RUST_STEPS" ]] \
  && pass "a path under rust/ runs the cargo tests and no pytest" \
  || fail "rust paths: rc=$RC, pytest line '$PYTEST', cargo ran '$CARGO'"

gate --tests-for chaperone/src/chaperone/app.py rust/Cargo.lock
[[ "$RC" == "0" && "$PYTEST" == "$FULL chaperone/tests $VECTORS $ALWAYS" && "$CARGO" == "$RUST_STEPS" ]] \
  && pass "a push of Python and Rust runs the suite and the cargo tests" \
  || fail "python and rust: rc=$RC, pytest line '$PYTEST', cargo ran '$CARGO'"

# The process-level suite, as integration/proc/AGENTS.md runs it on four
# workers: a pytest process of its own. Each skip is a failure, as in the
# `proc` job of CI (.github/workflows/gate.yml).
PROC="run pytest integration/proc -m slow -n 4"

gate --tests-for integration/proc/proc_tree.py integration/proc/test_proc_table.py
[[ "$RC" == "0" && "$PYTEST" == "$PROC" && -z "$CARGO" ]] \
  && pass "a path under integration/proc/ runs the process suite and no other suite" \
  || fail "process suite paths: rc=$RC, pytest lines '$PYTEST', cargo ran '$CARGO'"
said "integration/proc, for integration/proc/proc_tree.py and 1 more" \
  && pass "the gate says which path picked the process suite" \
  || fail "no reason line for integration/proc: $(cat "$OUT")"
[[ "$NO_SKIP" == "1" ]] \
  && pass "a skip in the process suite is a failure, as in CI" \
  || fail "process suite paths: CRECHE_PROC_NO_SKIP was '$NO_SKIP'"

# Prose under integration/proc/ (bin/lib/docsrule.sh) picks no suite: no test
# reads it. A push of a package and one line of that document then needs no
# playpen bundle.
NO_PROC="no process suite: each path under integration/proc/ is prose"

gate --tests-for integration/proc/proc_tree.py integration/proc/AGENTS.md
[[ "$RC" == "0" && "$PYTEST" == "$PROC" ]] \
  && said "integration/proc, for integration/proc/proc_tree.py" && ! said "and 1 more" \
  && pass "prose beside a process suite path is not a reason for the process suite" \
  || fail "a process suite path and prose: rc=$RC, pytest lines '$PYTEST', $(cat "$OUT")"

gate --tests-for chaperone/src/chaperone/app.py integration/proc/AGENTS.md
[[ "$RC" == "0" && "$PYTEST" == "$FULL chaperone/tests $VECTORS $ALWAYS" ]] && said "$NO_PROC" \
  && pass "a package and prose under integration/proc/ run the suite of the package only" \
  || fail "a package and process suite prose: rc=$RC, pytest lines '$PYTEST', $(cat "$OUT")"

gate --tests-for integration/proc/AGENTS.md integration/proc/README.md
[[ "$RC" == "0" && -z "$PYTEST" ]] && said "$NO_PROC" \
  && pass "prose under integration/proc/ alone runs no pytest, and the gate says why" \
  || fail "process suite prose alone: rc=$RC, pytest lines '$PYTEST', $(cat "$OUT")"

# A .md under tests/ or fixtures/ is data that a test loads, not prose.
gate --tests-for integration/proc/fixtures/registry/instructions.md
[[ "$RC" == "0" && "$PYTEST" == "$PROC" ]] \
  && pass "a .md of a fixture under integration/proc/ runs the process suite" \
  || fail "a fixture .md under integration/proc: rc=$RC, pytest lines '$PYTEST'"

gate --tests-for '"integration/proc/a\tb.py"'
[[ "$RC" == "0" && "$PYTEST" == "$PROC" ]] \
  && pass "a process suite path that git wrote in quotes picks the process suite" \
  || fail "a quoted process suite path: rc=$RC, pytest lines '$PYTEST'"

gate --tests-for chaperone/src/chaperone/app.py integration/proc/proc_tree.py
[[ "$RC" == "0" && "$PYTEST" == "$FULL chaperone/tests $VECTORS $ALWAYS"$'\n'"$PROC" ]] \
  && pass "a package and the process suite run as two pytest processes" \
  || fail "a package and the process suite: rc=$RC, pytest lines '$PYTEST'"
[[ "$NO_SKIP" == "unset 1" ]] \
  && pass "only the process suite gets CRECHE_PROC_NO_SKIP" \
  || fail "a package and the process suite: CRECHE_PROC_NO_SKIP was '$NO_SKIP'"

gate --tests-for uv.lock integration/proc/proc_tree.py
[[ "$RC" == "0" && "$PYTEST" == "$FULL"$'\n'"$PROC" ]] && said "full suite, for uv.lock" \
  && pass "a path in no package and a process suite path run the full suite, then the process suite" \
  || fail "uv.lock and the process suite: rc=$RC, pytest lines '$PYTEST'"

gate --tests-for rust/Cargo.lock integration/proc/proc_tree.py
[[ "$RC" == "0" && "$PYTEST" == "$PROC" && "$CARGO" == "$RUST_STEPS" ]] \
  && pass "a Rust path and a process suite path run the cargo tests and the process suite" \
  || fail "rust and the process suite: rc=$RC, pytest lines '$PYTEST', cargo ran '$CARGO'"

# The two old suites stay paths in no package (integration/AGENTS.md).
for old in integration/tests/stack.py integration/tests_manager/conftest.py \
  integration/fixtures/stage3-registry/families/chat/family.yaml; do
  gate --tests-for "$old"
  [[ "$RC" == "0" && "$PYTEST" == "$FULL" ]] && said "full suite, for $old" \
    && pass "$old is in no package: full suite, and no process suite" \
    || fail "$old: rc=$RC, pytest lines '$PYTEST'"
done

PYTEST_RC=1 gate --tests-for integration/proc/proc_tree.py
[[ "$RC" != "0" ]] && ! said "quality-gate: PASS" \
  && pass "a failing process suite fails the gate (rc=$RC)" \
  || fail "a failing process suite: rc=$RC, $(cat "$OUT")"

PYTEST_RC=1 gate --tests-for chaperone/src/chaperone/app.py integration/proc/proc_tree.py
[[ "$RC" != "0" && "$PYTEST" == "$FULL chaperone/tests $VECTORS $ALWAYS" ]] \
  && pass "a failing package suite stops the gate before the process suite" \
  || fail "a failing package suite: rc=$RC, pytest lines '$PYTEST'"

gate --tests
[[ "$RC" == "0" && "$PYTEST" == "$FULL" ]] \
  && pass "--tests does not hold the process suite: CI runs it for every change" \
  || fail "--tests: rc=$RC, pytest lines '$PYTEST'"

gate --tests-for
[[ "$RC" == "0" && -z "$PYTEST" && -z "$CARGO" ]] \
  && pass "no path runs no tests" \
  || fail "no path: rc=$RC, pytest line '$PYTEST', cargo ran '$CARGO'"

gate --docs
[[ "$RC" == "0" && "$PYTEST" == "$FULL -m docs" ]] \
  && pass "--docs runs only the tests marked docs, with the full run's flags" \
  || fail "--docs: rc=$RC, pytest line '$PYTEST'"

PYTEST_RC=1 gate --tests-for chaperone/src/chaperone/app.py
[[ "$RC" != "0" ]] && ! said "quality-gate: PASS" \
  && pass "a failing suite fails the gate (rc=$RC)" \
  || fail "a failing suite: rc=$RC, $(cat "$OUT")"

gate --test
[[ "$RC" == "2" && ! -s "$WORK/uv.log" ]] \
  && pass "an unknown flag is refused before any check runs" \
  || fail "--test: rc=$RC, uv ran: $(cat "$WORK/uv.log")"

# --- the hook: pushed refs to changed paths --------------------------------

REPO="$WORK/repo"
ZERO="$(printf '%040d' 0)"
UNFETCHED="$(printf '%040d' 1)"
git init -q -b main "$REPO"
mkdir -p "$REPO/bin/lib" "$REPO/lib"

# The real docs rule, which the hook sources from the tree it pushes.
cp "$HERE/../lib/docsrule.sh" "$REPO/bin/lib/docsrule.sh"

# A gate that knows --tests-for and --docs: it writes its argv, one per line,
# and exits with GATE_RC.
cat > "$REPO/bin/quality-gate.sh" <<'FAKE'
#!/usr/bin/env bash
# A stand-in for the real gate, which knows --tests-for and --docs.
printf '%s\n' "$@" > "$GATE_ARGV"
exit "${GATE_RC:-0}"
FAKE
chmod 0755 "$REPO/bin/quality-gate.sh"

# commit PATH...: appends a line to each PATH, commits them all, prints the sha.
commit() {
  local path

  for path in "$@"; do
    mkdir -p "$REPO/$(dirname -- "$path")"
    echo "$path" >> "$REPO/$path"
  done
  git -C "$REPO" add -A
  git -C "$REPO" -c user.email=t@t -c user.name=t commit -q -m "touch $*"
  git -C "$REPO" rev-parse HEAD
}

# push LINE...: the real hook in $REPO, with LINE... on stdin, the way git
# feeds it. Sets RC, and ARGV to what the gate was given ("none" when the
# hook never called it).
push() {
  OUT="$WORK/hook.out"
  rm -f "$WORK/argv"
  printf '%s\n' "$@" > "$WORK/stdin"
  (cd "$REPO" && GATE_ARGV="$WORK/argv" "$HOOK" origin git@example.invalid:x.git \
    < "$WORK/stdin") > "$OUT" 2>&1
  RC=$?
  ARGV="none"
  if [[ -f "$WORK/argv" ]]; then
    ARGV="$(tr '\n' ' ' < "$WORK/argv")"
    ARGV="${ARGV% }"
  fi
}

# main: BASE, then DOCS after the fork. topic: BASE, PEP, MGR.
BASE="$(commit README.md)"
git -C "$REPO" checkout -q -b topic
PEP="$(commit chaperone/src/chaperone/app.py)"
MGR="$(commit caregiver/src/caregiver/loop.py)"
git -C "$REPO" checkout -q main
DOCS="$(commit docs/later.md)"
git -C "$REPO" update-ref refs/remotes/origin/main "$DOCS"

push "refs/heads/topic $MGR refs/heads/topic $PEP"
[[ "$RC" == "0" && "$ARGV" == "--tests-for caregiver/src/caregiver/loop.py" ]] \
  && pass "an updated branch tests remote sha..local sha" \
  || fail "updated branch: rc=$RC, gate got '$ARGV'"

push "refs/heads/topic $MGR refs/heads/topic $ZERO"
[[ "$ARGV" == "--tests-for caregiver/src/caregiver/loop.py chaperone/src/chaperone/app.py" ]] \
  && pass "a new branch tests from its merge-base, not from origin/main's tip" \
  || fail "new branch: gate got '$ARGV'"

git -C "$REPO" update-ref -d refs/remotes/origin/main
push "refs/heads/topic $MGR refs/heads/topic $ZERO"
[[ "$ARGV" == "--tests" ]] && said "no merge-base" \
  && pass "a new branch with no origin/main runs the full suite" \
  || fail "no origin/main: gate got '$ARGV', $(cat "$OUT")"
git -C "$REPO" update-ref refs/remotes/origin/main "$DOCS"

push "(delete) $ZERO refs/heads/topic $MGR"
[[ "$RC" == "0" && "$ARGV" == "none" ]] && said "deleted" \
  && pass "a deleted branch runs nothing" \
  || fail "delete: rc=$RC, gate got '$ARGV'"

push "(delete) $ZERO refs/heads/old $PEP" \
  "refs/heads/a $MGR refs/heads/a $BASE" \
  "refs/heads/b $MGR refs/heads/b $PEP"
[[ "$ARGV" == "--tests-for caregiver/src/caregiver/loop.py chaperone/src/chaperone/app.py" ]] \
  && pass "several refs test the union of their paths, once each" \
  || fail "several refs: gate got '$ARGV'"

push "refs/heads/same $BASE refs/heads/same $ZERO"
[[ "$RC" == "0" && "$ARGV" == "none" ]] \
  && pass "a push that changes no path runs nothing" \
  || fail "no change: rc=$RC, gate got '$ARGV'"

push "refs/heads/topic $MGR refs/heads/topic $UNFETCHED"
[[ "$ARGV" == "--tests" ]] \
  && pass "a remote sha this clone never fetched runs the full suite" \
  || fail "unknown remote sha: gate got '$ARGV'"

GATE_RC=1 push "refs/heads/topic $MGR refs/heads/topic $PEP"
[[ "$RC" == "1" ]] \
  && pass "the gate's exit code is the push's" \
  || fail "a failing gate: the hook exited $RC"

git -C "$REPO" checkout -q topic
git -C "$REPO" mv chaperone/src/chaperone/app.py lib/app.py
git -C "$REPO" -c user.email=t@t -c user.name=t commit -q -m "move app out of chaperone"
MOVED="$(git -C "$REPO" rev-parse HEAD)"
push "refs/heads/topic $MOVED refs/heads/topic $MGR"
[[ "$ARGV" == "--tests-for chaperone/src/chaperone/app.py lib/app.py" ]] \
  && pass "a rename counts its old path too, so the package it left is tested" \
  || fail "rename: gate got '$ARGV'"

# --- the hook: a docs-only push --------------------------------------------

# versions ROOT MEMBER: pyproject.toml and uv.lock the way the workspace
# writes them, the root package at ROOT and one member at MEMBER. Commits
# both, prints the sha.
versions() {
  printf '[project]\nname = "agent-control-workspace"\nversion = "%s"\n' "$1" \
    > "$REPO/pyproject.toml"
  printf '[[package]]\nname = "%s"\nversion = "%s"\n\n' \
    agent-control-workspace "$1" chaperone "$2" > "$REPO/uv.lock"
  git -C "$REPO" add -A
  git -C "$REPO" -c user.email=t@t -c user.name=t commit -q -m "versions $1 $2"
  git -C "$REPO" rev-parse HEAD
}

git -C "$REPO" checkout -q main
LOCKED="$(versions 0.1.0 0.1.0)"
git -C "$REPO" update-ref refs/remotes/origin/main "$LOCKED"
git -C "$REPO" checkout -q -b prose

PROSE="$(commit docs/rework/runbook.md chaperone/AGENTS.md README.md)"
push "refs/heads/prose $PROSE refs/heads/prose $ZERO"
[[ "$RC" == "0" && "$ARGV" == "--docs" ]] && said "docs only" \
  && pass "a push of nothing but .md files runs only the tests marked docs" \
  || fail "docs only: rc=$RC, gate got '$ARGV', $(cat "$OUT")"

# No number lives in a file since release.yml allocates versions at the
# merge, so no line of pyproject.toml or uv.lock is prose.
BUMPED="$(versions 0.1.1 0.1.0)"
push "refs/heads/prose $BUMPED refs/heads/prose $ZERO"
[[ "$ARGV" == "--tests-for README.md chaperone/AGENTS.md docs/rework/runbook.md pyproject.toml uv.lock" ]] \
  && pass "a root version line that moves is code: the suites run" \
  || fail "docs and a root version line: gate got '$ARGV'"

MEMBER="$(versions 0.1.1 0.1.1)"
push "refs/heads/prose $MEMBER refs/heads/prose $BUMPED"
[[ "$ARGV" == "--tests-for uv.lock" ]] \
  && pass "a member's version in uv.lock is code: the suites run" \
  || fail "a member's version: gate got '$ARGV'"

DEPENDS="$(commit pyproject.toml)"
push "refs/heads/prose $DEPENDS refs/heads/prose $MEMBER"
[[ "$ARGV" == "--tests-for pyproject.toml" ]] \
  && pass "any other pyproject.toml change runs the suites" \
  || fail "a pyproject.toml change: gate got '$ARGV'"

# A fixture's .md is loaded by a test, and a non-.md file under docs/ can be
# one too: docs/rework/nodered/fixtures/ is.
PREVIOUS="$DEPENDS"
for fixture in chaperone/tests/registry/instructions.md integration/fixtures/chat/SKILL.md \
  docs/rework/nodered/legs.json; do
  CHANGED="$(commit "$fixture")"
  push "refs/heads/prose $CHANGED refs/heads/prose $PREVIOUS"
  [[ "$ARGV" == "--tests-for $fixture" ]] \
    && pass "$fixture is not prose: the suites run" \
    || fail "$fixture: gate got '$ARGV'"
  PREVIOUS="$CHANGED"
done

push "refs/heads/prose $PROSE refs/heads/prose $ZERO" \
  "refs/heads/topic $MGR refs/heads/topic $PEP"
[[ "$ARGV" == "--tests-for README.md caregiver/src/caregiver/loop.py chaperone/AGENTS.md docs/rework/runbook.md" ]] \
  && pass "one ref that is not docs only makes the whole push scoped" \
  || fail "docs and code refs: gate got '$ARGV'"

# A push that merged main in counts from main's tip, not from the branch's
# old head: main's own changes were tested where they were made.
git -C "$REPO" checkout -q -b catchup "$LOCKED"
PUSHED="$(commit docs/rework/catchup.md)"
git -C "$REPO" checkout -q main
MOVED_ON="$(commit chaperone/src/chaperone/later.py)"
git -C "$REPO" update-ref refs/remotes/origin/main "$MOVED_ON"
git -C "$REPO" checkout -q catchup
git -C "$REPO" -c user.email=t@t -c user.name=t merge -q --no-edit main
CAUGHT_UP="$(git -C "$REPO" rev-parse HEAD)"
push "refs/heads/catchup $CAUGHT_UP refs/heads/catchup $PUSHED"
[[ "$ARGV" == "--docs" ]] \
  && pass "a push that merged main in counts from main's tip, not its old head" \
  || fail "a push that merged main: gate got '$ARGV'"

OWN="$(commit caregiver/src/caregiver/own.py)"
push "refs/heads/catchup $OWN refs/heads/catchup $CAUGHT_UP"
[[ "$ARGV" == "--tests-for caregiver/src/caregiver/own.py" ]] \
  && pass "the push after that merge counts from its old head again" \
  || fail "after the merge: gate got '$ARGV'"

# A path under rust/ reaches the gate like any other path. The gate, not the
# hook, turns it into cargo steps (bin/lib/rustrule.sh).
RUSTY="$(commit rust/crates/one/src/lib.rs caregiver/src/caregiver/own.py)"
push "refs/heads/catchup $RUSTY refs/heads/catchup $OWN"
[[ "$ARGV" == "--tests-for caregiver/src/caregiver/own.py rust/crates/one/src/lib.rs" ]] \
  && pass "a path under rust/ reaches the gate with the other paths" \
  || fail "a rust path: gate got '$ARGV'"

RUST_PROSE="$(commit rust/AGENTS.md)"
push "refs/heads/catchup $RUST_PROSE refs/heads/catchup $RUSTY"
[[ "$ARGV" == "--docs" ]] \
  && pass "a .md under rust/ is prose: only the tests marked docs" \
  || fail "a .md under rust/: gate got '$ARGV'"

# A worktree cut before the docs rule has no rule to source.
rm "$REPO/bin/lib/docsrule.sh"
push "refs/heads/prose $PROSE refs/heads/prose $ZERO"
[[ "$ARGV" == "--tests-for README.md chaperone/AGENTS.md docs/rework/runbook.md" ]] \
  && pass "a tree with no docs rule runs the scoped suites" \
  || fail "no docs rule: gate got '$ARGV'"
cp "$HERE/../lib/docsrule.sh" "$REPO/bin/lib/docsrule.sh"

# A gate that knows the scoped mode but not the docs one would refuse --docs.
cat > "$REPO/bin/quality-gate.sh" <<'FAKE'
#!/usr/bin/env bash
# A stand-in for a gate that knows --tests-for and predates the docs mode.
printf '%s\n' "$@" > "$GATE_ARGV"
FAKE
push "refs/heads/prose $PROSE refs/heads/prose $ZERO"
[[ "$ARGV" == "--tests-for README.md chaperone/AGENTS.md docs/rework/runbook.md" ]] \
  && pass "a gate that predates --docs runs the scoped suites" \
  || fail "a gate with no --docs: gate got '$ARGV'"

# A worktree cut before the scoped mode still gets its tests: its gate would
# read --tests-for as "lint only" and pass.
cat > "$REPO/bin/quality-gate.sh" <<'FAKE'
#!/usr/bin/env bash
printf '%s\n' "$@" > "$GATE_ARGV"
FAKE
push "refs/heads/topic $MGR refs/heads/topic $PEP"
[[ "$ARGV" == "--tests" ]] \
  && pass "a gate that predates the scoped mode runs the full suite" \
  || fail "old gate: got '$ARGV'"

push "(delete) $ZERO refs/heads/topic $MGR"
[[ "$RC" == "0" && "$ARGV" == "none" ]] \
  && pass "a deleted branch runs nothing, whatever the gate's age" \
  || fail "delete with an old gate: rc=$RC, gate got '$ARGV'"

echo
if [[ "$FAILS" -eq 0 ]]; then
  echo "GATE: PASS"
  exit 0
else
  echo "GATE: FAIL ($FAILS)"
  exit 1
fi
