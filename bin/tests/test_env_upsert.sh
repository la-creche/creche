#!/usr/bin/env bash
# Regression test for the env-file wipe (bin/AGENTS.md, Tests): a `cat >`
# into the control-plane env file wipes every other key on a re-run, and
# envfile_upsert (bin/lib/envfile.sh) merges instead. This test seeds a
# fixture file with several keys, upserts one of them twice, and asserts
# the *other* keys are still there, byte for byte, and the file ends up
# 0600.
#
# Run: bash bin/tests/test_env_upsert.sh
set -uo pipefail
HERE="$(cd "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/../lib/envfile.sh"

FAILS=0
pass() { printf 'PASS: %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*"; FAILS=$((FAILS + 1)); }

TMPDIR="$(mktemp -d "${TMPDIR:-/tmp}/env_upsert_test.XXXXXX")"
trap 'rm -rf "$TMPDIR"' EXIT
ENV_FILE="$TMPDIR/env"

# Fixture: 11 keys of the kind such an env file holds (values are fakes).
cat > "$ENV_FILE" <<'EOF'
RESUME_TOKEN=abc123
ADMIN_TOKEN=def456
HA_WEBHOOK_URL=http://192.0.2.20:8123/api/webhook/agent-approval-abcdef012345
LITELLM_MASTER_KEY=sk-master-000
UI_ACCESS_KEY=uikey000
UI_BIND=127.0.0.1:8320
REGISTRY_WEBHOOK_SECRET=whsec000
GRANTS_TOKEN=grants000
PEP_CATALOG_TOKEN=catalog000
VIKUNJA_ADMIN_TOKEN=vik000
AGENTCTL_RUNNER_IMAGE=127.0.0.1:5000/agent-runner:0.1.0
EOF
chmod 0644 "$ENV_FILE"   # simulate the pre-fix mode (the wipe's second-order defect)
BEFORE_LINES=$(wc -l < "$ENV_FILE" | tr -d ' ')
BEFORE_KEYS="$(cut -d= -f1 "$ENV_FILE" | sort)"

# Run 1: upsert an existing key to a new value.
envfile_upsert "$ENV_FILE" AGENTCTL_RUNNER_IMAGE "127.0.0.1:5000/agent-runner:0.2.0"

AFTER1_LINES=$(wc -l < "$ENV_FILE" | tr -d ' ')
AFTER1_KEYS="$(cut -d= -f1 "$ENV_FILE" | sort)"
[[ "$AFTER1_LINES" -eq "$BEFORE_LINES" ]] \
  && pass "line count unchanged after first upsert ($AFTER1_LINES)" \
  || fail "line count changed after first upsert: $BEFORE_LINES -> $AFTER1_LINES"
[[ "$AFTER1_KEYS" == "$BEFORE_KEYS" ]] \
  && pass "key set unchanged after first upsert" \
  || fail "key set changed after first upsert"
grep -q '^AGENTCTL_RUNNER_IMAGE=127.0.0.1:5000/agent-runner:0.2.0$' "$ENV_FILE" \
  && pass "target key updated to new value" \
  || fail "target key not updated"
grep -q '^RESUME_TOKEN=abc123$' "$ENV_FILE" \
  && pass "unrelated key (RESUME_TOKEN) survived first upsert" \
  || fail "RESUME_TOKEN lost or changed after first upsert"

MODE1="$(stat -c '%a' "$ENV_FILE" 2>/dev/null || stat -f '%Lp' "$ENV_FILE" 2>/dev/null)"
[[ "$MODE1" == "600" ]] && pass "mode 0600 after first upsert" || fail "mode is $MODE1, expected 600"

# Run 2 (the actual regression): upsert the SAME key again with a third
# value, exactly what a second run of the site's bootstrap script does.
# Every key from run 1 must still be present afterwards -- this is what
# `cat >` broke.
envfile_upsert "$ENV_FILE" AGENTCTL_RUNNER_IMAGE "127.0.0.1:5000/agent-runner:0.3.0"

AFTER2_LINES=$(wc -l < "$ENV_FILE" | tr -d ' ')
AFTER2_KEYS="$(cut -d= -f1 "$ENV_FILE" | sort)"
[[ "$AFTER2_LINES" -eq "$BEFORE_LINES" ]] \
  && pass "line count unchanged after second upsert ($AFTER2_LINES)" \
  || fail "line count changed after second upsert: $BEFORE_LINES -> $AFTER2_LINES"
[[ "$AFTER2_KEYS" == "$BEFORE_KEYS" ]] \
  && pass "key set unchanged after second upsert (the env-wipe regression)" \
  || fail "key set changed after second upsert -- the env wipe regressed"
for k in RESUME_TOKEN ADMIN_TOKEN HA_WEBHOOK_URL LITELLM_MASTER_KEY UI_ACCESS_KEY \
         UI_BIND REGISTRY_WEBHOOK_SECRET GRANTS_TOKEN PEP_CATALOG_TOKEN VIKUNJA_ADMIN_TOKEN; do
  grep -q "^${k}=" "$ENV_FILE" || fail "key $k missing after second upsert"
done
grep -q '^AGENTCTL_RUNNER_IMAGE=127.0.0.1:5000/agent-runner:0.3.0$' "$ENV_FILE" \
  && pass "target key updated again on second upsert" \
  || fail "target key not updated on second upsert"

MODE2="$(stat -c '%a' "$ENV_FILE" 2>/dev/null || stat -f '%Lp' "$ENV_FILE" 2>/dev/null)"
[[ "$MODE2" == "600" ]] && pass "mode 0600 after second upsert" || fail "mode is $MODE2, expected 600"

# envfile_append_once: must not touch a value that already exists.
envfile_append_once "$ENV_FILE" RESUME_TOKEN "should-not-appear"
grep -q '^RESUME_TOKEN=abc123$' "$ENV_FILE" \
  && pass "append_once left an existing key untouched" \
  || fail "append_once overwrote an existing key"

# envfile_upsert on a brand new file: created 0600, not world/group readable.
NEWFILE="$TMPDIR/fresh_env"
( umask 022; : ) # sanity: confirm umask manipulation below actually matters
envfile_upsert "$NEWFILE" SOME_KEY "some-value"
MODE3="$(stat -c '%a' "$NEWFILE" 2>/dev/null || stat -f '%Lp' "$NEWFILE" 2>/dev/null)"
[[ "$MODE3" == "600" ]] && pass "fresh file created 0600" || fail "fresh file mode is $MODE3, expected 600"
grep -q '^SOME_KEY=some-value$' "$NEWFILE" && pass "fresh file got the key" || fail "fresh file missing the key"

# --- envfile_value: one key, never a `source` -------------------------------

VALUE_FILE="$TMPDIR/hooks.env"
cat > "$VALUE_FILE" <<'EOF'
APPROVAL_URL=http://127.0.0.1:1881/hook/approval
APPROVAL_TOKEN=first
LITELLM_MASTER_KEY=sk-base64==
APPROVAL_TOKEN=second
EOF

[[ "$(envfile_value "$VALUE_FILE" APPROVAL_URL)" == "http://127.0.0.1:1881/hook/approval" ]] \
  && pass "envfile_value reads a plain value" \
  || fail "envfile_value did not read APPROVAL_URL"

# A value with '=' in it (base64 padding) survives: cut -d= -f2- keeps the tail.
[[ "$(envfile_value "$VALUE_FILE" LITELLM_MASTER_KEY)" == "sk-base64==" ]] \
  && pass "envfile_value keeps a value that contains '='" \
  || fail "envfile_value truncated a value at its first '='"

# Last assignment wins, the way a shell would read the file.
[[ "$(envfile_value "$VALUE_FILE" APPROVAL_TOKEN)" == "second" ]] \
  && pass "envfile_value takes the last assignment" \
  || fail "envfile_value did not take the last assignment"

[[ -z "$(envfile_value "$VALUE_FILE" NO_SUCH_KEY)" ]] \
  && pass "envfile_value prints nothing for an absent key" \
  || fail "envfile_value invented a value"

[[ -z "$(envfile_value "$TMPDIR/not-a-file" APPROVAL_URL)" ]] \
  && pass "envfile_value prints nothing for an absent file" \
  || fail "envfile_value read a file that is not there"

# It must not export: `set -a` on a shared env file drags every other daemon
# secret into the caller's environment.
[[ -z "${APPROVAL_TOKEN:-}" ]] \
  && pass "envfile_value exported nothing" \
  || fail "envfile_value exported APPROVAL_TOKEN into the caller"

# --- envfile_hook_value: the new home first, the old one as a fallback ------

OLD_FILE="$TMPDIR/materializer-env"
cat > "$OLD_FILE" <<'EOF'
APPROVAL_TOKEN=from-the-old-file
LITELLM_MASTER_KEY=sk-old
EOF

[[ "$(envfile_hook_value "$VALUE_FILE" "$OLD_FILE" APPROVAL_TOKEN 2>/dev/null)" == "second" ]] \
  && pass "hook_value prefers the rework's own file" \
  || fail "hook_value read the deprecated file while the new one had the key"

[[ -z "$(envfile_hook_value "$VALUE_FILE" "$OLD_FILE" APPROVAL_TOKEN 2>&1 >/dev/null)" ]] \
  && pass "hook_value says nothing when it reads the new file" \
  || fail "hook_value warned about a file it did not read"

EMPTY_FILE="$TMPDIR/empty-hooks.env"
: > "$EMPTY_FILE"
[[ "$(envfile_hook_value "$EMPTY_FILE" "$OLD_FILE" APPROVAL_TOKEN 2>/dev/null)" == "from-the-old-file" ]] \
  && pass "hook_value falls back to the deprecated file" \
  || fail "hook_value did not fall back"

NOTICE="$(envfile_hook_value "$EMPTY_FILE" "$OLD_FILE" APPROVAL_TOKEN 2>&1 >/dev/null)"
case "$NOTICE" in
  *DEPRECATED*"$OLD_FILE"*) pass "the fallback says the old path is deprecated" ;;
  *) fail "the fallback notice does not name the old path: $NOTICE" ;;
esac

# The notice is a WARNING about a path, never about a value.
case "$NOTICE" in
  *from-the-old-file*) fail "the fallback notice printed the value" ;;
  *) pass "the fallback notice printed no value" ;;
esac

[[ -z "$(envfile_hook_value "$EMPTY_FILE" "$OLD_FILE" NO_SUCH_KEY 2>/dev/null)" ]] \
  && pass "hook_value prints nothing when neither file has the key" \
  || fail "hook_value invented a value"

echo
if [[ "$FAILS" -eq 0 ]]; then
  echo "GATE: PASS"
  exit 0
else
  echo "GATE: FAIL ($FAILS)"
  exit 1
fi
