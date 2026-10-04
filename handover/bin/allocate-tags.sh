#!/usr/bin/env bash
# CI — run by .github/workflows/release.yml's `release` job on every merge to
# `main`, once the merged tree passed its suite. Never run on a host, and
# never as root: it writes tags and Releases in GitHub, nothing on the host.
#   handover/bin/allocate-tags.sh [--dry-run]
#
# TWO REPOSITORIES RUN IT. agent-control's workflow runs it from
# its own checkout. agent-mcp's workflow checks agent-control out beside its
# own tree and borrows this script, because contract 06 §1 gives `mcp-servers`
# to agent-mcp and the allocator that knows that lives here. Two environment
# variables carry the difference, and both default to the agent-control case:
#   TAG_REPO_ROOT  the checkout to READ and TAG. Default: this script's own
#                  repository. Every `git` call below reads it.
#   TAG_REPO       the catalog's name for the repository this run tags, e.g.
#                  agent-control. Default: the name part of GITHUB_REPOSITORY,
#                  else of the tagged tree's origin.
#   RELEASE_CLI    the resolver command. Default: `uv run` against the
#                  `handover` project beside this script, wherever it sits.
#
# THE DRY RUN NEEDS NO WRITE SCOPE, and it says what the next merge would
# tag. It prints every component's range. From a developer's own checkout:
#   GITHUB_REPOSITORY=<owner>/agent-control GITHUB_SHA=$(git rev-parse HEAD) \
#     handover/bin/allocate-tags.sh --dry-run
# Both variables are optional. Without GITHUB_SHA the checkout's HEAD stands.
# Without GITHUB_REPOSITORY the tagged tree's `origin` remote names the
# repository instead, and there is no pull request to read, so every level
# stays `patch` and the Release body would be the commit subject. Every `gh`
# call here is a read, and each one already tolerates its own failure.
#
# WHICH COMPONENTS A RUN MAY TAG. Contract 06 §1 gives every component one
# repository, and `--repo` is what scopes the plan to it (`allocate.plan_tags`
# keeps `row.repo is repo` on every decision). Without it the CLI default
# `agent-control` would stand wherever the script ran: a run inside an
# agent-mcp checkout would plan agent-control's seven components and push
# `chaperone-v0.1.0` into agent-mcp. The repository name comes from the
# environment, and the planner refuses a name the catalog does not hold.
#
# An argument that is not `--dry-run` STOPS the run. A typo for it would
# otherwise be a real allocation by somebody who meant to look.
#
# Contract 06 §2.1, and stage7-releases.md pain 1: two sessions that merge
# the same root version leave main's tip untagged. Nobody chooses a number
# here. The pull request declares a bump
# LEVEL with a `bump:minor` or `bump:major` label, this script reads the
# newest tag at merge time, and the workflow's one `concurrency` group
# serializes every run on the branch. Two allocations therefore never
# overlap.
#
# A LEVEL IS PER COMPONENT. A range can hold several merged pull requests,
# and a merge queue lands several in one push. A component's level is the
# highest label among the pull requests in ITS range that changed ITS paths:
#
#   main  ──A──────B──────C       A: bump:minor, changed attendance/
#                                 B: no label,   changed chaperone/
#   attendance-v0.1.7 → 0.2.0     C: no label,   changed attendance/
#   chaperone-v0.1.12       → 0.1.13    A's label is not chaperone's: A changed no chaperone path
#
# A TAG COVERS EVERY CHANGE SINCE THE COMPONENT'S OWN LAST TAG.
# A run can be missed: a red `main` tags nothing, and a pending run gives
# way to a newer one. Merge A changes `noticeboard/` and its run is missed, merge B
# changes only `docs/` and its run passes. Reading one commit's paths would
# tag nothing for `noticeboard`, and A's change would never be tagged at all. So each
# component gets its own `git diff <its newest tag> <SHA>`, and each line
# handed to the planner says which component's range it came from.
#
# A component with NO tag at all gets its first one on whatever run comes
# next, whatever paths that commit changed (`allocate.plan_tags`).
#
# The DECISION is `handover allocate-tags`, which is pure and tested
# (handover/tests/test_handover_allocate.py). This script gathers the inputs
# and performs the plan, and holds no arithmetic of its own: which tag is
# newest and what number comes after it are both questions it ASKS.
#
# `git tag` refusing an existing ref is contract 06 §2.1's third row, "the
# tag exists on another SHA". It is fatal here and it is the only place that
# row can be detected: the planner's target is always above every tag it was
# shown.
#
# It never moves a tag. It never deletes one. A second run on one commit
# creates nothing (`noop`), which is what makes a re-run safe.
set -euo pipefail

usage() { printf 'usage: allocate-tags.sh [--dry-run]\n' >&2; }
say() { printf '%s\n' "$*"; }
die() { printf '::error::%s\n' "$*" >&2; exit 1; }

