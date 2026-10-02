#!/usr/bin/env bash
# Pins what a push tests. githooks/pre-push turns the pushed refs into the
# paths they change, and `bin/quality-gate.sh --tests-for` turns those paths
# into the suites of the packages they touch. A path in no package runs the
# full suite, and a deleted branch runs nothing. A push that merged main in
# counts from main's fork point, so main's own changes do not count. A push
# of nothing but docs runs only the tests marked docs (bin/lib/docsrule.sh).
# CI runs the full suite for any other change and is the merge gate: the
# scope decides whether a regression in the package just changed is caught
# before the push or only in CI.
#
# Runs the real gate and the real hook. `uv` is a fake on PATH that writes
# its argv to a file, and the hook runs in a throwaway repository whose
# bin/quality-gate.sh is a fake that writes its own. Every exit code comes
# straight from `$?`, never through a pipe.
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
  exit "${PYTEST_RC:-0}"
fi
FAKE
chmod 0755 "$WORK/bin/uv"

# gate ARG...: the real gate, with the fake uv. Sets RC, and PYTEST to the
# pytest line uv was asked for ("" when pytest never ran).
gate() {
  OUT="$WORK/gate.out"
  : > "$WORK/uv.log"
  PATH="$WORK/bin:$PATH" UV_LOG="$WORK/uv.log" "$GATE" "$@" > "$OUT" 2>&1
  RC=$?
  PYTEST="$(grep '^run pytest' "$WORK/uv.log")"
}

# The full run is the baseline: a scoped run must use the same flags.
gate --tests
FULL="$PYTEST"
[[ "$RC" == "0" && -n "$FULL" ]] \
  && pass "--tests runs pytest ($FULL)" \
  || fail "--tests: rc=$RC, pytest line '$FULL'"

gate
[[ "$RC" == "0" && -z "$PYTEST" ]] \
  && pass "no flag (pre-commit) runs no tests" \
  || fail "no flag: rc=$RC, pytest line '$PYTEST'"

gate --tests-for pep/src/agent_pep/app.py pep/README.md
[[ "$RC" == "0" && "$PYTEST" == "$FULL pep/tests $ALWAYS" ]] \
  && pass "one package runs its suite, with the full run's flags" \
  || fail "one package: rc=$RC, pytest line '$PYTEST'"
said "pep/tests, for pep/src/agent_pep/app.py and 1 more" \
  && pass "the gate says which path picked the suite" \
  || fail "no reason line for pep/tests: $(cat "$OUT")"

gate --tests-for managerd/tests/managerd_mws_registry/registry.yaml
[[ "$PYTEST" == "$FULL managerd/tests $ALWAYS" ]] \
  && pass "a test fixture directory counts as its package" \
  || fail "a fixture path: pytest line '$PYTEST'"

gate --tests-for pep/pyproject.toml managerd/AGENTS.md bin/lib/docsrule.sh
SCOPE="${PYTEST#"$FULL"}"
[[ "$RC" == "0" && "$PYTEST" == "$FULL "* ]] \
  && [[ "$(words "$SCOPE")" == "$(words "bin/tests managerd/tests pep/tests")" ]] \
  && pass "three packages run three suites, bin/tests already holding $ALWAYS" \
  || fail "three packages: rc=$RC, pytest line '$PYTEST'"

for stray in uv.lock pyproject.toml docs/host-release.md .github/workflows/gate.yml \
  githooks/pre-push supervisor/package.json README.md pep-old/src/x.py; do
  gate --tests-for pep/src/agent_pep/app.py "$stray"
  [[ "$RC" == "0" && "$PYTEST" == "$FULL" ]] && said "full suite, for $stray" \
    && pass "$stray is in no package: full suite" \
    || fail "$stray: rc=$RC, pytest line '$PYTEST'"
done

gate --tests-for
[[ "$RC" == "0" && -z "$PYTEST" ]] \
  && pass "no path runs no tests" \
  || fail "no path: rc=$RC, pytest line '$PYTEST'"

gate --docs
[[ "$RC" == "0" && "$PYTEST" == "$FULL -m docs" ]] \
  && pass "--docs runs only the tests marked docs, with the full run's flags" \
  || fail "--docs: rc=$RC, pytest line '$PYTEST'"

PYTEST_RC=1 gate --tests-for pep/src/agent_pep/app.py
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
PEP="$(commit pep/src/agent_pep/app.py)"
MGR="$(commit managerd/src/agent_managerd/loop.py)"
git -C "$REPO" checkout -q main
DOCS="$(commit docs/later.md)"
git -C "$REPO" update-ref refs/remotes/origin/main "$DOCS"

