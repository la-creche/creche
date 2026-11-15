#!/usr/bin/env bash
# OPERATOR — on the host, as the operator, no sudo, never as root:
#   /opt/creche/bin/setup-docs-mirror.sh
# Enables the timers of creche-docs-mirror and creche-code-corpus-sync, which
# the mirror reads from. It installs no unit file: a `host` release of
# creche-release links both units, and ExecStart of creche-docs-mirror.service
# names /opt/creche/bin/docs-mirror.py. Idempotent.
#
# Run AFTER `sudo creche-deploy` and after the `host` release is installed:
# ExecStart points into /opt/creche, so a unit started before a deploy runs
# yesterday's code.
set -euo pipefail

DEPLOYED=/opt/creche
REPO_LIST=/srv/agents/state/code-repos.txt
UNITS=(creche-code-corpus-sync creche-docs-mirror)

# The repos the mirror renders (SECTIONS in bin/docs-mirror.py). A missing one
# is a warning, not a failure: the mirror says so on every run too.
WANTED_REPOS=(creche mcp registry chaperone)

say() { printf '[setup-docs-mirror] %s\n' "$*"; }

systemctl --user daemon-reload
for unit in "${UNITS[@]}"; do
  systemctl --user enable --now "$unit.timer"
  say "enabled $unit.timer"
done

if [[ -f "$REPO_LIST" ]]; then
  for repo in "${WANTED_REPOS[@]}"; do
    grep -q "/$repo" "$REPO_LIST" \
      || say "! $REPO_LIST has no $repo — append its git URL (matching the style of the existing lines)"
  done
else
  say "! no $REPO_LIST — create it with one git URL per line; the corpus sync refuses to run without it"
fi

say "first mirror lands within the hour; run it now with: $DEPLOYED/bin/docs-mirror.py"
