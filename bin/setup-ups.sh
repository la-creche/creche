#!/usr/bin/env bash
# ROOT — on Hyperion, once, after the branch is deployed:
#   sudo /opt/creche/bin/setup-ups.sh
# OPERATOR — the read-only half, no sudo, for the power-loss runbook:
#   /opt/creche/bin/setup-ups.sh --check
#
# Setup family (bin/AGENTS.md): idempotent. Every file is rewritten from the
# same template on every run; the two values minted once (the upsd password
# and the card's SNMP community) are read back from the files and kept.
#
# WHAT THIS IS. Hyperion has no UPS software: three unclean stops in
# 2026-09. `ups1` (an SNMP card at the address UPS1_CARD) feeds it. This
# installs NUT, points `snmp-ups` at that card, and makes `upsmon` shut the
# host down cleanly in two cases:
#
#   mains fails ──> ups1 on battery ──> 5 min timer (upssched) ──> shutdown
#                                  └──> low battery (UPS flag) ──> shutdown
#
# General Sherman keeps its own NUT. Nothing here depends on it.
#
# WHAT IT REFUSES TO DO. It never tells the UPS to cut its output: the UPS
# also feeds the router, the switch and the rack vent. `upsmon` runs as a
# SECONDARY, so it never writes POWERDOWNFLAG, and Debian's system-shutdown
# hook (`upsmon -K`) therefore never runs `upsdrvctl shutdown`.
#
# RUN_AS_USER root, on purpose: the 5 minute timer ends in `upsmon -c fsd`,
# which signals upsmon's root parent. The default user `nut` gets EPERM.
#
# Prerequisites, copy-pasteable:
#   sudo creche-deploy <branch>   # /opt/creche/bin/ups-onbatt.sh
#   ( umask 077 && printf '%s\n' 'THE-COMMUNITY' > /root/ups1.community )
#   sudo UPS1_CARD=<card address> UPS1_COMMUNITY_FILE=/root/ups1.community \
#     /opt/creche/bin/setup-ups.sh
# UPS1_CARD has no default: the address of the card is the operator's.
# Without the file, and on a terminal, the script asks for the community
# once, silently. The value lives in /etc/nut/ups.conf (root:nut 0640).
# A rerun keeps it; a rerun WITH the file replaces it (a mistyped one).
set -euo pipefail

# A TEST SEAM, not a permission. `bin/tests/test_setup_ups.py` points every
# path below inside a temp directory and is REFUSED under root.
TEST_ROOT="${SETUP_UPS_TEST_ROOT:-}"

UPS_NAME=ups1
UPS_CARD="${UPS1_CARD:-}"
UPS_DESC="feeds Hyperion"
#: Seconds on battery before the forced shutdown: 5 minutes. Hyperion's
#: load is 10 to 36 % of the UPS, so the battery has plenty left, and the
#: router and switch on the same UPS keep their share.
ONBATT_SHUTDOWN_S=300
UPSD_USER=upsmon
NUT_GROUP=nut
NUT_ETC="$TEST_ROOT/etc/nut"
#: Debian's tmpfiles create this for upssched (nut:nut 0770).
SCHED_RUN=/run/nut/upssched
#: The timer's script, in the deployed tree. bin/AGENTS.md: production
#: runs repo scripts from /opt/creche/bin, never from the operator's tree.
CMDSCRIPT="$TEST_ROOT/opt/creche/bin/ups-onbatt.sh"
UNITS="nut-driver-enumerator.service nut-server.service nut-monitor.service nut.target"
DRIVER_UNIT="nut-driver@$UPS_NAME.service"
UPSC_WAIT_S=60

say() { printf '\n[ROOT] %s\n' "$*"; }
die() { printf 'setup-ups: %s\n' "$*" >&2; exit 1; }

guard_user() {
  local uid
  uid="$(id -u)"

  if [[ -n "$TEST_ROOT" ]]; then
    [[ "$uid" != 0 ]] || die "SETUP_UPS_TEST_ROOT is set and this is root. Refusing."
    return 0
  fi

  [[ "$uid" == 0 ]] || die "run via sudo"
}

