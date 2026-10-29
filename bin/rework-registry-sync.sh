#!/usr/bin/env bash
# The old name of creche-registry-sync.sh. It stays for one deploy, so that a unit file that still names it keeps working.
exec "$(dirname -- "${BASH_SOURCE[0]}")/creche-registry-sync.sh" "$@"
