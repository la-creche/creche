#!/usr/bin/env bash
# Test for require_root_owned() in bin/creche-handover: root refuses to
# execute, or to decrypt into its own environment, any path operator-side code
# can write. sops itself was <operator>:kvm 0775 on the host, measured
# 2026-09-16.
#
# Runs anywhere: `stat` is a fake on PATH, so this needs no GNU stat and no
# root.
#
# Run: bash bin/tests/test_handover_wrapper_owner.sh
set -uo pipefail
HERE="$(cd "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/../creche-handover"

FAILS=0
pass() { printf 'PASS: %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*"; FAILS=$((FAILS + 1)); }

# Source only the function under test; the script itself execs and needs root.
FN="$(sed -n '/^require_root_owned()/,/^}/p' "$SCRIPT")"
[[ -n "$FN" ]] || {
  fail "require_root_owned() not found in $SCRIPT"
  printf 'GATE: FAIL (%s)\n' "$FAILS"
  exit 1
}
eval "$FN"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# A `stat` that answers whatever the case under test needs, so the same
# assertions run on macOS and on the host.
mkdir -p "$WORK/bin"
cat > "$WORK/bin/stat" <<'FAKE'
#!/usr/bin/env bash
[[ -e "${!#}" ]] || exit 1
if [[ -d "${!#}" && -n "${FAKE_STAT_DIR:-}" ]]; then
  printf '%s\n' "$FAKE_STAT_DIR"
  exit 0
fi
printf '%s\n' "$FAKE_STAT"
FAKE
chmod 0755 "$WORK/bin/stat"
PATH="$WORK/bin:$PATH"

TARGET="$WORK/thing"
: > "$TARGET"

check() {
  local want="$1" answer="$2" what="$3" rc
  FAKE_STAT="$answer" require_root_owned "$TARGET"
  rc=$?
  [[ "$rc" == "$want" ]] && pass "$what" || fail "$what (rc=$rc, wanted $want)"
}

check 0 "root:root 755" "root:root 0755 passes"
check 0 "root:root 600" "root:root 0600 passes"
check 0 "root:root 644" "root:root 0644 passes"
check 1 "operator:kvm 775" "operator:kvm 0775 is refused"
check 1 "root:root 775" "group-writable is refused"
check 1 "root:root 646" "other-writable is refused"
check 1 "root:agents 664" "another group's writable file is refused"

FAKE_STAT="root:root 755" require_root_owned "$WORK/absent"
[[ "$?" == "1" ]] && pass "a missing path is refused" || fail "a missing path passed"

# The directories above the sops file. A directory somebody else can write
# is a file they can swap after the guard looked, so every one of them up to
# `/` must be root's too. The fake `stat` answers $FAKE_STAT_DIR for a
# directory when that is set.
for script in "$SCRIPT" "$HERE/../creche-handover-intake"; do
  CHAIN="$(sed -n '/^require_root_chain()/,/^}/p' "$script")"
  [[ -n "$CHAIN" ]] || { fail "require_root_chain() not found in $(basename "$script")"; continue; }
  eval "$CHAIN"

  mkdir -p "$WORK/site/infra"
  : > "$WORK/site/infra/secrets.enc.env"

  FAKE_STAT="root:root 644" FAKE_STAT_DIR="root:root 755" \
    require_root_chain "$WORK/site/infra/secrets.enc.env"
  [[ "$?" == "0" ]] && pass "$(basename "$script"): a chain root owns passes" \
    || fail "$(basename "$script"): a chain root owns was refused"

  FAKE_STAT="root:root 644" FAKE_STAT_DIR="operator:operator 755" \
    require_root_chain "$WORK/site/infra/secrets.enc.env"
  [[ "$?" == "1" ]] && pass "$(basename "$script"): a directory the operator owns is refused" \
    || fail "$(basename "$script"): a directory the operator owns passed"

  FAKE_STAT="root:root 644" FAKE_STAT_DIR="root:root 775" \
    require_root_chain "$WORK/site/infra/secrets.enc.env"
  [[ "$?" == "1" ]] && pass "$(basename "$script"): a group-writable directory is refused" \
    || fail "$(basename "$script"): a group-writable directory passed"

  grep -q 'require_root_chain "$SITE_SECRETS"' "$script" \
    && pass "$(basename "$script") guards the directories above the link" \
    || fail "$(basename "$script") never guards the directories above the link"
done

# Every path root executes or decrypts must reach BOTH guards, not only
# the first one: the file's own owner and mode, and every directory above
# it. The release wrapper names each path; the intake loops over `$path`.
for name in SOPS SECRETS EXECUTOR; do
  grep -q "require_root_owned \"\$$name\"" "$SCRIPT" \
    && pass "the release wrapper guards \$$name" \
    || fail "the release wrapper never guards \$$name"
  grep -q "require_root_chain \"\$$name\"" "$SCRIPT" \
    && pass "the release wrapper guards the directories above \$$name" \
    || fail "the release wrapper never guards the directories above \$$name"
done

INTAKE_SCRIPT="$HERE/../creche-handover-intake"
grep -q '^for path in "$INTAKE" "$SOPS" "$SECRETS"; do$' "$INTAKE_SCRIPT" \
  && grep -q 'require_root_owned "$path" || ! require_root_chain "$path"' "$INTAKE_SCRIPT" \
  && pass "the intake wrapper guards every path and the directories above it" \
  || fail "the intake wrapper leaves a path or a directory chain unguarded"

# The sops file is the site's, at one path no checkout holds. Root judges
# the file a link there names: the guard must get the resolved path, and
# neither wrapper may read a file of the deployed tree.
for script in "$SCRIPT" "$INTAKE_SCRIPT"; do
  name="$(basename "$script")"
  grep -q '^SITE_SECRETS=/etc/agent-control/secrets.enc.env$' "$script" \
    && pass "$name reads the site's sops file" \
    || fail "$name does not read /etc/agent-control/secrets.enc.env"
  grep -q '^SECRETS="$(readlink -f "$SITE_SECRETS" 2>/dev/null)" || SECRETS="$SITE_SECRETS"$' "$script" \
    && pass "$name resolves a link before the guard" \
    || fail "$name hands the guard an unresolved path"
  grep -v '^#' "$script" | grep -q '/opt/creche' \
    && fail "$name still reads the deployed tree" \
    || pass "$name reads nothing of the deployed tree"
done

# A link's own mode is 777, which the guard refuses: resolving first is
# what lets a deployment link the path. The resolved file is what is judged.
ln -s "$TARGET" "$WORK/link"
RESOLVED="$(readlink -f "$WORK/link")"
[[ "$RESOLVED" == "$(readlink -f "$TARGET")" ]] \
  && pass "a link resolves to the file it names" \
  || fail "a link resolved to $RESOLVED"

[[ "$FAILS" -eq 0 ]] && printf 'GATE: PASS\n' || {
  printf 'GATE: FAIL (%s)\n' "$FAILS"
  exit 1
}
