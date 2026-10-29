#!/usr/bin/env bash
# Shared KEY=VALUE env-file helpers for bootstrap/setup/migrate scripts
# (bin/AGENTS.md "Idempotent" contract: every step checks before it writes).
# Sourced, never executed:
#
#   . "$(dirname -- "${BASH_SOURCE[0]}")/lib/envfile.sh"
#   envfile_upsert "$ENV_FILE" AGENTCTL_RUNNER_IMAGE "$IMG"
#
# Bash 3.2-clean (no associative arrays, no mapfile) so Mac scripts can use it.
#
# envfile_upsert FILE KEY VALUE
#   Create FILE with `umask 077` only if it does not already exist, then set
#   KEY=VALUE: replace the existing "KEY=..." line in place, or append a new
#   line if the key is absent. Every OTHER key already in FILE is left byte
#   for byte alone. Ends with `chmod 0600 FILE`.
#
#   A `cat > "$ENV_FILE" <<EOF ... EOF` truncates the file and silently
#   drops every other key a re-run finds there; envfile_upsert never does
#   (bin/tests/test_env_upsert.sh is the regression test). Use
#   envfile_upsert for anything that has a *current* value that
#   may need to change on a re-run (e.g. a version-pinned image tag).
#
# envfile_append_once FILE KEY VALUE
#   Append KEY=VALUE only if KEY is absent; never touches an existing value.
#   Use this for a value that is minted once (a random token) and must never be
#   silently replaced by a second run.
#
# envfile_value FILE KEY
#   Print KEY's value on stdout, and nothing at all when the key or the file
#   is absent. The last assignment wins, the way a shell reading the file
#   would resolve it, and a value that itself contains '=' survives whole.
#   It is the reader half of the two writers above, and it exists so that no
#   script has to `set -a && . FILE`: these files hold every other daemon
#   secret beside the one value a caller wants, and sourcing one exports the
#   lot into the caller's environment and into every child it spawns.
#
# envfile_hook_value HOOKS_FILE OLD_FILE KEY
#   The rework's hook credentials, with the old system's env file as a
#   FALLBACK. Reads HOOKS_FILE first. Only when that file
#   has no value does it read OLD_FILE, and then it prints a DEPRECATED
#   notice on STDERR — stderr, so `$( )` never captures it into the value
#   and the unit's journal always gets it. The notice names the two PATHS
#   and never the value.
#
#   OLD_FILE is /srv/agents/state/materializer/env. Nothing writes it any
#   more; `bin/rework-cutover.sh up` copies what it holds into
#   /srv/agents/state/creche/hooks.env, after which this fallback reads
#   nothing.
#
# creche_env_get KEY [OPTION ...] and creche_env_check [OPTION ...]
#   Run `creche-env get` or `creche-env check` of <checkout>/.venv, where
#   <checkout> is the tree that holds this file (env/AGENTS.md). The source
#   of this file fixes <checkout>, so a later `cd` of the caller changes
#   nothing. OPTION goes to the command, for example `--env FILE`. No bash
#   code parses the files. Each function gives the status of the command:
#   78 for a refused file. A tree with no command also gives 78, with one
#   line on stderr.
#
# Never echo a secret: callers pass VALUE in, this file never logs it. The
# caller is responsible for printing key NAMES only, e.g.:
#   echo "keys now present: $(cut -d= -f1 "$ENV_FILE" | tr '\n' ' ')"

envfile_upsert() {
  local file="$1" key="$2" value="$3" tmp
  if [[ ! -f "$file" ]]; then
    ( umask 077 && : > "$file" )
  fi
  tmp="$(mktemp "${file}.XXXXXX")"
  if grep -q "^${key}=" "$file"; then
    # -F'=' splits on '=' only to find field 1 (the key); the replacement
    # line is built from the whole KEY/VALUE, not from joined fields, so a
    # VALUE that itself contains '=' (base64 padding, etc.) survives intact.
    # Every non-matching line is printed as $0, untouched.
    awk -v k="$key" -v v="$value" -F'=' '$1==k{$0=k"="v} {print}' "$file" > "$tmp"
  else
    cp "$file" "$tmp"
    printf '%s=%s\n' "$key" "$value" >> "$tmp"
  fi
  chmod 0600 "$tmp"
  mv "$tmp" "$file"
}

envfile_append_once() {
  local file="$1" key="$2" value="$3"
  if [[ ! -f "$file" ]]; then
    ( umask 077 && : > "$file" )
  fi
  grep -q "^${key}=" "$file" || printf '%s=%s\n' "$key" "$value" >> "$file"
  chmod 0600 "$file"
}

envfile_value() {
  local file="$1" key="$2"
  [[ -r "$file" ]] || return 0
  # `cut -f2-`, not `-f2`: a base64 value ends in '=' padding and a URL may
  # carry one in its query. The key half is anchored by the regex instead.
  grep -oE "^${key}=.*" "$file" 2>/dev/null | tail -1 | cut -d= -f2-
}

envfile_hook_value() {
  local hooks="$1" old="$2" key="$3" value
  value="$(envfile_value "$hooks" "$key")"
  if [[ -n "$value" ]]; then
    printf '%s' "$value"
    return 0
  fi

  value="$(envfile_value "$old" "$key")"
  [[ -n "$value" ]] || return 0

  # The PATHS, never the value. One line per read, so a unit that runs every
  # minute says it every minute until `up` has moved the file.
  printf 'DEPRECATED: %s came from %s. Nothing writes that file any more; run bin/rework-cutover.sh up to put it in %s.\n' \
    "$key" "$old" "$hooks" >&2
  printf '%s' "$value"
}

# The status of a fault of the env files, and of an absent command.
CRECHE_ENV_FAULT=78

# The tree that holds this file, as an absolute path. Empty when the source
# cannot find it: each call then gives the fault status.
_CRECHE_ENV_CHECKOUT="$(CDPATH="" cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)" \
  || _CRECHE_ENV_CHECKOUT=""

_creche_env_program() {
  local program="$_CRECHE_ENV_CHECKOUT/.venv/bin/creche-env"
  if [[ -z "$_CRECHE_ENV_CHECKOUT" || ! -x "$program" ]]; then
    printf 'creche-env: %s is no program\n' "$program" >&2
    return "$CRECHE_ENV_FAULT"
  fi

  printf '%s' "$program"
}

creche_env_get() {
  local program
  program="$(_creche_env_program)" || return "$CRECHE_ENV_FAULT"
  "$program" get "$@"
}

creche_env_check() {
  local program
  program="$(_creche_env_program)" || return "$CRECHE_ENV_FAULT"
  "$program" check "$@"
}
