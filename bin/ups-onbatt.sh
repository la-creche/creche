#!/usr/bin/env bash
# ROOT — run by upssched (nut-monitor, RUN_AS_USER root), never by hand:
#   /opt/creche/bin/ups-onbatt.sh <event>
# Installed as CMDSCRIPT by bin/setup-ups.sh. Operations family
# (bin/AGENTS.md): fails loudly into the journal.
#
# One event per call, the timer or alarm name from upssched.conf:
#   onbatt   5 minutes on battery ──> forced shutdown (`upsmon -c fsd`)
#   online   mains came back inside the window ──> nothing, say so
#   lowbatt  the UPS flags low battery ──> nothing, upsmon shuts down itself
set -euo pipefail

TAG=ups-onbatt

case "${1:-}" in
  onbatt)
    logger -t "$TAG" "ups1 on battery for the whole window: forced shutdown"
    exec upsmon -c fsd
    ;;
  online)
    logger -t "$TAG" "mains back inside the window: timer cancelled"
    ;;
  lowbatt)
    logger -t "$TAG" "low battery: upsmon shuts down on its own"
    ;;
  *)
    logger -t "$TAG" -p user.err "unknown event: ${1:-<none>}"
    exit 1
    ;;
esac
