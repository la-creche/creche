#!/usr/bin/env bash
# OPERATOR drift alarm, run from sbx-drift.timer on the host.
#
# Two checks, both read-only (no `sbx policy rm`, no mutation):
#   1. Every network allow must name one sandbox. A rule that names no
#      sandbox applies to all of them, and every egress grant is per-sandbox.
#   Both checks read `sbx policy ls --json`. A listing that does not read is
#   drift: a check that cannot see must not pass.
#   2. Per-sandbox + kit rules (`sbx policy ls --json`) must not allow a
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
# The two bearers come from /srv/agents/state/creche/hooks.env, which
# `bin/rework-cutover.sh up` writes. The old system's env file is a
# FALLBACK, and every read of it logs one DEPRECATED line
# (`bin/lib/envfile.sh`, `bin/creche-watchdog.sh`).
#
# The host's LAN address is AGENT_LAN_ADDRESS in /etc/creche/site.env
# (handover/src/handover/site.py). Without it the script stops before it
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
HOOKS_ENV="$DRIFT_TEST_PREFIX/srv/agents/state/creche/hooks.env"
DEPRECATED_ENV="$DRIFT_TEST_PREFIX/srv/agents/state/materializer/env"
. "$(dirname -- "${BASH_SOURCE[0]}")/lib/envfile.sh"
# Two values, never `set -a` and a `source`: that file holds every other
# daemon secret and sourcing it exports the lot into curl's environment.
APPROVAL_URL="$(envfile_hook_value "$HOOKS_ENV" "$DEPRECATED_ENV" APPROVAL_URL)"
APPROVAL_TOKEN="$(envfile_hook_value "$HOOKS_ENV" "$DEPRECATED_ENV" APPROVAL_TOKEN)"

# `AGENT_SITE_FILE` names another site file, which is how a test points at a
# fixture. LiteLLM :4000, PEP :8300, TEI :8085 on this address: any port.
SITE_FILE="${AGENT_SITE_FILE:-/etc/creche/site.env}"
ALLOWED_HOST="$(envfile_value "$SITE_FILE" AGENT_LAN_ADDRESS)"
if [[ -z "$ALLOWED_HOST" ]]; then
  printf 'sbx-drift-check: no AGENT_LAN_ADDRESS in %s\n' "$SITE_FILE" >&2
  exit 1
fi

ALLOWLIST_FILE=/srv/agents/state/egress-allowlist.txt
FAILS=0
DRIFT_LINES=()

say() { echo "$*"; }

# --- Read every rule once ------------------------------------------------
# `sbx policy ls --json` lists the global and the per-sandbox rules. sbx
# v0.43 and v0.48 give it the same fields: {"rules": [{"applies_to",
# "resource_type", "decision", "resources": [...]}, ...]}. The text tables
# changed their columns in v0.48, so this script reads no table.
#
# The filter prints one line for each network resource:
#   <applies_to> TAB <decision> TAB <resource>
# A text of another shape stops it with status 1, and both checks fail.
# Single quotes: the program holds no quote of that kind.
ROWS_PROGRAM='
import json, sys
try:
    doc = json.load(sys.stdin)
except ValueError as exc:
    sys.exit(f"not JSON: {exc}")
rules = doc.get("rules") if isinstance(doc, dict) else None
if not isinstance(rules, list):
    sys.exit("no list named rules")
def word(value):
    return isinstance(value, str) and value != "" and not any(c in value for c in "\t\n\r")
for rule in rules:
    if not isinstance(rule, dict):
        sys.exit("a rule is not an object")
    if rule.get("resource_type") != "network":
        continue
    scope, decision, resources = rule.get("applies_to"), rule.get("decision"), rule.get("resources")
    if not word(scope) or decision not in ("allow", "deny") or not isinstance(resources, list):
        sys.exit("a network rule of another shape")
    for resource in resources:
        if not word(resource):
            sys.exit("a resource that is not one word")
        print(f"{scope}\t{decision}\t{resource}")
'
RULES_JSON="$(sbx policy ls --json 2>/dev/null)"
LS_STATUS=$?
ROWS_TEXT="$(printf '%s' "$RULES_JSON" | python3 -I -c "$ROWS_PROGRAM")"
ROWS_STATUS=$?

if [[ "$LS_STATUS" -ne 0 || "$ROWS_STATUS" -ne 0 ]]; then
  say "DRIFT: cannot read 'sbx policy ls --json' (sbx status $LS_STATUS, filter status $ROWS_STATUS); no rule was checked"
  DRIFT_LINES+=("UNREADABLE: sbx policy ls --json")
  FAILS=1
  ROWS_TEXT=""
fi
mapfile -t ROWS <<< "$ROWS_TEXT"

# --- Check 1: no global network allow ------------------------------------
# A rule that names no sandbox applies to every sandbox.
GLOBAL_ALLOWS=()
for row in "${ROWS[@]}"; do
  IFS=$'\t' read -r scope decision resource <<< "$row"
  [[ -z "$row" || "$scope" == sandbox:* || "$decision" != allow ]] && continue
  GLOBAL_ALLOWS+=("$scope allow $resource")
done

if [[ "${#GLOBAL_ALLOWS[@]}" -eq 0 ]]; then
  [[ "$FAILS" -eq 0 ]] && say "sbx global policy clean: 0 network allows"
else
  say "DRIFT: ${#GLOBAL_ALLOWS[@]} global network allow(s):"
  printf '  %s\n' "${GLOBAL_ALLOWS[@]}"
  DRIFT_LINES+=("GLOBAL: ${#GLOBAL_ALLOWS[@]} network allow(s) that name no sandbox")
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

# Each allow row that names a sandbox. A deny row needs no check.
if [[ -n "$ROWS_TEXT" ]]; then
  OPENROUTER_HOLES=()
  BAD_ALLOWS=()
  for row in "${ROWS[@]}"; do
    IFS=$'\t' read -r scope decision resource <<< "$row"
    [[ -z "$row" || "$scope" != sandbox:* || "$decision" != allow ]] && continue
    line="$scope allow $resource"
    hostonly="${resource%%:*}"

    if [[ "$hostonly" == "openrouter.ai" ]]; then
      # Deny beats allow: a deny row for the same sandbox closes this one.
      if grep -qxF "$scope"$'\t'"deny"$'\t'"openrouter.ai" <<< "$ROWS_TEXT"; then
        continue
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
  done

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