push "refs/heads/topic $MGR refs/heads/topic $PEP"
[[ "$RC" == "0" && "$ARGV" == "--tests-for managerd/src/agent_managerd/loop.py" ]] \
  && pass "an updated branch tests remote sha..local sha" \
  || fail "updated branch: rc=$RC, gate got '$ARGV'"

push "refs/heads/topic $MGR refs/heads/topic $ZERO"
[[ "$ARGV" == "--tests-for managerd/src/agent_managerd/loop.py pep/src/agent_pep/app.py" ]] \
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
[[ "$ARGV" == "--tests-for managerd/src/agent_managerd/loop.py pep/src/agent_pep/app.py" ]] \
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
git -C "$REPO" mv pep/src/agent_pep/app.py lib/app.py
git -C "$REPO" -c user.email=t@t -c user.name=t commit -q -m "move app out of pep"
MOVED="$(git -C "$REPO" rev-parse HEAD)"
push "refs/heads/topic $MOVED refs/heads/topic $MGR"
[[ "$ARGV" == "--tests-for lib/app.py pep/src/agent_pep/app.py" ]] \
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
    agent-control-workspace "$1" agent-pep "$2" > "$REPO/uv.lock"
  git -C "$REPO" add -A
  git -C "$REPO" -c user.email=t@t -c user.name=t commit -q -m "versions $1 $2"
  git -C "$REPO" rev-parse HEAD
}

git -C "$REPO" checkout -q main
LOCKED="$(versions 0.1.0 0.1.0)"
git -C "$REPO" update-ref refs/remotes/origin/main "$LOCKED"
git -C "$REPO" checkout -q -b prose

PROSE="$(commit docs/rework/runbook.md pep/AGENTS.md README.md)"
push "refs/heads/prose $PROSE refs/heads/prose $ZERO"
[[ "$RC" == "0" && "$ARGV" == "--docs" ]] && said "docs only" \
  && pass "a push of nothing but .md files runs only the tests marked docs" \
  || fail "docs only: rc=$RC, gate got '$ARGV', $(cat "$OUT")"

# No number lives in a file since release.yml allocates versions at the
# merge, so no line of pyproject.toml or uv.lock is prose.
BUMPED="$(versions 0.1.1 0.1.0)"
push "refs/heads/prose $BUMPED refs/heads/prose $ZERO"
[[ "$ARGV" == "--tests-for README.md docs/rework/runbook.md pep/AGENTS.md pyproject.toml uv.lock" ]] \
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
for fixture in pep/tests/registry/instructions.md integration/fixtures/chat/SKILL.md \
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
[[ "$ARGV" == "--tests-for README.md docs/rework/runbook.md managerd/src/agent_managerd/loop.py pep/AGENTS.md" ]] \
  && pass "one ref that is not docs only makes the whole push scoped" \
  || fail "docs and code refs: gate got '$ARGV'"

# A push that merged main in counts from main's tip, not from the branch's
# old head: main's own changes were tested where they were made.
git -C "$REPO" checkout -q -b catchup "$LOCKED"
PUSHED="$(commit docs/rework/catchup.md)"
git -C "$REPO" checkout -q main
MOVED_ON="$(commit pep/src/agent_pep/later.py)"
git -C "$REPO" update-ref refs/remotes/origin/main "$MOVED_ON"
git -C "$REPO" checkout -q catchup
git -C "$REPO" -c user.email=t@t -c user.name=t merge -q --no-edit main
CAUGHT_UP="$(git -C "$REPO" rev-parse HEAD)"
push "refs/heads/catchup $CAUGHT_UP refs/heads/catchup $PUSHED"
[[ "$ARGV" == "--docs" ]] \
  && pass "a push that merged main in counts from main's tip, not its old head" \
  || fail "a push that merged main: gate got '$ARGV'"

OWN="$(commit managerd/src/agent_managerd/own.py)"
push "refs/heads/catchup $OWN refs/heads/catchup $CAUGHT_UP"
[[ "$ARGV" == "--tests-for managerd/src/agent_managerd/own.py" ]] \
  && pass "the push after that merge counts from its old head again" \
  || fail "after the merge: gate got '$ARGV'"

# A worktree cut before the docs rule has no rule to source.
rm "$REPO/bin/lib/docsrule.sh"
push "refs/heads/prose $PROSE refs/heads/prose $ZERO"
[[ "$ARGV" == "--tests-for README.md docs/rework/runbook.md pep/AGENTS.md" ]] \
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
[[ "$ARGV" == "--tests-for README.md docs/rework/runbook.md pep/AGENTS.md" ]] \
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