# Two roots, and in agent-control they are one directory. SELF_ROOT is where
# this script and the `handover` project live, and it is used for nothing else.
# REPO_ROOT is the checkout this run reads and tags, which agent-mcp's
# workflow points at its own tree. Splitting them is what lets one allocator
# serve both repositories without a second copy of it.
SELF_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
REPO_ROOT=$(cd -- "${TAG_REPO_ROOT:-$SELF_ROOT}" 2>/dev/null && pwd) \
  || die "TAG_REPO_ROOT is not a directory: ${TAG_REPO_ROOT:-}"

DRY_RUN=no
case "${1:-}" in
  "") ;;
  --dry-run) DRY_RUN=yes ;;
  *) usage; exit 2 ;;
esac

# One argument at most. `--dry-run --dry-run` is a caller who believes
# something else is being passed.
if [[ $# -gt 1 ]]; then
  usage
  exit 2
fi

# The resolver's own command. `uv run` builds the package's environment on a
# fresh runner. A caller that already has the console script sets RELEASE_CLI
# to its absolute path.
if [[ -n "${RELEASE_CLI:-}" ]]; then
  # shellcheck disable=SC2206
  CLI=(${RELEASE_CLI})
else
  CLI=(uv run --project "$SELF_ROOT/handover" handover)
fi

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/ranges"

# The repository whose components this run may tag, as contract 06 §1 names
# it: the name part of `<owner>/<name>`. CI always sets GITHUB_REPOSITORY. A
# developer's checkout usually does not, so the TAGGED TREE's own origin
# answers — its own, never this script's, so a borrowed run cannot read
# agent-control's remote and plan agent-control's components. Neither source
# is trusted: probe 1 below hands the name to the planner, which holds the
# catalog and refuses anything else.
#
# TAG_REPO says it outright, and wins. A repository may be hosted under a
# name the catalog does not hold, and then neither source above can name
# it: its workflow sets TAG_REPO to the catalog's name. GitHub is still
# asked by GITHUB_REPOSITORY, the hosted name.
REPO=${GITHUB_REPOSITORY:-$(git -C "$REPO_ROOT" remote get-url origin 2>/dev/null || true)}
REPO=${REPO##*/}
REPO=${REPO%.git}
REPO=${TAG_REPO:-$REPO}
[[ -n "$REPO" ]] \
  || die "no TAG_REPO, no GITHUB_REPOSITORY and no origin remote: nothing says which repository this run tags"

# `gh release create` reads the repository from the current directory's
# remote. A borrowed run stands in a directory that is not the one being
# tagged, so the Release names its repository outright.
GH_REPO_ARGS=()
if [[ -n "${GITHUB_REPOSITORY:-}" ]]; then
  GH_REPO_ARGS=(--repo "$GITHUB_REPOSITORY")
fi

SHA=${GITHUB_SHA:-$(git -C "$REPO_ROOT" rev-parse HEAD)}
git -C "$REPO_ROOT" rev-parse -q --verify "$SHA^{commit}" >/dev/null \
  || die "$SHA is not a commit in this checkout"

git -C "$REPO_ROOT" tag --list > "$WORK/tags"
git -C "$REPO_ROOT" tag --points-at "$SHA" > "$WORK/at-sha"

# The merged pull request, for the Release body. A direct push has none, and
# then the commit subject is the body.
#
# One read of that pull request, into one file. `gh api` prints the RESPONSE
# BODY on stdout and exits non-zero on an HTTP error, and swallowing only the
# exit code left the body behind as the answer: a token that cannot read pull
# requests handed a 401 body to the Release notes. A failed read leaves
# nothing, and nothing reads as no pull request.
pr_read() {
  local query=$1 out=$2
  gh api "repos/$GITHUB_REPOSITORY/commits/$SHA/pulls" --jq "$query" > "$out" 2>/dev/null \
    || : > "$out"
}

PR_JSON=$WORK/pr.json
: > "$PR_JSON"
if [[ -n "${GITHUB_REPOSITORY:-}" ]]; then
  pr_read '[.[] | select(.merged_at != null)][0] // empty' "$PR_JSON"
fi

# Every merged pull request that asked for a level, as `<merge SHA> <label>`
# lines. Two reads for the whole run, however long a range is: a pull
# request with no bump label asks for `patch`, which is the default, so only
# the labelled ones are worth knowing. A failed read leaves nothing, which
# reads as nobody asking.
BUMP_LABELS=(bump:minor bump:major)
: > "$WORK/bumps"
if [[ -n "${GITHUB_REPOSITORY:-}" ]]; then
  for label in "${BUMP_LABELS[@]}"; do
    gh pr list --repo "$GITHUB_REPOSITORY" --state merged --label "$label" --limit 200 \
      --json mergeCommit --jq '.[].mergeCommit.oid' > "$WORK/merged" 2>/dev/null \
      || : > "$WORK/merged"
    while IFS= read -r merged; do
      [[ "$merged" =~ ^[0-9a-f]{40}$ ]] && printf '%s %s\n' "$merged" "$label" >> "$WORK/bumps"
    done < "$WORK/merged"
  done
fi

BODY=""
if [[ -s "$PR_JSON" ]]; then
  pr_read '[.[] | select(.merged_at != null)][0] | "\(.title) (#\(.number))"' "$WORK/body"
  BODY=$(cat "$WORK/body")
fi
[[ -n "$BODY" ]] || BODY=$(git -C "$REPO_ROOT" log -1 --format=%s "$SHA")

say "repo:    $REPO"
say "commit:  $SHA"
say "labels:  $(tr '\n' ' ' < "$WORK/bumps" | sed 's/ $//' | grep . || printf none)"

# ---- two probes: the component list, and each component's newest tag -----
#
# The script must answer neither question itself. "Which of these tags is
# the newest" is version arithmetic and it lives in allocate.py, and the
# component list and its `path` column live in catalog.py. So it asks
# twice, with plans it then throws away. Both are reads: they create
# nothing and need no write scope.
#
# Probe 1, the component list. An empty tag list makes every component
# untagged, and `plan_tags` reports every untagged component of the repo
# whatever the paths say. This is how the script learns the names without
# holding a second copy of the catalog.
#
# It is also where an unknown repository stops the run. `--repo`'s choices
# are the catalog's own `Repo`, so the planner is the one place that says
# which names exist, and this script never holds a second copy of the list.
: > "$WORK/empty"
if ! "${CLI[@]}" allocate-tags --repo "$REPO" \
  --paths "$WORK/empty" --tags "$WORK/empty" > "$WORK/names" 2> "$WORK/refusal"; then
  cat "$WORK/refusal" >&2
  die "the planner refused repository '$REPO'; its own message is above"
fi

# Probe 2, each component's current version. A paths file naming every
# directory in the tree, at every depth, reaches every component: contract
# 06 §1's `path` column is a directory of the tree or the whole repo.
# `--at-sha` is left out ON PURPOSE: a re-run must still learn each
# component's version, and a `noop` row carries the tag rather than the
# range's base.
{ git -C "$REPO_ROOT" ls-tree -r -d --name-only "$SHA"; printf '.\n'; } > "$WORK/tree"
"${CLI[@]}" allocate-tags --repo "$REPO" --paths "$WORK/tree" --tags "$WORK/tags" > "$WORK/probe" \
  || die "the planner refused the probe"

# ---- one range per component ----------------------------------------------
#
# Each changed path is cut down before it is handed in. One range then costs
# a handful of lines instead of one per changed file, and a range that spans
# a year stays inside the planner's input caps.
#
# The planner makes the cut, because the cut depends on the catalog and this
# script holds no copy of it (`allocate.cut_path`). A path is cut to its
# first segment, which reaches every component whose directories are
# top-level entries. A build can also install a directory deeper in the
# tree, such as a crate under `rust/crates/`, and a binary component moves
# with `rust/Cargo.lock`. A path that such a nested path holds is cut to
# that path, so its line still reaches its component.
#
# cut_paths FILE: the cut of every changed path in FILE, each line once.
# Its stdin is closed: the loops below read their own input on stdin, and a
# child that read it would take their next lines.
cut_paths() {
  "${CLI[@]}" allocate-tags --repo "$REPO" --cut "$1" < /dev/null \
    || die "the planner refused the changed paths"
}

: > "$WORK/paths"
: > "$WORK/levels"
: > "$WORK/seen"
REFUSED=0
while read -r outcome tag from _detail; do
  case "$outcome" in create|noop) ;; *) continue ;; esac
  [[ "$tag" == *-v* ]] || continue
  component=${tag%-v*}
  printf '%s\n' "$component" >> "$WORK/seen"

  # No tag, no range. `plan_tags`'s first-tag rule tags it whatever changed.
  if [[ "$from" == "-" ]]; then
    say "range:   $component: first tag, no previous tag to measure from"
    printf '%s\n' "Range: none. There is no previous tag to measure from." \
      > "$WORK/ranges/$component"
    continue
  fi

  # A tag that is not an ancestor of this commit is not this commit's
  # history: a force-push left it behind, or somebody made it by hand on a
  # side branch. Diffing against it would describe a range that never
  # happened, so this component refuses and the others are still tagged.
  previous="$component-v$from"
  if ! git -C "$REPO_ROOT" merge-base --is-ancestor "$previous" "$SHA" 2>/dev/null; then
    printf '::error::%s\n' \
      "$previous is not an ancestor of $SHA: $component refused, its range is not history" >&2
    REFUSED=$((REFUSED + 1))
    continue
  fi

  count=$(git -C "$REPO_ROOT" rev-list --count "$previous..$SHA")
  say "range:   $component: $count commit(s) since $previous"

  git -C "$REPO_ROOT" diff --name-only "$previous" "$SHA" > "$WORK/changed"
  cut_paths "$WORK/changed" > "$WORK/segments"
  while IFS= read -r segment; do
    [[ -n "$segment" ]] || continue
    printf '%s\t%s\n' "$component" "$segment" >> "$WORK/paths"
  done < "$WORK/segments"

  # The levels this range asks for. A labelled pull request counts when its
  # merge commit is on this range's first-parent line, and the planner then
  # keeps the label only if that merge changed one of the component's own
  # paths. So each line carries the cut paths THAT merge changed.
  git -C "$REPO_ROOT" rev-list --first-parent "$previous..$SHA" > "$WORK/merges"
  while read -r merged label; do
    grep -qxF "$merged" "$WORK/merges" || continue
    git -C "$REPO_ROOT" diff --name-only "$merged^1" "$merged" \
      > "$WORK/merged-changed" 2>/dev/null || : > "$WORK/merged-changed"
    cut_paths "$WORK/merged-changed" > "$WORK/merged-segments"
    while IFS= read -r segment; do
      [[ -n "$segment" ]] || continue
      printf '%s\t%s\t%s\n' "$component" "$label" "$segment" >> "$WORK/levels"
    done < "$WORK/merged-segments"
  done < "$WORK/bumps"

  # The pull requests the range holds, read from the commit subjects rather
  # than from the API, so the Release body names them.
  git -C "$REPO_ROOT" log --format=%s "$previous..$SHA" > "$WORK/subjects"
  pulls=$(grep -oE '#[0-9]+' "$WORK/subjects" | sort -u | tr '\n' ' ' || true)
  {
    printf 'Range: %s commit(s) since %s.\n' "$count" "$previous"
    [[ -n "$pulls" ]] && printf 'Pull requests named in that range: %s\n' "${pulls% }"
    :
  } > "$WORK/ranges/$component"
done < "$WORK/probe"

# A component probe 2 never reached has no directory in this tree, so this
# run cannot read a range for it. It is said out loud rather than left out:
# a component that is quietly skipped is a tag nobody knows is missing.
while read -r outcome tag _from _detail; do
  [[ "$outcome" == create && "$tag" == *-v* ]] || continue
  missing=${tag%-v*}
  grep -qxF "$missing" "$WORK/seen" \
    || say "range:   $missing: no path in this tree, so no range was read"
done < "$WORK/names"

"${CLI[@]}" allocate-tags \
  --repo "$REPO" \
  --paths "$WORK/paths" \
  --tags "$WORK/tags" \
  --at-sha "$WORK/at-sha" \
  --levels "$WORK/levels" > "$WORK/plan" || die "the planner refused the inputs"

cat "$WORK/plan"

# The Release body: what merged, what this tag is, and what it covers.
release_notes() {
  local component=$1 tag=$2 detail=$3
  printf '%s\n\n' "$BODY"
  printf '%s: %s.\n' "$tag" "$detail"
  if [[ -f "$WORK/ranges/$component" ]]; then
    cat "$WORK/ranges/$component"
  fi

  return 0
}

MADE=0
while read -r outcome tag _from detail; do
  case "$outcome" in
    noop) say "keeping $tag: already on this commit" ;;
    create)
      if [[ "$DRY_RUN" == yes ]]; then
        say "would create $tag on $SHA and publish its Release"
        continue
      fi

      NOTES=$(release_notes "${tag%-v*}" "$tag" "$detail")

      # A tag that already exists stops the run. Never -f, never a delete.
      git -C "$REPO_ROOT" tag "$tag" "$SHA" \
        || die "conflict: $tag exists on another commit; a tag is never moved"
      git -C "$REPO_ROOT" push origin "refs/tags/$tag" \
        || die "conflict: $tag exists on the remote; a tag is never moved"
      gh release create "$tag" "${GH_REPO_ARGS[@]+"${GH_REPO_ARGS[@]}"}" \
        --target "$SHA" --title "$tag" --notes "$NOTES" \
        || die "$tag is pushed but its Release did not publish"
      say "released $tag on $SHA: $BODY"
      MADE=$((MADE + 1))
      ;;
    *) continue ;;
  esac
done < "$WORK/plan"

say "allocate-tags: $MADE created"

# Reported last so the tags that WERE right are already made and pushed. A
# run that ends here needs a human: a tag in this repository is not in this
# commit's history, and every run will say so until somebody fixes it.
[[ "$REFUSED" -eq 0 ]] \
  || die "$REFUSED component(s) refused: a newest tag that is not an ancestor of $SHA"
