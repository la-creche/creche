#!/usr/bin/env bash
# OPERATOR rework outage alarm. Runs every minute from
# `agent-rework-watchdog.timer` on the host, as the operator:
#   /opt/agent-control/bin/rework-watchdog.sh
#   /opt/agent-control/bin/rework-watchdog.sh --last   # print the verdict
#
# WHY IT EXISTS. A rework service can die in silence: a SIGHUP that arrives
# before the PEP's handler is installed is a clean exit to systemd, and a
# unit past its start limit is never started again. Every status document
# keeps saying `in_sync` meanwhile, because a dead writer changes nothing.
#
# So this watchdog SHARES NO FATE with what it watches. It is a oneshot
# under the user manager, it holds no rework code, it dials no rework
# service for permission, and it works when all five units are dead.
# `managerd`'s own `pep_unreachable` fault (contract 05 §3.3) covers the
# PEP from INSIDE `managerd`, which is no help when `managerd` is the dead
# one. This is the outside half.
#
# FIVE CHECKS, each with its own timeout, none needing a credential:
#   1. pep       GET :8300/healthz on the host's LAN address must answer 200.  5 s
#                Loopback answers nothing on that host: every service
#                binds the LAN address (README).
#   2. managerd  the newest `written_at` under
#                /srv/agents/state/rework/families/*/status.json must be
#                younger than 180 s. Contract 05 §2 rule 4 rewrites each
#                document at least every 30 s, and §2 rule 5 makes a reader
#                call one older than 90 s `unknown`, so 180 s is two of
#                those and cannot fire on a manager that is running.  5 s
#   3. sessiond  the Unix socket must exist AND answer. An unauthenticated
#                GET returns 401, which proves a listener and needs no
#                token (invariant 13).  5 s
#   4. units     `systemctl --user is-failed` must be false for each of the
#                five rework user units.  5 s
#   5. registry  `registry-sync.service` must have ended well inside the
#                last 5 minutes. It is what pulls /srv/agents/registry,
#                and a fleet that ignores every merge looks exactly like a
#                quiet morning from in here.  5 s
#
# A CHECK MUST FAIL TWICE IN A ROW BEFORE IT CAN PUSH. `agent-control-deploy`
# and the release visit restart the PEP on purpose, and its `/healthz` is
# away for 10 to 15 s each time. A one-minute probe lands in that gap about
# one time in four, and the operator would get "rework DOWN" for a restart
# they ordered themselves. So a lone failure is recorded and named in the journal
# (`<key>: <detail>, 1 of 2`) but never pushed: only a SECOND consecutive
# failure of the SAME check confirms it. A 10-15 s blip cannot land on two
# probes a minute apart, so this removes the false alarm without hiding a real
# one.
#
# This applies to all five checks alike, including managerd's, which already
# waits for 180 s of staleness before its first failed reading. Requiring a
# second consecutive stale reading adds up to one more minute on top (180 to
# 240 s worst case) before a push. That is still far short of an outage of
# hours, which is what this script exists to catch, and a normal managerd
# restart is fast enough that it never makes the status document 180 s
# stale even once, so the extra minute costs nothing a real outage would
# notice.
#
# ONE PUSH PER OUTAGE. Past confirmation, it notifies on a CHANGE of verdict
# only, from a state file: one "DOWN" when something is confirmed down, one
# "recovered" when it comes back — recovery needs no second reading, because
# missing an early recovery costs nothing and delaying one costs a false
# "still down" card. Never one push a minute for five hours. A notice that
# cannot be sent is logged and the CONFIRMED verdict is left alone, so the
# next run tries the push again.
#
# It exits non-zero while ANY check has failed this run, confirmed or not,
# so `systemctl --user status agent-rework-watchdog` shows trouble at once —
# a lone unconfirmed failure still turns the unit red, it just does not
# reach the operator's phone until it repeats.
#
# WHERE THE TWO BEARERS COME FROM. `APPROVAL_URL` and `APPROVAL_TOKEN` are
# read from /srv/agents/state/rework/hooks.env, which `bin/rework-cutover.sh
# up` writes and owns. The OLD system's /srv/agents/state/materializer/env
# is a FALLBACK: nothing writes it any more, and every read of it puts one
# DEPRECATED line in the journal. `bin/sbx-drift-check.sh` reads the same
# two values the same way.
#
# Prerequisites, copy-pasteable:
#   command -v curl date grep find systemctl mktemp
#   [ -r /srv/agents/state/rework/hooks.env ]   # APPROVAL_URL/APPROVAL_TOKEN
#   [ -r /etc/agent-control/site.env ]          # AGENT_LAN_ADDRESS
#
# RUN IT AS THE OPERATOR, never as root. There is no `id -u` refusal, because a
# watchdog that refuses is a watchdog that is silent, which is the failure
# this file exists to end (`bin/sbx-drift-check.sh` makes the same call).
# The cost of getting it wrong is that root owns the verdict file, the operator's
# runs can no longer write it, and every run then reads the verdict as
# "nothing known" and pushes again. `remember` says so in the journal when
# it cannot write. `chown -R <operator>: /srv/agents/state/rework/watchdog` is
# the repair.
#
# NOT -e: a failed check must COUNT, not abort the run. A watchdog that
# stopped at the first thing it found would report one outage out of five.
set -uo pipefail

