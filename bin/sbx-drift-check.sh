#!/usr/bin/env bash
# OPERATOR drift alarm, run from sbx-drift.timer on the host.
#
# Two checks, both read-only (no `sbx policy rm`, no mutation):
#   1. Global local-policy must hold ZERO network allows — every egress
#      grant is per-sandbox.
#   2. Per-sandbox + kit rules (`sbx policy ls --wide`) must not allow a
#      host other than the host's LAN address (LiteLLM/PEP/TEI) or the seeded allowlist:
#      every sandbox is created from sbx's built-in `shell` kit, which
#      attaches its own `kit:<name>` network rule set —
#      non-editable, recreated on every `sbx create`, and invisible to
#      check 1 and to the manager's own egress probes
#      (caregiver/src/caregiver/egress.py, and the driver that applies
#      it: both only probe the granted host and a fixed denied list). Today
#      that kit rule includes an unconditional `allow openrouter.ai` on
#      every sandbox — a bypass of LiteLLM (no budget, no model allow-list,
#      no spend log) for anything that can smuggle an OpenRouter key into a
#      sandbox — so it gets an explicit, always-on check rather than
#      waiting for the allowlist file below.
#
# /srv/agents/state/egress-allowlist.txt (one host per line, `#` comments,
# blank lines ignored) is the exception list for check 2. Nothing writes it:
# an operator seeds and edits it by hand, and this script only reads it.
#
# On drift, POSTs a `kind: notice` to the approval hook (docs/node-red-mcp.md
# §1.3) so the alert lands on the phone without Approve/Deny buttons — this
# is an alarm, not a gate. Exit 1 on any drift so the unit shows failed in
# systemctl too.
#
# The two bearers come from /srv/agents/state/rework/hooks.env, which
# `bin/rework-cutover.sh up` writes. The old system's env file is a
# FALLBACK, and every read of it logs one DEPRECATED line
# (`bin/lib/envfile.sh`, `bin/rework-watchdog.sh`).
#
# The host's LAN address is AGENT_LAN_ADDRESS in /etc/agent-control/site.env
# (release/src/agent_release/site.py). Without it the script stops before it
# asks sbx anything: no default, because a default would be somebody's host.
set -uo pipefail
# sbx phones home on every invocation and that, not the work asked of it, is
# most of what each call costs (SBX_NO_TELEMETRY, measured on this host).
# Set here because this script runs from a timer, with no parent to set it.
export SBX_NO_TELEMETRY="${SBX_NO_TELEMETRY:-1}"
export PATH="$HOME/.local/bin:$PATH"

# A TEST SEAM, not a permission. Empty everywhere else, including on
# the host. It moves only the two env files, which is all a test of this
# script's credential source needs — `sbx` itself is a binstub.
DRIFT_TEST_PREFIX="${SBX_DRIFT_TEST_PREFIX:-}"
HOOKS_ENV="$DRIFT_TEST_PREFIX/srv/agents/state/rework/hooks.env"
DEPRECATED_ENV="$DRIFT_TEST_PREFIX/srv/agents/state/materializer/env"
. "$(dirname -- "${BASH_SOURCE[0]}")/lib/envfile.sh"
# Two values, never `set -a` and a `source`: that file holds every other
# daemon secret and sourcing it exports the lot into curl's environment.
APPROVAL_URL="$(envfile_hook_value "$HOOKS_ENV" "$DEPRECATED_ENV" APPROVAL_URL)"
APPROVAL_TOKEN="$(envfile_hook_value "$HOOKS_ENV" "$DEPRECATED_ENV" APPROVAL_TOKEN)"

# `AGENT_SITE_FILE` names another site file, which is how a test points at a
# fixture. LiteLLM :4000, PEP :8300, TEI :8085 on this address: any port.
SITE_FILE="${AGENT_SITE_FILE:-/etc/agent-control/site.env}"
ALLOWED_HOST="$(envfile_value "$SITE_FILE" AGENT_LAN_ADDRESS)"
if [[ -z "$ALLOWED_HOST" ]]; then
  printf 'sbx-drift-check: no AGENT_LAN_ADDRESS in %s\n' "$SITE_FILE" >&2
  exit 1
fi

ALLOWLIST_FILE=/srv/agents/state/egress-allowlist.txt
FAILS=0
DRIFT_LINES=()

say() { echo "$*"; }

# --- Check 1: global local-policy must be empty -----------------------
# Same extraction the bootstrap reset uses: one removable rule per line.
mapfile -t RULES < <(sbx policy inspect local-policy 2>/dev/null \
  | grep -oE 'sbx policy rm network --id [0-9a-f-]+' || true)
COUNT="${#RULES[@]}"

if [[ "$COUNT" -eq 0 ]]; then
  say "sbx global policy clean: 0 network allows"
else
  say "DRIFT: $COUNT global network allow rule(s) in local-policy:"
  sbx policy inspect local-policy 2>/dev/null || true
  DRIFT_LINES+=("GLOBAL: $COUNT network allow rule(s) in local-policy")
  FAILS=1
fi

