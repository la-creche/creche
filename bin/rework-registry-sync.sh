#!/usr/bin/env bash
# OPERATOR — pull the registry checkout onto agent-registry's `main`. Runs every
# minute from `registry-sync.timer` on the host, as the operator:
#   /opt/creche/bin/rework-registry-sync.sh
#   /opt/creche/bin/rework-registry-sync.sh --last   # HEAD and its age
#
# WHY IT EXISTS. `caregiver` reads the registry out of the checkout at
# /srv/agents/registry and applies a change within one pass, and it never
# pulls. This script is the one door that does. When nothing pulls, the
# checkout freezes, nothing on the host knows, and every family and
# MCP-server change the operator merges reaches nothing at all.
#
# TWO VERBS AND NOTHING ELSE: `fetch --prune`, then `merge --ff-only`. It
# NEVER rebases, resets, cleans or pushes. The noticeboard commits here
# (`noticeboard/src/noticeboard/registrywrite.py`) and never pushes, so a local
# commit in this checkout is the operator's own unpushed work — a normal Tuesday,
# not corruption. Rebasing it under the operator from a timer is not recoverable, so a
# checkout that has diverged is REPORTED and left exactly as it is.
#
# WHAT `caregiver` SEES DURING A FAST-FORWARD. It polls every 2 seconds
# (`caregiver/src/caregiver/loop.py`, POLL_INTERVAL_S) and its revision is
# a CONTENT hash over every file under `families/`, `mcp/` and `skills/`
# (`family/src/agent_family/registry.py`, `revision_of`), never the git sha.
# Git moves `HEAD` in one atomic ref update, and then writes the working tree
# one file at a time. So a poll that lands mid-checkout can read a MIXED tree
# and hash it to a revision no commit ever had. Three things make that
# affordable, and none of them is this script's lock:
#   1. A mixed tree is usually an INVALID one — a family naming an MCP server
#      whose file has not landed — and `load_registry` answers issues per
#      family. Contract 01 §7 rule 5: one bad file is not a registry outage,
#      and the other ten families are untouched.
#   2. The loop is level-triggered. The next poll, at most two seconds later
#      and after a checkout that takes milliseconds, reads the true revision,
#      sees it moved and converges on the real file. A half-read never
#      latches.
#   3. The cost of the window is at worst one pass acting on a tree that the
#      next pass corrects — a sandbox replaced twice, a LiteLLM key written
#      twice. Both are idempotent by design (contract 05 §5).
# A lock between two processes that do not share it is not a lock, so this
# script does not pretend to hold `caregiver` off. Its lock is for its own
# runs (below).
#
# THE LOCK. `flock -n` on a dedicated descriptor, taken BEFORE the fetch. The
# timer fires every minute and a fetch over ssh can take longer than that, so
# two runs in one checkout is a real shape: one fetching while the other
# merges. `-n` and not a wait, because a run that cannot start now has
# nothing to add — the next firing is a minute away. The lock lives on a
# descriptor rather than in a directory so the kernel drops it when this
# process ends however it ends, and a killed run leaves nothing stale.
#
# WHICH KEY THE FETCH USES. The remote is `git@github.com:<owner>/agent-
# registry.git`, so this is ssh as the operator. Nothing here names a key, exactly
# like `bin/sync-code-corpus.sh`, so ssh takes the operator's default identity.
# This script only FETCHES, so a read-only deploy key serves it completely,
# and a per-repository deploy key can land under it without a change here.
#
# THE UNIT ENVIRONMENT. `registry-sync.service` sets `HOME=%h` and no
# `SSH_AUTH_SOCK`: there is no agent, so ssh reads the key off disk. A
# passphrase on that key would be a prompt, and a oneshot has no terminal, so
# `GIT_TERMINAL_PROMPT=0` and `core.askPass=` below turn a prompt into a fast
# failure instead of a run that hangs until `TimeoutStartSec`.
#
# EXIT CODES. 0 when the checkout ended current (moved, already current, or
# the lock was busy), 1 on any refusal, 2 on a usage error. A refusal turns
# the unit red, which is what the watchdog's fifth check reads.
#
# JOURNAL LINES. One line when `HEAD` MOVED, one line on every REFUSAL, and
# silence otherwise. A line a minute for ever is how a real refusal gets
# missed.
#
# Prerequisites, copy-pasteable:
#   command -v git flock timeout
#   [ -d /srv/agents/registry/.git ]           # the site's bootstrap script
#   ssh -T git@github.com                      # the key that fetches
#
# RUN IT AS THE OPERATOR, never as root. There is no `id -u` refusal — the same call
# `bin/rework-watchdog.sh` makes, and for the same reason: a sync that
# refuses is a sync that is silent, which is the failure this file exists to
# end. The cost of getting it wrong is a root-owned object in the operator's
# checkout, which the next operator-run fetch reports as a refusal.
#
# NOT -e: a refusal must be REPORTED and then end the run on its own terms.
# An abort at the first non-zero command would leave the journal without the
# line that says which refusal it was.
set -uo pipefail