# A TEST SEAM, not a permission. `bin/tests/test_rework_watchdog.py` points
# every absolute root inside a temp directory. Empty everywhere else,
# including on the host.
TEST_PREFIX="${WATCHDOG_TEST_PREFIX:-}"

STATE_ROOT="$TEST_PREFIX/srv/agents/state/rework"
FAMILIES_DIR="$STATE_ROOT/families"
SOCKET="$STATE_ROOT/sock/sessiond.sock"
# This script's own memory: two lines, 0700 directory, 0600 file, both under
# `umask`, never a chmod after the write (bin/AGENTS.md, Secrets). Line 1 is the
# last CONFIRMED verdict's key (what `--last` prints and a push claims).
# Line 2 is the RAW set of checks that failed on that same run, confirmed or
# not — written every run, because a check that failed once must be
# remembered even when nothing was confirmed, or the next run could never
# tell "1 of 2" from a fresh failure. It is operator-written input to itself,
# not a stranger's, but it is still read bounded (`bin/AGENTS.md`'s
# byte-cap-before-parse habit): a `head -c` cap, never a `source`.
WATCH_DIR="$STATE_ROOT/watchdog"
VERDICT_FILE="$WATCH_DIR/verdict"
STATE_READ_CAP=4096

# The rework's own hook credentials, and the old system's env file behind
# them as a fallback. `bin/lib/envfile.sh` holds the reader and the
# deprecation notice, so this script, the drift check and the cutover all
# read them the same way.
HOOKS_ENV="$STATE_ROOT/hooks.env"
DEPRECATED_ENV="$TEST_PREFIX/srv/agents/state/materializer/env"
. "$(dirname -- "${BASH_SOURCE[0]}")/lib/envfile.sh"

# The deployment's site file (release/src/agent_release/site.py).
# `AGENT_SITE_FILE` names another one, which is how a test points at a fixture.
SITE_FILE="${AGENT_SITE_FILE:-/etc/agent-control/site.env}"

# Services bind the LAN address. Loopback answers nothing on this host.
# `WATCHDOG_PEP_URL` wins; otherwise the PEP is on the site's address.
PEP_PORT=8300
PEP_URL="${WATCHDOG_PEP_URL:-}"
HEALTH_PATH=/healthz
HTTP_OK=200
HTTP_UNAUTHORIZED=401

# The five ALWAYS-ON user units `bin/rework-cutover.sh up` installs, in start
# order. A long-running unit added there is added here. The two oneshots it
# also installs are not: this alarm's own, and `registry-sync.service`, which
# check 5 below reads by its RESULT rather than by `is-failed` — a oneshot
# that is meant to exit says more about itself that way.
UNITS="agent-sessiond.service agent-managerd.service agent-door-owui.service \
agent-trigger-webhooks.service creche-noticeboard.service"

# Contract 05 §2 rule 5's 90 s, doubled. A document this old means nobody
# who is running has looked.
STALE_AFTER_S=180

