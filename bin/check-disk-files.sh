#!/usr/bin/env bash
# CI: refuses a file of a disk-image row past its limit, before the `sign`
# job signs it (decision CH78 rule 3).
#   DISK_REPO=creche DISK_ROW=disks DISK_OUT=<dir> bin/check-disk-files.sh
# The job `disks` of release.yml runs it after the build of the row, when
# the plan tags `disks`. No hook and no unit runs it. The host never runs it.
#
# The row comes from `handover disk-assets --repo "$DISK_REPO"`.
# bin/lib/artifactrows.sh is the one reader of that line form. The row
# names the out variable, and each signed file with its bytes at most
# (`artifacts.asset_limits`). This script holds no copy of a file of the
# row or of a limit.
#
# Variables:
#   DISK_REPO       the catalog name of the repository.
#   DISK_ROW        the disk-image component that the build made.
#   DISK_OUT        for `disks`: the out variable that the row names, the
#                   directory that the build wrote. It holds each file of
#                   the row, SHA256SUMS and nothing else.
#   RELEASE_CLI     the `handover` command. Default: `uv run` against the
#                   `handover` project beside this script.
#
# The checks, in order:
#   1. Each file of the row is a regular file in the directory, and no link.
#   2. Each file has at most the bytes of its limit. Root refuses a larger
#      file, so CI must not sign it.
#   3. The directory holds no other entry than the files and SHA256SUMS.
#      So the `sign` job signs only files that this script checked.
# The bundles come from the `sign` job, after this script. The tag script
# checks the limit of each bundle (handover/bin/allocate-tags.sh).
#
# Exit 0 when each check passes. Exit 1 with one line for the first fault.
#
# Bash 3.2-clean: its tests run it on a development machine.
set -euo pipefail
export LC_ALL=C

SELF_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)

# shellcheck source=lib/artifactrows.sh
. "$SELF_ROOT/bin/lib/artifactrows.sh"

#: The file of the digests that the build writes beside the files.
DIGEST_FILE="SHA256SUMS"

die() {
  printf 'check-disk-files: %s\n' "$*" >&2
  exit 1
}

[[ -n "${DISK_REPO:-}" ]] || die "the variable DISK_REPO is not set"
[[ -n "${DISK_ROW:-}" ]] || die "the variable DISK_ROW is not set"

# The resolver's own command, as bin/build-artifacts.sh starts it.
if [[ -n "${RELEASE_CLI:-}" ]]; then
  # shellcheck disable=SC2206
  CLI=(${RELEASE_CLI})
else
  CLI=(uv run --project "$SELF_ROOT/handover" handover)
fi

ROWS="$(mktemp "${TMPDIR:-/tmp}/check-disk-files.XXXXXX")" || die "cannot make a file for the rows"
trap 'rm -f -- "$ROWS"' EXIT

"${CLI[@]}" disk-assets --repo "$DISK_REPO" < /dev/null > "$ROWS" || die "handover gave no disk rows"
read_disk_rows "$ROWS" || die "$ROWS_FAULT"

ROW=""
for ((at = 0; at < ${#DISK_COMPONENT[@]}; at++)); do
  if [[ "${DISK_COMPONENT[$at]}" == "$DISK_ROW" ]]; then
    ROW="$at"
  fi
done
[[ -n "$ROW" ]] || die "$DISK_ROW has no disk-image row"

OUT_ENV="${DISK_OUT_ENV[$ROW]}"
OUT="${!OUT_ENV:-}"
[[ -n "$OUT" ]] || die "the variable $OUT_ENV is not set"
[[ -d "$OUT" && ! -L "$OUT" ]] || die "$OUT_ENV names no directory"

# 1 and 2: each file of the row, and its limit.
names=" $DIGEST_FILE "
for ((at = 0; at < ${#DISK_FILE[@]}; at++)); do
  if [[ "${DISK_FILE_ROW[$at]}" != "$ROW" ]]; then
    continue
  fi

  name="${DISK_FILE[$at]}"
  limit="${DISK_FILE_MAX[$at]}"
  path="$OUT/$name"
  [[ -f "$path" && ! -L "$path" ]] || die "$DISK_ROW: $name is no regular file in $OUT_ENV"
  size=$(($(wc -c < "$path")))
  if [[ "$size" -gt "$limit" ]]; then
    die "$DISK_ROW: $name has $size bytes, more than its limit of $limit"
  fi
  printf 'check-disk-files: %s: %s bytes, at most %s\n' "$name" "$size" "$limit"
  names+="$name "
done

# 3: no other entry. `ls -A` lists each name that starts with a period too.
while IFS= read -r entry; do
  if [[ -n "$entry" && "$names" != *" $entry "* ]]; then
    die "$DISK_ROW: $OUT_ENV holds $entry, which is no file of the row"
  fi
done <<< "$(ls -A -- "$OUT")"
