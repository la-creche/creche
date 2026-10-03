#!/usr/bin/env bash
# Refresh the code corpus the oracle indexes (OPERATOR, no sudo). These are
# DEDICATED clones, never the operator's working checkouts: a working tree carries
# uncommitted state and build output, and the corpus must be reproducible.
#
# Repo list lives OUTSIDE the corpus (a .txt inside it would get indexed):
#   /srv/agents/state/code-repos.txt   one repository per line, # comments ok
#
#   <git URL>            clones to <corpus>/<the URL's last part>
#   <git URL> <name>     clones to <corpus>/<name>
#
# The second form is for a repository its host calls something else than
# the name this code knows it by: the release executor reads
# /srv/agents/code/<the catalog's name>, whatever the URL says.
#
# CODE_CORPUS_ROOT and CODE_REPO_LIST name another root and another list.
# The tests set them, and nothing on a host does.
set -euo pipefail
CORPUS_ROOT="${CODE_CORPUS_ROOT:-/srv/agents/code}"
REPO_LIST="${CODE_REPO_LIST:-/srv/agents/state/code-repos.txt}"

# A directory name: letters, digits, `.`, `_` and `-`, starting with a
# letter or a digit. It is joined onto the corpus root, so `..`, a slash
# and a leading `-` or `.` are refused.
NAME_RE='^[A-Za-z0-9][A-Za-z0-9._-]*$'

[[ -f "$REPO_LIST" ]] || {
  echo "no $REPO_LIST — create it with one git URL per line" >&2
  exit 2
}
mkdir -p "$CORPUS_ROOT"

fails=0
# `|| [[ -n "$line" ]]`: `read` answers non-zero on a last line that has no
# newline, and that line still names a repository.
while IFS= read -r line || [[ -n "$line" ]]; do
  line="${line%%#*}"
  read -r url name extra <<< "$line" || true
  [[ -z "$url" ]] && continue

  if [[ -n "$extra" ]]; then
    echo "FAILED $url: a line holds a URL and at most one name" >&2
    fails=$((fails + 1))
    continue
  fi

  [[ -n "$name" ]] || name="$(basename "${url%.git}")"
  if [[ ! "$name" =~ $NAME_RE ]]; then
    echo "FAILED $url: not a directory name: $name" >&2
    fails=$((fails + 1))
    continue
  fi

  dest="$CORPUS_ROOT/$name"
  if [[ -d "$dest/.git" ]]; then
    # ff-only: the corpus never carries local commits, so a diverged clone is
    # a problem to look at, not to merge away.
    if git -C "$dest" pull --ff-only --quiet; then
      echo "updated $name -> $(git -C "$dest" rev-parse --short HEAD)"
    else
      echo "FAILED pull $name" >&2; fails=$((fails + 1))
    fi
  elif git clone --quiet "$url" "$dest"; then
    echo "cloned $name -> $(git -C "$dest" rev-parse --short HEAD)"
  else
    echo "FAILED clone $url" >&2; fails=$((fails + 1))
  fi
done < "$REPO_LIST"

echo "corpus at $CORPUS_ROOT; provision new repos with:  ./bin/provision-library.sh <name> code"
exit "$fails"