# The registry's door. `bin/rework-cutover.sh up` installs both
# units and `bin/rework-registry-sync.sh` owns the stamp's path; it is
# spelled once more here because this script must work when that one does
# not run at all.
SYNC_SERVICE=registry-sync.service
SYNC_TIMER=registry-sync.timer
SYNC_STAMP="$STATE_ROOT/registry-sync/last-success"
# The timer fires every minute, so five is four missed firings. That is well
# past a slow fetch and well inside the brief's "tell the operator within five
# minutes". In MINUTES, because `find -mmin` is what reads it.
SYNC_STALE_AFTER_MIN=5
# What `systemctl show -p Result` says about a run that ended well.
UNIT_RESULT_OK=success

# One ceiling per check, so a hung mount or a black-holed address costs the
# run five seconds and not the minute until the next firing.
CHECK_TIMEOUT_S=5
# The notice itself, which reaches Node-RED over the LAN.
NOTICE_TIMEOUT_S=10

# The verdict when nothing is down. A word, so the state file is readable.
ALL_WELL=up

# Fixed ids, so a repeat REPLACES the card on the phone rather than adding
# one. Different from `bin/sbx-drift-check.sh`'s pair on purpose: the two
# alarms must not overwrite each other.
NOTICE_JOB_ID=yyyyyyyyyyyyyyyyyyyyyyyyyy
NOTICE_GATE_ID=0000000000000001
NOTICE_AGENT=rework-watchdog

say() { printf '%s\n' "$*"; }

# --- the last verdict, for `bin/rework-cutover.sh status` -------------------

if [[ "${1:-}" == "--last" ]]; then
  if [[ -r "$VERDICT_FILE" ]]; then
    head -1 "$VERDICT_FILE"
    exit 0
  fi

  say "unknown (no run yet: $VERDICT_FILE)"
  exit 0
fi

if [[ $# -gt 0 ]]; then
  say "usage: rework-watchdog.sh [--last]"
  exit 2
fi

# --- where the PEP is -------------------------------------------------------

# After `--last`, which dials nothing. No default: a default would be
# somebody's host, and probing it would alarm about the wrong machine.
if [[ -z "$PEP_URL" ]]; then
  LAN_ADDRESS="$(envfile_value "$SITE_FILE" AGENT_LAN_ADDRESS)"
  if [[ -z "$LAN_ADDRESS" ]]; then
    printf 'rework-watchdog: no AGENT_LAN_ADDRESS in %s\n' "$SITE_FILE" >&2
    exit 1
  fi

  PEP_URL="http://$LAN_ADDRESS:$PEP_PORT"
fi

# --- what the last run found, read before this run's checks -----------------

# Bounded, never `source`d: this file is this script's own past output, not
# a stranger's, but a byte cap before the parse is the habit every other
# reader of untrusted-shaped input in this repo keeps (bin/AGENTS.md,
# release/AGENTS.md's trust rules). Two `head -c` reads of a two-line file
# cost nothing measurable.
STATE_BLOB=""
[[ -r "$VERDICT_FILE" ]] && STATE_BLOB="$(head -c "$STATE_READ_CAP" "$VERDICT_FILE" 2>/dev/null)"
STORED_VERDICT="$(printf '%s\n' "$STATE_BLOB" | sed -n '1p')"
STORED_RAW="$(printf '%s\n' "$STATE_BLOB" | sed -n '2p')"

# Was <key> among the checks that failed on the PREVIOUS run too? That is
# the whole of "two consecutive failures": no count to cap, because a check
# that recovers even once must start back at "1 of 2" on its next failure.
was_raw_down() {  # was_raw_down <key>
  case " $STORED_RAW " in
    *" $1 "*) return 0 ;;
    *) return 1 ;;
  esac
}

# --- what this run found -----------------------------------------------------

# Two pairs of parallel strings, not arrays: bash 3.2 on macOS has no
# associative arrays and the test runs there (bin/AGENTS.md).
# DOWN_KEYS/DOWN_LINES are every check that failed THIS run, confirmed or
# not — this is what decides the exit code and what next run compares
# against. CONFIRMED_KEYS/CONFIRMED_LINES are the subset that ALSO failed
# last run: only these decide the verdict and a push. Every *_LINES value
# carries detail that may move between runs (an age in seconds does) and
# therefore must never decide anything by itself.
DOWN_KEYS=""
DOWN_LINES=""
CONFIRMED_KEYS=""
CONFIRMED_LINES=""