# Read `key = value` back from one of the files below, "" when absent.
conf_value() {
  local file="$1" key="$2"
  [[ -f "$file" ]] || return 0
  sed -n -E "s/^[[:space:]]*${key}[[:space:]]*=[[:space:]]*//p" "$file" | head -n1
}

# One idiom for every file: a 0600 temp file under umask 077, then `install`
# with the final owner, group and mode. No world-readable window
# (bin/AGENTS.md, Secrets).
write_conf() {
  local target="$1" mode="$2" tmp
  tmp="$(mktemp)"
  ( umask 077 && cat > "$tmp" )
  install -m "$mode" -o root -g "$NUT_GROUP" "$tmp" "$target"
  rm -f -- "$tmp"
}

# Order: the file the operator names, then the value already in ups.conf, then the
# prompt. The file first, so a mistyped community has a way out.
community() {
  local value
  if [[ -n "${UPS1_COMMUNITY_FILE:-}" ]]; then
    [[ -r "$UPS1_COMMUNITY_FILE" ]] || die "$UPS1_COMMUNITY_FILE: not readable"
    value="$(head -n1 "$UPS1_COMMUNITY_FILE")"
    [[ -n "$value" ]] || die "$UPS1_COMMUNITY_FILE is empty"
    printf '%s' "$value"
    return 0
  fi

  value="$(conf_value "$NUT_ETC/ups.conf" community)"
  if [[ -n "$value" ]]; then
    printf '%s' "$value"
    return 0
  fi

  [[ -t 0 ]] || die "no community: set UPS1_COMMUNITY_FILE (see the header)"
  read -rs -p "SNMP read community of the card at $UPS_CARD: " value </dev/tty
  printf '\n' >&2
  [[ -n "$value" ]] || die "empty community"
  printf '%s' "$value"
}

upsd_password() {
  local value
  value="$(conf_value "$NUT_ETC/upsd.users" password)"
  [[ -n "$value" ]] || value="$(openssl rand -hex 16)"
  printf '%s' "$value"
}

check() {
  local unit
  # Not the enumerator: a oneshot reads "inactive" when it worked.
  say "units"
  for unit in nut-server.service nut-monitor.service nut.target $DRIVER_UNIT; do
    printf '%-36s %s\n' "$unit" "$(systemctl is-active "$unit" 2>/dev/null || true)"
  done

  say "$UPS_NAME"
  upsc "$UPS_NAME" 2>&1 | grep -E '^(ups\.status|battery\.charge|ups\.load|input\.voltage):' || true

  say "killpower flag (must say: Power down flag is not set)"
  upsmon -K 2>&1 || true
}

if [[ "${1:-}" == "--check" ]]; then
  check
  exit 0
fi

guard_user
[[ -n "$UPS_CARD" ]] || die "set UPS1_CARD to the address of the SNMP card (see the header)"
[[ -x "$CMDSCRIPT" ]] || die "$CMDSCRIPT missing: deploy the branch first"

say "0. packages"
if ! command -v upsmon >/dev/null || ! command -v upsd >/dev/null || [[ ! -e /lib/nut/snmp-ups && -z "$TEST_ROOT" ]]; then
  # One unrelated repository failing its update (Hyperion, 2026-10-01) must
  # not stop a host whose present lists hold the packages. The install
  # decides: it still stops everything when it fails.
  apt-get update -qq || echo "apt-get update failed for a repository: trying the present lists"
  apt-get install -y -qq nut-client nut-server nut-snmp
fi
getent group "$NUT_GROUP" >/dev/null || die "no group $NUT_GROUP after install"

say "1. $NUT_ETC"
install -d -m 0750 -o root -g "$NUT_GROUP" "$NUT_ETC"
COMMUNITY="$(community)"
PASSWORD="$(upsd_password)"

