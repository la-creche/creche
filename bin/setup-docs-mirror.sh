#!/usr/bin/env bash
# OPERATOR — on the host, as the operator, no sudo, never as root:
#   /opt/creche/bin/setup-docs-mirror.sh
# Installs the docs-mirror timer and the timer of code-corpus-sync, which the
# mirror reads from. Idempotent: run it again after any unit-source change.
# The mirror depends on a fresh corpus, so one run installs both units. The
# unit files are not in this repository: creche-release ships them, and the
# ExecStart of docs-mirror.service must name /opt/creche/bin/docs-mirror.py.
#
# Run AFTER `sudo creche-deploy`: ExecStart points into /opt/creche, so units
# installed before a deploy run yesterday's code.
set -euo pipefail

DEPLOYED=/opt/creche
UNIT_DIR="$HOME/.config/systemd/user"
REPO_LIST=/srv/agents/state/code-repos.txt
UNITS=(code-corpus-sync docs-mirror)

# The repos the mirror renders (SECTIONS in bin/docs-mirror.py). A missing one
# is a warning, not a failure: the mirror says so on every run too.
WANTED_REPOS=(creche mcp registry chaperone)

say() { printf '[setup-docs-mirror] %s\n' "$*"; }

[[ -d "$DEPLOYED/systemd" ]] || {
  say "no $DEPLOYED — run 'sudo creche-deploy' first"; exit 1;
}

mkdir -p "$UNIT_DIR"
for unit in "${UNITS[@]}"; do
  install -m 0644 "$DEPLOYED/systemd/$unit.service" "$DEPLOYED/systemd/$unit.timer" "$UNIT_DIR/"
  say "installed $unit.{service,timer}"
done

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