note_down() {  # note_down <key> <line>
  DOWN_KEYS="$DOWN_KEYS $1"
  DOWN_LINES="$DOWN_LINES$2"$'\n'

  if was_raw_down "$1"; then
    CONFIRMED_KEYS="$CONFIRMED_KEYS $1"
    CONFIRMED_LINES="$CONFIRMED_LINES$2"$'\n'
    say "DOWN: $2"
    return 0
  fi

  say "$1: $2, 1 of 2"
}

# --- 1. the PEP ---------------------------------------------------------------

check_pep() {
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' -m "$CHECK_TIMEOUT_S" \
    "$PEP_URL$HEALTH_PATH" 2>/dev/null)"

  if [[ "$code" == "$HTTP_OK" ]]; then
    say "ok: the PEP answers $HEALTH_PATH"
    return 0
  fi

  note_down pep "the PEP did not answer $HTTP_OK at $PEP_URL$HEALTH_PATH (got ${code:-nothing})"
}

# --- 2. managerd is publishing ------------------------------------------------

# RFC 3339 (`2026-09-21T14:19:02Z`, managerd/src/agent_managerd/clock.py) to
# seconds since the epoch. GNU `date -d` first, BSD `date -j -f` second: the
# script runs on the host, which is GNU, and its test runs on a Mac, which
# is not. The GNU form fails with "illegal option -- d" on BSD and prints
# nothing, so the fall-through is clean.
epoch_of() {  # epoch_of <RFC 3339 stamp>
  date -u -d "$1" +%s 2>/dev/null && return 0

  date -u -j -f '%Y-%m-%dT%H:%M:%SZ' "$1" +%s 2>/dev/null
}

# The `written_at` of one status document, or nothing. `grep`, not a JSON
# parser: this script must run when every component tree is mid-release.
stamp_of() {  # stamp_of <status.json>
  grep -o '"written_at"[[:space:]]*:[[:space:]]*"[^"]*"' "$1" 2>/dev/null \
    | head -1 \
    | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z'
}

check_managerd() {
  local documents newest now stamp seconds age
  # `find` under `timeout`, because a state root on a hung mount would
  # otherwise hold this run until the next firing.
  documents="$(timeout "$CHECK_TIMEOUT_S" \
    find "$FAMILIES_DIR" -maxdepth 2 -name status.json 2>/dev/null)"

  if [[ -z "$documents" ]]; then
    note_down managerd "no status document under $FAMILIES_DIR: managerd has published nothing"
    return 0
  fi

  # The NEWEST one. The stamps are fixed width and UTC, so they compare as
  # plain strings and no clock is needed to order them (clock.py).
  newest=""
  while IFS= read -r path; do
    [[ -n "$path" ]] || continue
    stamp="$(stamp_of "$path")"
    [[ -n "$stamp" ]] || continue
    [[ "$stamp" > "$newest" ]] && newest="$stamp"
  done <<< "$documents"

  if [[ -z "$newest" ]]; then
    note_down managerd "no readable written_at under $FAMILIES_DIR"
    return 0
  fi

  seconds="$(epoch_of "$newest")"
  if [[ -z "$seconds" ]]; then
    note_down managerd "could not read the time $newest out of a status document"
    return 0
  fi

  now="$(date -u +%s)"
  age=$(( now - seconds ))
  if [[ "$age" -lt "$STALE_AFTER_S" ]]; then
    say "ok: managerd published ${age}s ago ($newest)"
    return 0
  fi

  note_down managerd "no status document written for ${age}s (newest $newest, limit ${STALE_AFTER_S}s)"
}

# --- 3. sessiond ---------------------------------------------------------------

check_sessiond() {
  local code
  if [[ ! -S "$SOCKET" ]]; then
    note_down sessiond "no socket at $SOCKET"
    return 0
  fi

  # 401 is the answer to an unauthenticated list, and an answer is the
  # point: it proves a listener without presenting a token.
  code="$(curl -s -o /dev/null -w '%{http_code}' -m "$CHECK_TIMEOUT_S" \
    --unix-socket "$SOCKET" http://sessiond/v1/sessions 2>/dev/null)"

  if [[ "$code" == "$HTTP_UNAUTHORIZED" || "$code" == "$HTTP_OK" ]]; then
    say "ok: sessiond answers on its socket"
    return 0
  fi

  note_down sessiond "the socket at $SOCKET answered ${code:-nothing}, not $HTTP_UNAUTHORIZED"
}