# nut.conf is sourced by Debian's system-shutdown hook: keep it plain shell.
write_conf "$NUT_ETC/nut.conf" 0644 <<EOF
# Written by bin/setup-ups.sh. Rerun it, do not edit.
MODE=standalone
EOF

write_conf "$NUT_ETC/ups.conf" 0640 <<EOF
# Written by bin/setup-ups.sh. Rerun it, do not edit.
[$UPS_NAME]
    driver = snmp-ups
    port = $UPS_CARD
    mibs = ietf
    snmp_version = v2c
    community = $COMMUNITY
    pollfreq = 30
    desc = "$UPS_DESC"
EOF

write_conf "$NUT_ETC/upsd.conf" 0640 <<EOF
# Written by bin/setup-ups.sh. Rerun it, do not edit.
LISTEN 127.0.0.1 3493
EOF

write_conf "$NUT_ETC/upsd.users" 0640 <<EOF
# Written by bin/setup-ups.sh. Rerun it, do not edit.
[$UPSD_USER]
    password = $PASSWORD
    upsmon secondary
EOF

# SECONDARY: shuts down on low battery or on FSD, never writes
# POWERDOWNFLAG, so the halt hook never cuts the UPS output (header).
write_conf "$NUT_ETC/upsmon.conf" 0640 <<EOF
# Written by bin/setup-ups.sh. Rerun it, do not edit.
RUN_AS_USER root
MONITOR $UPS_NAME@localhost 1 $UPSD_USER $PASSWORD secondary
MINSUPPLIES 1
SHUTDOWNCMD "/sbin/shutdown -h +0"
NOTIFYCMD /sbin/upssched
POLLFREQ 5
POLLFREQALERT 5
HOSTSYNC 15
DEADTIME 15
POWERDOWNFLAG $NUT_ETC/no_killpower
NOTIFYFLAG ONLINE   SYSLOG+EXEC
NOTIFYFLAG ONBATT   SYSLOG+EXEC
NOTIFYFLAG LOWBATT  SYSLOG+EXEC
NOTIFYFLAG FSD      SYSLOG
NOTIFYFLAG COMMOK   SYSLOG
NOTIFYFLAG COMMBAD  SYSLOG
NOTIFYFLAG SHUTDOWN SYSLOG
NOTIFYFLAG REPLBATT SYSLOG
NOTIFYFLAG NOCOMM   SYSLOG
RBWARNTIME 43200
NOCOMMWARNTIME 300
FINALDELAY 5
EOF

# The 5 minute rule. ONLINE inside the window cancels the timer.
write_conf "$NUT_ETC/upssched.conf" 0640 <<EOF
# Written by bin/setup-ups.sh. Rerun it, do not edit.
CMDSCRIPT ${CMDSCRIPT#"$TEST_ROOT"}
PIPEFN $SCHED_RUN/upssched.pipe
LOCKFN $SCHED_RUN/upssched.lock
AT ONBATT * START-TIMER onbatt $ONBATT_SHUTDOWN_S
AT ONLINE * CANCEL-TIMER onbatt online
AT LOWBATT * EXECUTE lowbatt
EOF
echo "6 files written under $NUT_ETC (secrets 0640 root:$NUT_GROUP)"

say "2. units"
systemctl daemon-reload
# shellcheck disable=SC2086
systemctl enable $UNITS
# The enumerator turns ups.conf into $DRIVER_UNIT and starts it.
systemctl restart nut-driver-enumerator.service
systemctl restart nut-server.service nut-monitor.service

say "3. $UPS_NAME answers"
for _ in $(seq 1 "$UPSC_WAIT_S"); do
  if upsc "$UPS_NAME" ups.status >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
upsc "$UPS_NAME" ups.status >/dev/null 2>&1 || die "$UPS_NAME silent after ${UPSC_WAIT_S}s: read journalctl -u $DRIVER_UNIT. Wrong community? Rerun with UPS1_COMMUNITY_FILE=<a 0600 file that holds it>"

check
say "DONE. Test: pull the mains plug of $UPS_NAME; a clean shutdown follows in $ONBATT_SHUTDOWN_S s."