# --- Check 2: per-sandbox + kit rules ----------------------------------
EXTRA_ALLOW=()
if [[ -r "$ALLOWLIST_FILE" ]]; then
  while IFS= read -r line; do
    line="${line%%#*}"
    line="$(echo "$line" | tr -d '[:space:]')"
    [[ -n "$line" ]] && EXTRA_ALLOW+=("$line")
  done < "$ALLOWLIST_FILE"
else
  say "NOTE: $ALLOWLIST_FILE not found; no exceptions beyond $ALLOWED_HOST are honoured"
fi

# `sbx policy ls --wide` is the one view that lists per-sandbox AND
# kit-sourced rules together; local-policy (check 1) does not show either.
# The exact column layout is not documented anywhere in this repo, so the
# parse below only looks for an "allow <host>[:<port>]" token on each line
# and is tolerant of whatever precedes it (rule id, sandbox name, "kit:foo",
# "local", ...). Re-check this regex against a live `sbx policy ls --wide`
# if sbx's output format ever changes.
WIDE="$(sbx policy ls --wide 2>/dev/null || true)"
if [[ -z "$WIDE" ]]; then
  say "WARNING: 'sbx policy ls --wide' returned nothing; per-sandbox/kit rules were not checked"
else
  OPENROUTER_HOLES=()
  BAD_ALLOWS=()
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    host="$(echo "$line" | grep -oiE 'allow[[:space:]]+[A-Za-z0-9._-]+(:[0-9]+)?' | awk '{print $2}')"
    [[ -z "$host" ]] && continue
    hostonly="${host%%:*}"

    if [[ "$hostonly" == "openrouter.ai" ]]; then
      # This line itself is the "allow" (that's how we found it); the "deny
      # beats allow" escape hatch is a SEPARATE line for the same sandbox,
      # so look for one instead of grepping this line for "deny" (which,
      # having already matched "allow ...", can never be true).
      sbx_id="$(echo "$line" | grep -oE 'sandbox:[A-Za-z0-9_.-]+' || true)"
      if [[ -n "$sbx_id" ]] \
         && echo "$WIDE" | grep -F "$sbx_id" | grep -i 'deny' | grep -qi 'openrouter'; then
        continue # a per-sandbox deny for openrouter.ai is present — fine
      fi
      OPENROUTER_HOLES+=("$line")
      continue
    fi

    [[ "$hostonly" == "$ALLOWED_HOST" ]] && continue

    allowed=0
    for a in "${EXTRA_ALLOW[@]:-}"; do
      [[ -n "$a" && "$hostonly" == "$a" ]] && { allowed=1; break; }
    done
    [[ "$allowed" -eq 1 ]] && continue

    BAD_ALLOWS+=("$line")
  done <<< "$WIDE"

  if [[ "${#OPENROUTER_HOLES[@]}" -gt 0 ]]; then
    say "DRIFT: openrouter.ai egress present without a per-sandbox deny:"
    printf '  %s\n' "${OPENROUTER_HOLES[@]}"
    DRIFT_LINES+=("OPENROUTER: ${#OPENROUTER_HOLES[@]} sandbox rule(s) allow openrouter.ai with no deny")
    FAILS=1
  fi
  if [[ "${#BAD_ALLOWS[@]}" -gt 0 ]]; then
    say "DRIFT: network allow(s) to a host that is neither $ALLOWED_HOST nor in $ALLOWLIST_FILE:"
    printf '  %s\n' "${BAD_ALLOWS[@]}"
    DRIFT_LINES+=("EGRESS: ${#BAD_ALLOWS[@]} allow rule(s) outside $ALLOWED_HOST and the allowlist")
    FAILS=1
  fi
  if [[ "$FAILS" -eq 0 ]]; then
    say "sbx per-sandbox/kit policy clean: no allow outside $ALLOWED_HOST or $ALLOWLIST_FILE, no bare openrouter.ai"
  fi
fi

if [[ "$FAILS" -eq 0 ]]; then
  exit 0
fi

if [[ -n "${APPROVAL_URL:-}" && -n "${APPROVAL_TOKEN:-}" ]]; then
  SUMMARY="sbx policy drift: $(IFS='; '; echo "${DRIFT_LINES[*]}") — see journalctl --user -u sbx-drift"
  # `-H @file`, never the bearer on argv (bin/AGENTS.md, Secrets): argv is
  # world-readable in /proc for the life of the curl process. The fixed
  # job/gate ids only give the card a stable tag, so a repeat replaces it.
  HDR_FILE="$(mktemp)"
  ( umask 077 && printf 'Authorization: Bearer %s\n' "$APPROVAL_TOKEN" > "$HDR_FILE" )
  curl -s -o /dev/null -m 10 -X POST "$APPROVAL_URL" \
    -H @"$HDR_FILE" -H 'Content-Type: application/json' \
    -d "{\"kind\":\"notice\",\"job_id\":\"zzzzzzzzzzzzzzzzzzzzzzzzzz\",\"gate_id\":\"0000000000000000\",\"agent\":\"sbx-drift\",\"summary\":\"$SUMMARY\"}" \
    && say "phone alert sent"
  rm -f "$HDR_FILE"
fi
exit 1
