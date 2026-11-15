#!/usr/bin/env bash
# OPERATOR — on the host, as the operator, never as root: /opt/creche/bin/nr-setup.sh
# Setup for the creche side of the protected approval instance
# (see the operator's private notes). Mints APPROVAL_TOKEN append-once, points
# APPROVAL_URL at the loopback Node-RED hook, mints the stack's
# CREDENTIAL_SECRET once, probes 1881, and prints the operator steps it must
# not do itself — the HA user, the Arcane stack, the flow import (no
# script in this repo may touch the approval instance's files). Idempotent.
#
# Step 9: the reviewer's LiteLLM virtual key, minted once and PRINTED
# once, because it has to reach /etc/chaperone/secrets/reviewer_key — a file
# only the chaperone user writes, and this script runs as the operator.
#
# Steps 4-8: the state dirs, the clone of the private
# nodered-flows repo, the sync's env file (the password is typed at a
# prompt, never argv, never echoed), then `nodered-flows-sync --scan-only`
# and a question — the first push is irreversible, so nothing is installed
# or enabled until the operator has read what it would send and answered y. With no
# terminal it never asks and never enables. Every step skips what a missing
# operator step blocks and says so; re-run it after each one.
#
# BEFORE running: deploy this tree with `sudo creche-deploy`.
#
# Environment, no default for any of them (a LAN address, a repository owner
# and a host name are the operator's, not this tree's):
#   HYPERION_IP           the LAN address of this host
#   NODERED_URL           the base URL of the Node-RED admin API
#   APPROVAL_FLOWS_REPO   git URL of the private repo of the approval flows
#   NODERED_FLOWS_REPO    git URL of the private repo the sync pushes to
#   HA_URL                the base URL of Home Assistant (printed steps only)
#   LITELLM_URL           the base URL of LiteLLM (step 9 only)
set -euo pipefail
[[ "$(id -u)" != "0" ]] || { echo "run as the operator, not root" >&2; exit 1; }
say() { printf '\n[OPERATOR] %s\n' "$*"; }
# One top-level field out of a JSON body on stdin. Never prints the body: on a
# successful mint the body IS the key.
py_field() { python3 -c "import json,sys
try:
    print(json.load(sys.stdin).get('$1', ''))
except ValueError:
    print('')"; }
# Does /key/list name this alias? Measured 2026-09-18 against a live
# LiteLLM: /key/info?key_alias=<name> answers 404 for a key that is absent,
# and its shape for a key that is PRESENT is unverified — so the existence
# check reads the list, whose shape is verified in both directions. Exit 0
# means the alias is already there.
py_alias() { python3 -c "import json,sys
try:
    body = json.load(sys.stdin)
except ValueError:
    sys.exit(1)
rows = body.get('keys') or body.get('data') or []
names = [r.get('key_alias') if isinstance(r, dict) else r for r in rows]
sys.exit(0 if sys.argv[1] in names else 1)" "$1"; }
# curl against LiteLLM as its admin. The master key reaches curl in a 0600
# header file, never on argv: /proc/<pid>/cmdline is world-readable for the
# life of the process and this host sets no hidepid (bin/AGENTS.md §Secrets).
# `|| status=$?`, not a bare `status=$?`: under set -e a failed call would
# otherwise skip the rm.
litellm_curl() {  # litellm_curl <url> [curl args...]
  local header status=0
  header="$(mktemp)"
  ( umask 077 && printf 'Authorization: Bearer %s\n' "$LITELLM_MASTER_KEY" > "$header" )

  curl -s -m 10 -H @"$header" "$@" || status=$?
  rm -f "$header"
  return "$status"
}
REPO="$(cd "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
. "$REPO/bin/lib/envfile.sh"
# The home of the approval hook's URL and bearer, read by the watchdog of
# creche-release. Step 1 below writes the pair HERE.
HOOKS_ENV=/srv/agents/state/creche/hooks.env
# Step 9 mints against LiteLLM, and the master key lives in the hook env file
# rather than in this shell. The shell's own value wins. The file is parsed
# by `envfile_value`, never sourced, so no other key of it is exported. A run
# with no key still reaches the skip branch of step 9.
LITELLM_MASTER_KEY="${LITELLM_MASTER_KEY:-$(envfile_value "$HOOKS_ENV" LITELLM_MASTER_KEY)}"
export LITELLM_MASTER_KEY
# The old system's env file. Nothing maintains it any more. It still carries
# RESUME_TOKEN and HA_WEBHOOK_URL, which belong to the Node-RED flow rather
# than to this repository, so this script still reads it — and writes nothing
# to it.
ENV_FILE=/srv/agents/state/materializer/env
# The stack's own .env lines live here between runs so a re-run never
# re-mints them (setup-vault-sync.sh keeps its member password the same
# way). Under /srv/agents/state (operator-owned, not group-writable), NOT under
# materializer/ (setgid `agents`: chaperone could rename a file there).
STACK_ENV_COPY=/srv/agents/state/nodered-approvals.env
STACK_DIR=/srv/arcane/nodered-approvals
HOOK_BASE="http://127.0.0.1:1881"
APPROVAL_URL_VALUE="$HOOK_BASE/hook/approval"
HYPERION_IP="${HYPERION_IP:?set HYPERION_IP to the LAN address of this host}"
FLOWS_REPO="${APPROVAL_FLOWS_REPO:?set APPROVAL_FLOWS_REPO to the git URL of the approval flows repo}"
HA_URL="${HA_URL:?set HA_URL to the base URL of Home Assistant}"
# The flows sync. Its env file sits beside the other operator-only secrets,
# not under nodered/ (group agents can traverse that one).
DEPLOYED=/opt/creche
UNIT_DIR="$HOME/.config/systemd/user"
# The reviewer key, on the strongest alias.
# LiteLLM's address: LITELLM_URL when it is set, else the host's own LAN IP.
# NOT loopback — `creche-litellm` publishes to the LAN address and nothing
# answers on 127.0.0.1:4000, so a loopback URL makes step 9 skip silently.
LITELLM="${LITELLM_URL:-http://$HYPERION_IP:4000}"
REVIEWER_ALIAS=reviewer
REVIEWER_MODEL=agent_max
REVIEWER_BUDGET=2
REVIEWER_WINDOW=1d
#: One page holds every key this LiteLLM has (11 on 2026-09-18) with room to
#: grow; the scan does not paginate, so a fleet past this would need it to.
REVIEWER_LIST_SIZE=100
NR_STATE=/srv/agents/state/nodered
NR_CLONE="$NR_STATE/flows-repo"
NR_LATCH="$NR_STATE/sync.latch"
SYNC_ENV=/srv/agents/state/nodered-sync.env
SYNC_USER=nodered-sync
SYNC_UNIT=nodered-flows-sync
SYNC_BIN="$DEPLOYED/.venv/bin/$SYNC_UNIT"
SYNC_REPO="${NODERED_FLOWS_REPO:?set NODERED_FLOWS_REPO to the git URL of the flows repo}"
NODERED_URL_VALUE="${NODERED_URL:?set NODERED_URL to the base URL of the Node-RED admin API}"

# The scan runs with the env file's keys, exported in a subshell: the
# password never enters this script's own environment or anyone's argv. The
# file is parsed, not sourced — a value is data, never a command.
run_scan() (
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || continue
    export "${BASH_REMATCH[1]}=${BASH_REMATCH[2]}"
  done < "$SYNC_ENV"
  exec "$SYNC_BIN" --scan-only
)

# Only this exit means a latch was written (noderedsync.Exit.LATCHED). Any
# other failure — the network, a 5xx, the add-on's port speaking HTTPS —
# latches nothing, and saying otherwise sends the operator after a file that is
# not there (bin/tests/test_nr_setup_scan_note.sh).
SYNC_EXIT_LATCHED=77
scan_failed_note() { # scan_failed_note <exit code of the scan>
  if [[ "$1" == "$SYNC_EXIT_LATCHED" ]]; then
    echo "scan failed — nothing enabled. The password was refused and is now latched; the line above says how to clear it"
    return
  fi
  echo "scan failed (exit $1) — nothing enabled, and no latch was written. The line above says why; fix it and re-run"
}

say "0. preflight"
grep -q '^RESUME_TOKEN=' "$ENV_FILE" 2>/dev/null \
  || { echo "RESUME_TOKEN missing from $ENV_FILE — it is the flow's own callback bearer; the printed steps below say where it goes" >&2; exit 1; }

say "1. hooks env: APPROVAL_TOKEN (minted once) + APPROVAL_URL (values not printed)"
# append_once for the token — minted once, never silently replaced; upsert
# for the URL — it has a current answer that may move (bin/lib/envfile.sh).
envfile_append_once "$HOOKS_ENV" APPROVAL_TOKEN "$(openssl rand -hex 24)"
envfile_upsert "$HOOKS_ENV" APPROVAL_URL "$APPROVAL_URL_VALUE"
echo "keys now present in $HOOKS_ENV: $(cut -d= -f1 "$HOOKS_ENV" | tr '\n' ' ')"
if grep -q '^HA_WEBHOOK_URL=' "$ENV_FILE" 2>/dev/null; then
  echo "HA_WEBHOOK_URL is still in $ENV_FILE — left alone on purpose; it is the flow's, not this repository's"
fi

say "2. the stack's .env lines (CREDENTIAL_SECRET minted once; kept at $STACK_ENV_COPY, 0600)"
envfile_append_once "$STACK_ENV_COPY" CREDENTIAL_SECRET "$(openssl rand -hex 32)"
echo "keys: $(cut -d= -f1 "$STACK_ENV_COPY" | tr '\n' ' ')"

say "3. probe 1881 (informational until the stack exists)"
LOCAL=$(curl -s -o /dev/null -w '%{http_code}' -m 3 "$HOOK_BASE/" || true)
LAN=$(curl -s -o /dev/null -w '%{http_code}' -m 3 "http://$HYPERION_IP:1881/" || true)
echo "127.0.0.1:1881 -> $LOCAL (want != 000 once the stack is up)"
echo "$HYPERION_IP:1881 -> $LAN (must stay 000: the publish is loopback-only)"

say "4. the state dirs: the sync's own; no MCP server reaches them"
install -d -o "$(id -un)" -g agents -m 2750 "$NR_STATE"
install -d -o "$(id -un)" -g agents -m 2750 "$NR_CLONE"
echo "$NR_STATE {flows-repo 2750}"

say "5. the clone of $SYNC_REPO"
CLONED=0
if [[ -d "$NR_CLONE/.git" ]]; then
  CLONED=1
  echo "already cloned"
elif ( umask 027 && GIT_TERMINAL_PROMPT=0 git clone -q "$SYNC_REPO" "$NR_CLONE" ); then
  CLONED=1
  echo "cloned"
else
  echo "clone FAILED — create the PRIVATE repo first (N2 below), then re-run"
fi
if [[ "$CLONED" == "1" ]]; then
  # An empty repo clones with an unborn HEAD: pin the branch the sync
  # pushes. It makes the first commit itself; this script commits nothing.
  git -C "$NR_CLONE" rev-parse -q --verify HEAD >/dev/null \
    || git -C "$NR_CLONE" symbolic-ref HEAD refs/heads/main
  # Group READ on new objects, as the clone's 2750 allows — never `group`,
  # which writes.
  git -C "$NR_CLONE" config core.sharedRepository 0640
fi

say "6. $SYNC_ENV (0600): URL + user, and the password typed once (never echoed)"
envfile_upsert "$SYNC_ENV" NODERED_URL "$NODERED_URL_VALUE"
envfile_upsert "$SYNC_ENV" NODERED_SYNC_USER "$SYNC_USER"
if grep -q '^NODERED_SYNC_PASSWORD=' "$SYNC_ENV"; then
  echo "password present (append-once; to replace it: rm $SYNC_ENV and re-run)"
elif [[ -t 0 ]]; then
  read -rs -p "HA password of '$SYNC_USER' (empty = not yet): " SYNC_PASSWORD || true
  echo
  case "$SYNC_PASSWORD" in
    "") echo "skipped — create the HA user first (N1 below), then re-run" ;;
    # systemd's EnvironmentFile= re-parses these; the unit would send another password.
    *[[:space:]\"\'\\]*) echo "REFUSED: no spaces, quotes or backslashes — change it in HA, re-run" ;;
    *) envfile_append_once "$SYNC_ENV" NODERED_SYNC_PASSWORD "$SYNC_PASSWORD" ;;
  esac
  unset SYNC_PASSWORD
else
  echo "no terminal: password step skipped"
fi
echo "keys now present: $(cut -d= -f1 "$SYNC_ENV" | tr '\n' ' ')"

say "7. what the first push would send (--scan-only: one GET; nothing written, committed or pushed)"
ENABLE=0
if [[ "$CLONED" != "1" ]] || ! grep -q '^NODERED_SYNC_PASSWORD=' "$SYNC_ENV"; then
  echo "skipped: needs the clone (5) and the password (6)"
elif [[ ! -x "$SYNC_BIN" ]]; then
  echo "skipped: $SYNC_BIN missing — sudo creche-deploy first"
elif systemctl --user is-enabled --quiet "$SYNC_UNIT.timer" 2>/dev/null; then
  ENABLE=1
  echo "timer already enabled: consent was given on an earlier run"
elif [[ ! -t 0 ]]; then
  echo "no terminal: nobody to read the scan, so no request is sent and nothing is enabled"
elif ! { run_scan; SCAN_RC=$?; [[ "$SCAN_RC" == "0" ]]; }; then
  scan_failed_note "$SCAN_RC"
else
  echo "Secret-shaped strings above would land in the remote repo as they are, for good."
  echo "To fix first: move them into node credentials or tab env, Deploy, re-run."
  read -r -p "Enable the 5-minute sync and push these flows to the remote repo? [y/N] " ANSWER || true
  if [[ "${ANSWER:-}" == [yY] ]]; then
    ENABLE=1
  fi
fi

say "8. units: $SYNC_UNIT.{service,timer}"
if [[ "$ENABLE" == "1" ]]; then
  mkdir -p "$UNIT_DIR"
  install -m 0644 "$DEPLOYED/systemd/$SYNC_UNIT.service" "$DEPLOYED/systemd/$SYNC_UNIT.timer" "$UNIT_DIR/"
  systemctl --user daemon-reload
  systemctl --user enable --now "$SYNC_UNIT.timer"
  echo "enabled; first run within 5 min, or now: systemctl --user start $SYNC_UNIT.service"
else
  echo "nothing installed or enabled. Re-run bin/nr-setup.sh when ready; it asks again."
fi
if [[ -e "$NR_LATCH" ]]; then
  echo "LATCH present ($NR_LATCH): the password was refused once and nothing retries."
  echo "  rm $SYNC_ENV && ./bin/nr-setup.sh    # type the right one"
  echo "  rm $NR_LATCH"
fi

say "9. the reviewer's LiteLLM virtual key ($REVIEWER_ALIAS)"
# Minted here rather than by `caregiver` because it
# belongs to no family, and the registry has nothing to declare it in. This
# is its only minter.
if [[ -z "${LITELLM_MASTER_KEY:-}" ]]; then
  echo "LITELLM_MASTER_KEY is in neither this shell nor $HOOKS_ENV — skipped."
  echo "  export it and re-run."
elif litellm_curl "$LITELLM/key/list?page=1&size=$REVIEWER_LIST_SIZE&return_full_object=true" \
       | py_alias "$REVIEWER_ALIAS"; then
  echo "$REVIEWER_ALIAS already exists at $LITELLM — left alone."
  echo "  LiteLLM shows a key's cleartext exactly once, so a re-mint is a NEW key:"
  echo "  if /etc/chaperone/secrets/reviewer_key does not hold it, delete this one"
  echo "  in LiteLLM and re-run."
else
  # The response body IS the key on success, so it is never echoed whole and
  # never logged: only the one line below prints it, for step N5.
  REVIEWER_MINT=$(litellm_curl "$LITELLM/key/generate" \
    -H 'Content-Type: application/json' \
    -d "{\"key_alias\":\"$REVIEWER_ALIAS\",\"models\":[\"$REVIEWER_MODEL\"],\"max_budget\":$REVIEWER_BUDGET,\"budget_duration\":\"$REVIEWER_WINDOW\"}" \
    | py_field key)
  if [[ "$REVIEWER_MINT" == sk-* ]]; then
    echo "minted $REVIEWER_ALIAS: $REVIEWER_MODEL, \$$REVIEWER_BUDGET per $REVIEWER_WINDOW"
    echo
    echo "  COPY THIS ONCE — LiteLLM will not show it again:"
    echo "    reviewer_key: $REVIEWER_MINT"
    echo
    echo "  Write it into /etc/chaperone/secrets/reviewer_key with step N5 below."
    echo "  chaperone refuses to start while install.toml binds a file it cannot read."
  else
    echo "mint FAILED (no key in the answer). Is $LITELLM up, and the master key right?"
  fi
fi

say "DONE. Operator steps, in order — each is yours, none is this script's:"
cat <<EOF
 Git-backed flows:
  N1) HA ($HA_URL): Settings -> People -> Users -> Add user. Username
      '$SYNC_USER', Administrator OFF, "Local access only" ON (older HA: "Can
      only log in from the local network"), a generated password WITHOUT
      spaces, quotes or backslashes. NOT a read-only account: the add-on's
      nginx accepts any valid HA user and then the whole Node-RED admin API
      is open. The password lives only in $SYNC_ENV.
      Also: Settings -> Add-ons -> Node-RED -> Configuration -> 'ssl' OFF,
      Save, Info -> Restart. The add-on ships 'ssl: true'; the sync speaks
      plain HTTP like every other hop to this host.
  N2) The git host: create the PRIVATE repo $SYNC_REPO, EMPTY (no
      README), in NO access token's repository list.
  N3) Re-run bin/nr-setup.sh: it clones, asks for the password, shows what
      the first push would send and asks before enabling anything.
  N4) Password manager: the add-on's credential_secret (HA -> Settings ->
      Add-ons -> Node-RED -> Configuration). Neither repo backs it up, and
      without it flows_cred.json cannot be decrypted.

 The reviewer key:
  N5) Step 9 above printed the reviewer key once. On Hyperion, paste it at
      the hidden prompt. The file is chaperone's, mode 0600, no newline:
        read -rsp 'reviewer key: ' K; echo
        printf '%s' "\$K" | sudo -u chaperone sh -c 'umask 077 && cat > /etc/chaperone/secrets/reviewer_key'; unset K
        sudo systemctl restart creche-chaperone.service
      A root-owned file there blocks the write and makes chaperone refuse
      to start: sudo rm it first. Readers: the nodered and nodered-gated
      entries of /etc/chaperone/install.toml, as REVIEW_LITELLM_KEY.

 The approval instance, kept for a rebuild:
  1) HA ($HA_URL): create user 'agent-approvals' (an ADMINISTRATOR —
     measured 2026-09-16: a non-admin cannot subscribe to
     mobile_app_notification_action), log in as it once, Profile -> Security
     -> long-lived access token. Different from HA_TOKEN (the chaperone's).
  2) The git host: create the PRIVATE repo $FLOWS_REPO (human-only: it
     must appear in NO access token's repository list) and push the prepared local
     repo from your workstation:
       cd <local approval-flows repo> && git remote add origin $FLOWS_REPO && git push -u origin main
  3) Arcane stack, on this host:
       sudo install -d -o "\$(id -un)" -g "\$(id -gn)" -m 0750 /srv/arcane   # once, if absent
       git clone $FLOWS_REPO $STACK_DIR
       install -m 0600 $STACK_ENV_COPY $STACK_DIR/.env
       edit $STACK_DIR/compose.yaml: replace <digest> (its README, step 2)
       then in Arcane (on this host, port 3552) import the stack from
       $STACK_DIR, or: docker compose -f $STACK_DIR/compose.yaml up -d
     Re-run this script: step 3 must then read 127.0.0.1 != 000, $HYPERION_IP == 000.
  4) Import the flow through a tunnel, from your workstation:
       ssh -L 1881:127.0.0.1:1881 <this host>
       open http://127.0.0.1:1881   (the adminAuth user from data/settings.js)
     Manage palette -> install node-red-contrib-home-assistant-websocket;
     Import -> flows.json from the repo; then set the credentials its README
     lists: the "Home Assistant" server node (host $HA_URL +
     the agent-approvals token from 1), and on the "Agent approvals" tab
     (double-click its header -> Edit flow -> Environment):
       RESUME_TOKEN   = grep '^RESUME_TOKEN=' $ENV_FILE | cut -d= -f2-
       APPROVAL_TOKEN = grep '^APPROVAL_TOKEN=' $HOOKS_ENV | cut -d= -f2-
       PHONE_USER_ID  = the value on the old tab of the HA Node-RED (or
                        context.user_id of a real notification-action event)
     Deploy. Until this, every push 401s at the hook and lands in the
     pusher's journal as FAILED — the old HA path still carries the phone.
  5) Nothing to restart. The one reader is the oneshot watchdog of
     creche-release, on a timer, and it takes the new values on its next run.
  6) Prove the leg by hand: run the watchdog of creche-release once, then make
     a check fail and watch the phone.
  7) Only after that: delete the "Agent approvals" tab on the HA Node-RED and delete automation.agent_approval_push_v2 in HA.
EOF
