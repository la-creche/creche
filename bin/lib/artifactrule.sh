# Sourced only (bin/AGENTS.md, Library): the one copy of "does this change
# touch an input of an archive?". .github/actions/scope sources it, so the
# job `artifacts` of gate.yml builds the archives of the binary components
# only for a change that can change them. release.yml does not ask: its job
# `artifacts` builds at each push, because each run can tag a component that
# an older run missed.
#
# Source bin/lib/rustrule.sh first. This rule calls `rust_touched`.
#
# An input of an archive is one of these:
#
#   a change that rust_touched counts   rust/, vectors/ and the files of the
#                                       Rust checks
#   the manifest of a component         component.yaml or component.toml, at
#                                       the root or one directory deep. It
#                                       holds the build commands of a
#                                       binary component
#   a path under handover/              the release tool gives the rows, the
#                                       limits and the walk of a tree
#   bin/build-artifacts.sh              the build itself
#   this file                           the rule itself
#
# What this cannot read counts as a change: the build is the safe answer.
#
# Bash 3.2-clean: a test runs it on a development machine.

#: The directory of the release tool, and the two names of a manifest.
ARTIFACT_TOOL_DIR="handover"
ARTIFACT_MANIFESTS="component.yaml component.toml"

#: The script that builds, and this rule.
ARTIFACT_FILES="bin/build-artifacts.sh bin/lib/artifactrule.sh"

# artifact_path PATH: whether PATH is an input of an archive that
# rust_touched does not count. git writes a path with an unusual byte inside
# double quotes, and the quotes do not change the answer.
artifact_path() {
  local path="$1" one name dir

  if [[ "$path" == \"*\" ]]; then
    path="${path#\"}"
    path="${path%\"}"
  fi

  case "$path" in
    "$ARTIFACT_TOOL_DIR"/*)
      return 0
      ;;
  esac

  for one in $ARTIFACT_FILES; do
    if [[ "$path" == "$one" ]]; then
      return 0
    fi
  done

  name="${path##*/}"
  dir="${path%/*}"
  for one in $ARTIFACT_MANIFESTS; do
    # At the root, `dir` is the path itself. One directory deep, `dir` holds
    # no slash.
    if [[ "$name" == "$one" ]] && [[ "$dir" == "$path" || "$dir" != */* ]]; then
      return 0
    fi
  done

  return 1
}

# artifacts_touched FROM TO: whether the change FROM..TO touches an input of
# an archive.
artifacts_touched() {
  local paths path

  if rust_touched "$1" "$2"; then
    return 0
  fi

  if ! paths="$(git -c core.quotePath=false diff --name-only --no-renames \
    "$1" "$2" 2>/dev/null)"; then
    return 0
  fi

  while IFS= read -r path; do
    if artifact_path "$path"; then
      return 0
    fi
  done <<< "$paths"

  return 1
}