# A TEST SEAM, not a permission. `bin/tests/test_rework_registry_sync.py`
# points every absolute root inside a temp directory. Empty everywhere else,
# including on the host.
TEST_PREFIX="${REGISTRY_SYNC_TEST_PREFIX:-}"

# The REAL registry checkout. `caregiver` watches it, the trigger door reads
# it, and the noticeboard commits into it.
REGISTRY="$TEST_PREFIX/srv/agents/registry"
STATE_ROOT="$TEST_PREFIX/srv/agents/state/rework"

# This script's own two files, 0700 directory and 0600 files, both under
# `umask` and never a chmod after the write (bin/AGENTS.md, Secrets).
SYNC_DIR="$STATE_ROOT/registry-sync"
LOCK_FILE="$SYNC_DIR/lock"
# Written on every run that ENDED WELL. `bin/rework-watchdog.sh`'s fifth
# check reads its AGE, which is how a timer that stopped firing is found
# without dialling GitHub.
STAMP_FILE="$SYNC_DIR/last-success"
# The descriptor the lock lives on. Spelled as a LITERAL in both places
# below, because bash 3.2 cannot redirect to a descriptor held in a variable
# and `eval` is not worth one number. Change one, change the other.
LOCK_FD=9

REMOTE=origin
BRANCH=main
UPSTREAM="$REMOTE/$BRANCH"

# The one file `caregiver` would write INTO this checkout, through
# `live_manifest.publish` (`caregiver/src/caregiver/live_manifest.py`),
# which nothing calls today (contract 06 §3.4). It is derived from root's
# ledger, so this script may discard its copy, and it must: a derived file
# may never hold a family change up. See `clear_derived`.
DERIVED_FILE=live-manifest.json

# One ceiling per call. A black-holed remote otherwise holds the run until
# the unit's own `TimeoutStartSec`, and the lock with it.
FETCH_TIMEOUT_S=60
GIT_TIMEOUT_S=30

# A git message reaches the journal. Bounded, because it carries a remote's
# words (invariants 12 and 14: everything across a process boundary is
# untrusted input).
MAX_DETAIL=200

say() { printf '%s\n' "$*"; }

# Every refusal says the same word, so one grep finds all of them.
refuse() {  # refuse <reason>
  say "registry: REFUSED $*"
  exit 1
}

# A parent's leaked GIT_DIR or GIT_WORK_TREE redirects `-C <dir>` at a
# different repository entirely, and a unit inherits whatever started it.
# `flowsync/src/agent_flowsync/gitenv.py` drops the same five for the same
# reason.
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR GIT_OBJECT_DIRECTORY
# git must never stop for a prompt in a unit that has no terminal.
export GIT_TERMINAL_PROMPT=0

# Git's own configuration on the command line, the way
# `bin/rework-cutover.sh`'s `tree_git` does it and for the same reason: this
# checkout is untrusted input. Anything that can write a family file into it
# can write `.git/config` and `.git/hooks/post-merge`, and a fast-forward
# runs hooks. Each word closes one command-shaped setting
# (`handover/src/handover/executor/source.py` argues the full list):
#   safe.directory   one exact path, so a checkout that a root visit left
#                    root-owned is a refusal about git's answer rather than
#                    about "dubious ownership"
#   core.hooksPath   post-merge and post-checkout, which this script runs
#   core.fsmonitor   the one setting that runs a command on an index refresh
#   core.pager       a pager needs a terminal, and a unit has none
#   core.editor      one interactive verb away
#   core.askPass     the GUI asker, which would hang the oneshot
#   protocol.ext     `ext::<command>` is a URL that IS a command
# `core.sshCommand=false` is deliberately NOT here: the remote is ssh and
# this script's whole job is to reach it. The executor can forbid it because
# it clones `--local` and never dials anything.
reg_git() {  # reg_git <seconds> <git args...>
  local seconds="$1"
  shift
  timeout "$seconds" git \
    -c "safe.directory=$REGISTRY" \
    -c core.hooksPath=/dev/null \
    -c core.fsmonitor=false \
    -c core.pager=cat \
    -c core.editor=false \
    -c core.askPass= \
    -c protocol.ext.allow=never \
    -C "$REGISTRY" "$@"
}

