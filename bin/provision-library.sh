#!/usr/bin/env bash
# Provision the library for one corpus (OPERATOR, no sudo): image, sandbox with
# the corpus as its only workspace, TEI-only egress, timer unit.
#   ./bin/provision-library.sh notes           # vault scope (default)
#   ./bin/provision-library.sh agent-control code
#   ./bin/provision-library.sh alpha test      # gate fixture, no timer
# Prerequisite: AGENT_LAN_ADDRESS in /etc/agent-control/site.env, or
# LAN_ADDRESS in the environment.
set -euo pipefail
# sbx phones home on every invocation and that, not the work asked of it, is
# most of what each call costs. Set here because nothing that starts this
# script sets it.
export SBX_NO_TELEMETRY="${SBX_NO_TELEMETRY:-1}"
(( $# >= 1 && $# <= 2 )) || { echo "usage: $0 <name> [vault|code|test]" >&2; exit 2; }
NAME="$1"
CORPUS="${2:-vault}"
# The vault keeps bare index names (renaming would force a full re-embed);
# every later corpus is namespaced so a repo cannot collide with a scope.
# UNIT empty = no timer: a gate fixture does not need recurring background
# work forever, and the reaper already ignores non-vault, non-code roots.
case "$CORPUS" in
  vault) SCOPE_DIR="/srv/agents/vault/$NAME"; IDX_NAME="$NAME";      UNIT="index" ;;
  code)  SCOPE_DIR="/srv/agents/code/$NAME";  IDX_NAME="code-$NAME"; UNIT="index-code" ;;
  test)  SCOPE_DIR="/srv/agents/test/$NAME";  IDX_NAME="test-$NAME"; UNIT="" ;;
  *) echo "unknown corpus $CORPUS; use vault, code or test" >&2; exit 2 ;;
esac
IDX_DIR="/srv/agents/state/index/$IDX_NAME"
REPO="$(cd "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
. "$REPO/bin/lib/envfile.sh"

# The host's LAN address, where TEI answers: the sandbox's one egress hole,
# and fixed into the image at build time (library/Dockerfile). `LAN_ADDRESS`
# wins, then the site file.
# `AGENT_SITE_FILE` names another site file, which is how a test points at a
# fixture. No default: a default would be somebody's host.
SITE_FILE="${AGENT_SITE_FILE:-/etc/agent-control/site.env}"
LAN_ADDRESS="${LAN_ADDRESS:-$(envfile_value "$SITE_FILE" AGENT_LAN_ADDRESS || true)}"
[[ -n "$LAN_ADDRESS" ]] || { echo "no AGENT_LAN_ADDRESS in $SITE_FILE" >&2; exit 1; }

[[ -d "$SCOPE_DIR" ]] || { echo "no corpus dir $SCOPE_DIR" >&2; exit 2; }
mkdir -p "$IDX_DIR"
REGISTRY="127.0.0.1:5000"
VER="$(python3 -c "import tomllib;print(tomllib.load(open('$REPO/library/pyproject.toml','rb'))['project']['version'])")"
# The image keeps its old name: every index-* sandbox on a host was
# created from it, and a new name would orphan them.
IMAGE_NAME="agent-indexer"
IMG="$REGISTRY/$IMAGE_NAME:$VER"
SBX="index-$IDX_NAME"

echo "[1] image $IMG"
docker build -q --build-arg "AGENT_LAN_ADDRESS=$LAN_ADDRESS" -t "$IMG" "$REPO/library/" >/dev/null
docker push -q "$IMG" >/dev/null
echo "[2] sandbox $SBX (scope ro + index rw, egress: TEI only)"
if ! sbx ls 2>/dev/null | grep -q "^$SBX "; then
  # scope is READ-ONLY to the library; only the external index dir is rw.
  # sbx requires the PRIMARY (first) workspace to be read/write, so the
  # index dir leads and the scope rides second with :ro.
  sbx create shell "$IDX_DIR" "$SCOPE_DIR:ro" -t "$IMG" --name "$SBX" -m 1g --cpus 2 -q
fi
sbx policy allow network --sandbox "$SBX" "$LAN_ADDRESS:8085" >/dev/null 2>&1 || true
# The built-in `shell` kit attaches its own `allow openrouter.ai` to every
# sandbox — a LiteLLM bypass `caregiver` already denies for every family
# (caregiver/src/caregiver/driver.py, KIT_EXTRA_EGRESS). Deny beats
# allow. Unguarded on purpose: a repeat deny exits 0 ("Already covered"), so
# a failure here is real, and a sandbox left without it is exactly what
# sbx-drift-check.sh alarms on.
sbx policy deny network --sandbox "$SBX" openrouter.ai >/dev/null
# `sbx policy check` exits non-zero on Denied, so under `set -euo pipefail`
# the checks must be captured (the denied ones are the DESIRED outcome).
ALLOW=$(sbx policy check network --sandbox "$SBX" "$LAN_ADDRESS:8085" 2>&1 | head -1 || true)
DENY=$(sbx policy check network --sandbox "$SBX" "$LAN_ADDRESS:4000" 2>&1 | head -1 || true)
KIT=$(sbx policy check network --sandbox "$SBX" openrouter.ai 2>&1 | head -1 || true)
printf '%s\n%s\n%s\n' "$ALLOW" "$DENY" "$KIT"
case "$ALLOW" in Allowed*) ;; *) echo "expected Allowed for TEI, got: $ALLOW" >&2; exit 1 ;; esac
case "$DENY" in Denied*) ;; *) echo "expected Denied for LiteLLM, got: $DENY" >&2; exit 1 ;; esac
case "$KIT" in Denied*) ;; *) echo "expected Denied for openrouter.ai, got: $KIT" >&2; exit 1 ;; esac
BUILD="sbx exec $SBX -- sh -c 'index-scope $SCOPE_DIR $IDX_DIR; rc=\$?; sleep 8; exit \$rc'"
if [[ -z "$UNIT" ]]; then
  echo "[3] units: skipped ($CORPUS corpus); build on demand with"
  echo "  $BUILD"
  echo "provisioned: $SBX (no timer)"
  exit 0
fi
echo "[3] units"
mkdir -p ~/.config/systemd/user
install -m 0644 "$REPO/systemd/$UNIT@.service" "$REPO/systemd/$UNIT@.timer" ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now "$UNIT@$NAME.timer"
systemctl --user list-timers "$UNIT@$NAME.timer" --no-pager | head -3
echo "provisioned: run now with  systemctl --user start --no-block $UNIT@$NAME"