# --- 4. no rework unit is failed ------------------------------------------------

check_units() {
  local unit failed
  failed=""
  for unit in $UNITS; do
    # `is-failed` exits 0 when the unit IS failed. Under `timeout`, because
    # a wedged user manager must not hold the run.
    if timeout "$CHECK_TIMEOUT_S" systemctl --user is-failed "$unit" >/dev/null 2>&1; then
      failed="$failed $unit"
    fi
  done

  if [[ -z "$failed" ]]; then
    say "ok: no rework user unit is failed"
    return 0
  fi

  note_down units "systemd reports failed:${failed}"
}

# --- 5. the registry still reaches this host ----------------------------------

# WHAT THIS CHECK IS NOT. It does not ask GitHub whether `main` has moved
# past the checkout. That needs a credential and a network call, and this
# watchdog holds neither — dialling GitHub every minute for ever would also
# be its own problem. It asks whether the thing that WOULD have pulled is
# still running: when nothing pulls, the checkout freezes, every service
# stays up, and every family change reaches nothing.
#
# Two readings, both cheap and both local:
#   1. the stamp `bin/rework-registry-sync.sh` writes when a run ENDED WELL
#      is younger than 5 minutes. Read with `find -mmin`, so no clock and no
#      parsing: GNU and BSD `find` both have it, which `stat` cannot say.
#   2. the last run's `Result` is `success`. A run that REFUSED — a diverged
#      checkout, a wrong key — leaves the stamp where the last good run put
#      it, so the age alone would stay quiet for five minutes. `Result`
#      names it in the same minute.
#
# A host whose TIMER IS NOT ENABLED is skipped, never failed. One that never
# took this cutover, and one after a `rollback`, has no sync to be stale,
# and a check that cannot be satisfied is an alarm that cries every minute
# for ever.
check_registry_sync() {
  local stale result
  if ! timeout "$CHECK_TIMEOUT_S" systemctl --user is-enabled "$SYNC_TIMER" >/dev/null 2>&1; then
    say "ok: $SYNC_TIMER is not enabled here, so there is no registry sync to watch"
    return 0
  fi

  if [[ ! -f "$SYNC_STAMP" ]]; then
    note_down registry "no $SYNC_STAMP: $SYNC_SERVICE has never ended well"
    return 0
  fi

  stale="$(timeout "$CHECK_TIMEOUT_S" \
    find "$SYNC_STAMP" -mmin "+$SYNC_STALE_AFTER_MIN" 2>/dev/null)"
  if [[ -n "$stale" ]]; then
    note_down registry \
      "the registry checkout has not been pulled for over ${SYNC_STALE_AFTER_MIN}m ($SYNC_STAMP)"
    return 0
  fi

  result="$(timeout "$CHECK_TIMEOUT_S" \
    systemctl --user show "$SYNC_SERVICE" -p Result --value 2>/dev/null)"
  if [[ "$result" != "$UNIT_RESULT_OK" ]]; then
    note_down registry \
      "the last $SYNC_SERVICE run ended '${result:-nothing}', not $UNIT_RESULT_OK: journalctl --user -u $SYNC_SERVICE"
    return 0
  fi

  say "ok: the registry was pulled inside ${SYNC_STALE_AFTER_MIN}m and the last run succeeded"
}

# --- the verdict, and the one push ------------------------------------------

check_pep
check_managerd
check_sessiond
check_units
check_registry_sync

# RAW_KEYS is every check that failed THIS run: the exit code tracks this
# one, so a lone unconfirmed failure still turns the unit red at once.
RAW_KEYS="${DOWN_KEYS# }"

# VERDICT is CONFIRMED only: a check counts here only on its SECOND
# consecutive failure.
VERDICT="${CONFIRMED_KEYS# }"
[[ -n "$VERDICT" ]] || VERDICT="$ALL_WELL"

