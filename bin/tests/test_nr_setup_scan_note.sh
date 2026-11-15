#!/usr/bin/env bash
# Regression test for bin/nr-setup.sh step 7's scan note. Only the sync's
# exit 77 (noderedsync.Exit.LATCHED) means a latch was written. Any other
# failure, such as an HTTP 400 from a port that speaks HTTPS, latches
# nothing, and a note that says otherwise sends the operator to clear a latch
# that does not exist.
#
# Run: bash bin/tests/test_nr_setup_scan_note.sh
set -uo pipefail
HERE="$(cd "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/../nr-setup.sh"

FAILS=0
pass() { printf 'PASS: %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*"; FAILS=$((FAILS + 1)); }

# Source only what is under test; the script itself needs Hyperion.
for pattern in '/^SYNC_EXIT_LATCHED=/p' '/^scan_failed_note()/,/^}/p'; do
  PART="$(sed -n "$pattern" "$SCRIPT")"
  [[ -n "$PART" ]] || { fail "$pattern not found in $SCRIPT"; echo "GATE: FAIL ($FAILS)"; exit 1; }
  eval "$PART"
done

GOT="$(scan_failed_note 77)"
[[ "$GOT" == *"latched"* && "$GOT" == *"nothing enabled"* ]] \
  && pass "exit 77 says the password was refused and latched" \
  || fail "exit 77 gave: $GOT"

for rc in 75 78 1; do
  GOT="$(scan_failed_note "$rc")"
  [[ "$GOT" == *"no latch"* && "$GOT" == *"exit $rc"* && "$GOT" == *"nothing enabled"* && "$GOT" != *"now latched"* ]] \
    && pass "exit $rc says no latch was written" \
    || fail "exit $rc gave: $GOT"
done

[[ "$FAILS" -eq 0 ]] && { echo "GATE: PASS"; exit 0; } || { echo "GATE: FAIL ($FAILS)"; exit 1; }