short() {  # short <rev>
  reg_git "$GIT_TIMEOUT_S" rev-parse --short "$1" 2>/dev/null
}

# git's stderr, flattened to one bounded line for the journal.
detail() {  # detail <file>
  head -c "$MAX_DETAIL" "$1" 2>/dev/null | tr '\n' ' ' | tr -s ' '
}

# --- what `bin/rework-cutover.sh status` prints -----------------------------

# `HEAD` and its age, in one line, the way `bin/rework-watchdog.sh --last`
# answers with its verdict. Git computes the age itself (`--date=relative`),
# so no `date` parsing happens here and the line reads the same on both
# platforms.
if [[ "${1:-}" == "--last" ]]; then
  if [[ ! -d "$REGISTRY/.git" ]]; then
    say "unknown (no checkout at $REGISTRY)"
    exit 0
  fi

  HEAD_LINE="$(reg_git "$GIT_TIMEOUT_S" log -1 --format='%h (%cd)' --date=relative 2>/dev/null)"
  LAST_SYNC="never"
  [[ -r "$STAMP_FILE" ]] && LAST_SYNC="$(head -c 64 "$STAMP_FILE" 2>/dev/null | tr -d '\n')"
  say "${HEAD_LINE:-unknown (git could not read $REGISTRY)}, last successful sync $LAST_SYNC"
  exit 0
fi