# STORED_VERDICT is what was on disk before this run (read above, alongside
# STORED_RAW). WAS is what the last run CONCLUDED and confirmed, and a host
# nobody has looked at yet is assumed well: the alternative pushes a
# "recovered" card the first time the alarm ever runs.
WAS="${STORED_VERDICT:-$ALL_WELL}"

remember() {  # remember <confirmed verdict> <raw keys this run>
  ( umask 077 && mkdir -p "$WATCH_DIR" ) || return 1
  ( umask 077 && printf '%s\n%s\n' "$1" "$2" > "$VERDICT_FILE" )
}

# A quote or a backslash in a path would break the JSON body. Nothing here
# comes from a caller, so this is belt and braces rather than a defence.
plain() {  # plain <text>
  printf '%s' "$1" | tr -d '"\\' | tr '\n' ';' | tr -s ' ;' ' ;'
}

# The phone push. `-H @file`, never the bearer on argv: /proc/<pid>/cmdline
# is world readable for the life of the curl process on a host that sets no
# hidepid (bin/AGENTS.md §Secrets).
send_notice() {  # send_notice <summary>
  local header status
  if [[ -z "${APPROVAL_URL:-}" || -z "${APPROVAL_TOKEN:-}" ]]; then
    say "NOTICE NOT SENT (no APPROVAL_URL/APPROVAL_TOKEN in $HOOKS_ENV): $1"
    return 1
  fi

  header="$(mktemp)"
  ( umask 077 && printf 'Authorization: Bearer %s\n' "$APPROVAL_TOKEN" > "$header" )
  curl -sS -f -o /dev/null -m "$NOTICE_TIMEOUT_S" -X POST "$APPROVAL_URL" \
    -H @"$header" -H 'Content-Type: application/json' \
    -d "{\"kind\":\"notice\",\"job_id\":\"$NOTICE_JOB_ID\",\"gate_id\":\"$NOTICE_GATE_ID\",\
\"agent\":\"$NOTICE_AGENT\",\"summary\":\"$(plain "$1")\"}"
  status=$?
  rm -f "$header"

  if [[ "$status" == 0 ]]; then
    say "phone alert sent"
    return 0
  fi

  say "NOTICE NOT SENT (curl exit $status), retrying next run: $1"
  return 1
}

# Never `set -a` either file: both hold other daemon secrets, and sourcing
# one exports every last of them into this script and into curl. Two values
# are wanted and two values are read.
APPROVAL_URL="$(envfile_hook_value "$HOOKS_ENV" "$DEPRECATED_ENV" APPROVAL_URL)"
APPROVAL_TOKEN="$(envfile_hook_value "$HOOKS_ENV" "$DEPRECATED_ENV" APPROVAL_TOKEN)"

if [[ "$VERDICT" == "$WAS" ]]; then
  say "verdict unchanged: $VERDICT (no push)"
  # Written every run, not only the first: RAW_KEYS moves even when VERDICT
  # does not (a check at "1 of 2", or one that just cleared its streak), and
  # the next run's was_raw_down needs THIS run's raw set to tell "1 of 2"
  # from "2 of 2". `bin/rework-cutover.sh status` asks for line 1 alone.
  remember "$WAS" "$RAW_KEYS" || say "WARNING: could not write $VERDICT_FILE"

  [[ -z "$RAW_KEYS" ]] && exit 0

  exit 1
fi

if [[ "$VERDICT" == "$ALL_WELL" ]]; then
  SUMMARY="rework recovered: $WAS answers again, all five checks pass"
else
  SUMMARY="rework DOWN: $(printf '%s' "$CONFIRMED_LINES") see journalctl --user -u agent-rework-watchdog"
fi

say "verdict moved: $WAS -> $VERDICT"
if send_notice "$SUMMARY"; then
  remember "$VERDICT" "$RAW_KEYS" || say "WARNING: could not write $VERDICT_FILE; the next run pushes again"
else
  # The push failed: keep the OLD confirmed verdict so the next run sees
  # "changed" again and retries the push. RAW_KEYS still gets written, or
  # a retried confirmation could never tell itself apart from a fresh "1 of
  # 2" and would silently wait a second extra minute.
  remember "$WAS" "$RAW_KEYS" || say "WARNING: could not write $VERDICT_FILE; the next run pushes again"
fi

[[ -z "$RAW_KEYS" ]] && exit 0
exit 1