if [[ $# -gt 0 ]]; then
  say "usage: rework-registry-sync.sh [--last]"
  exit 2
fi

# --- one run at a time ------------------------------------------------------

( umask 077 && mkdir -p "$SYNC_DIR" ) || refuse "cannot create $SYNC_DIR"

# The descriptor is opened first and locked second, so the lock and the file
# are the same object for every run. The file is created under `umask 077`
# first, because `exec 9>` would otherwise create it under the unit's own
# mask (bin/AGENTS.md, Secrets: never a chmod after the write).
( umask 077 && : >> "$LOCK_FILE" ) || refuse "cannot create $LOCK_FILE"
exec 9>"$LOCK_FILE" || refuse "cannot open $LOCK_FILE"

if ! flock -n "$LOCK_FD"; then
  # Not a refusal: the run that holds the lock is doing this run's work, and
  # the unit's `TimeoutStartSec` is what stops one that wedges.
  say "registry: another run holds the lock, skipping this firing"
  exit 0
fi

# --- the checkout -----------------------------------------------------------

[[ -d "$REGISTRY/.git" ]] || refuse "no checkout at $REGISTRY"

ON="$(reg_git "$GIT_TIMEOUT_S" symbolic-ref --short HEAD 2>/dev/null)"
[[ -n "$ON" ]] || refuse "$REGISTRY is not on a branch (a detached HEAD): git -C $REGISTRY status"
[[ "$ON" == "$BRANCH" ]] || refuse "$REGISTRY is on $ON, not $BRANCH"

BEFORE="$(short HEAD)"
[[ -n "$BEFORE" ]] || refuse "git cannot read HEAD in $REGISTRY"

# --- the fetch --------------------------------------------------------------

ERRORS="$(mktemp)" || refuse "cannot make a temporary file"
trap 'rm -f "$ERRORS"' EXIT

# `--prune`, so a branch deleted upstream stops answering here too. This is
# the one call that leaves the host, and the one a wrong key fails.
if ! reg_git "$FETCH_TIMEOUT_S" fetch --prune "$REMOTE" >/dev/null 2>"$ERRORS"; then
  refuse "fetch from $REMOTE failed: $(detail "$ERRORS")"
fi

TARGET="$(short "$UPSTREAM")"
[[ -n "$TARGET" ]] || refuse "$REMOTE has no $BRANCH after the fetch"

if [[ "$BEFORE" == "$TARGET" ]]; then
  # Silence. Nothing moved, and nothing refused.
  ( umask 077 && date -u +%Y-%m-%dT%H:%M:%SZ > "$STAMP_FILE" ) \
    || say "registry: WARNING could not write $STAMP_FILE"
  exit 0
fi

# --- may this checkout move? ------------------------------------------------

# A local commit. `--is-ancestor` exits 0 when HEAD is behind or equal, and
# non-zero the moment this checkout carries something upstream does not.
if ! reg_git "$GIT_TIMEOUT_S" merge-base --is-ancestor HEAD "$UPSTREAM" >/dev/null 2>&1; then
  refuse "$REGISTRY has local commit(s): $BEFORE is not an ancestor of $UPSTREAM. \
The noticeboard commits here and never pushes. Push them, or move them aside, by hand"
fi

# The one derived file, cleared BEFORE the working tree is judged. Two
# shapes, both measured against real git:
#   1. TRACKED and modified, and the incoming commits touch it too: git
#      refuses the whole merge with "Your local changes would be
#      overwritten". Restoring HEAD's copy is safe because the file is
#      derived from root's ledger.
#   2. UNTRACKED, and the incoming commits ADD it: git refuses with "The
#      following untracked working tree files would be overwritten". This is
#      the shape that is live TODAY — agent-registry does not track the file
#      yet — so the first commit that adds it upstream would otherwise wedge
#      every family change behind a file nobody edited.
# The alternative is `update-index --assume-unchanged`, which lies to git in
# a way nothing on the host would ever show again.
clear_derived() {
  if reg_git "$GIT_TIMEOUT_S" ls-files --error-unmatch "$DERIVED_FILE" >/dev/null 2>&1; then
    reg_git "$GIT_TIMEOUT_S" diff --quiet HEAD -- "$DERIVED_FILE" >/dev/null 2>&1 && return 0

    reg_git "$GIT_TIMEOUT_S" checkout -- "$DERIVED_FILE" >/dev/null 2>&1 \
      || refuse "cannot restore $DERIVED_FILE in $REGISTRY"
    say "registry: restored $DERIVED_FILE from HEAD before the merge (it is derived from the ledger)"
    return 0
  fi

  # Untracked here. It only blocks the merge when the target carries it.
  [[ -e "$REGISTRY/$DERIVED_FILE" ]] || return 0
  reg_git "$GIT_TIMEOUT_S" cat-file -e "$UPSTREAM:$DERIVED_FILE" >/dev/null 2>&1 || return 0

  rm -f "$REGISTRY/$DERIVED_FILE" \
    || refuse "cannot remove the untracked $DERIVED_FILE in $REGISTRY"
  say "registry: removed an untracked $DERIVED_FILE that $UPSTREAM brings in"
}

clear_derived

# A dirty tracked file. STRICTER THAN GIT on purpose: git fast-forwards
# straight over a dirty path the incoming commits do not touch (measured,
# `bin/tests/test_rework_registry_sync.py`), which would leave somebody's
# half-finished edit sitting on top of a tree that moved under it. Reported
# and left alone, like a local commit.
DIRTY="$(reg_git "$GIT_TIMEOUT_S" diff --name-only HEAD -- 2>/dev/null | head -5 | tr '\n' ' ')"
if [[ -n "$DIRTY" ]]; then
  refuse "$REGISTRY has modified tracked file(s): ${DIRTY% }. \
Commit them, or throw them away, by hand"
fi

# --- the move ---------------------------------------------------------------

COUNT="$(reg_git "$GIT_TIMEOUT_S" rev-list --count "HEAD..$UPSTREAM" 2>/dev/null)"
[[ -n "$COUNT" ]] || COUNT="?"

if ! reg_git "$GIT_TIMEOUT_S" merge --ff-only "$UPSTREAM" >/dev/null 2>"$ERRORS"; then
  refuse "fast-forward to $TARGET failed: $(detail "$ERRORS")"
fi

AFTER="$(short HEAD)"
say "registry: $BEFORE -> $AFTER, $COUNT commit(s)"

( umask 077 && date -u +%Y-%m-%dT%H:%M:%SZ > "$STAMP_FILE" ) \
  || say "registry: WARNING could not write $STAMP_FILE"

exit 0
