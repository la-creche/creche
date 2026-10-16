#!/usr/bin/env bash
# CI: builds, walks, packs and checks the archive of each binary component.
#   ARTIFACT_REPO=agent-control ARTIFACT_OUT=<dir> bin/build-artifacts.sh
# The job `artifacts` of gate.yml and of release.yml runs it on a GitHub
# runner, and bin/artifact-probe.sh runs it with the row of one crate. No
# hook and no unit runs it. The host never runs it: root builds nothing.
#
# The rows come from `handover artifacts --repo "$ARTIFACT_REPO"`: one row
# for each binary component of the catalog, with the build commands of its
# manifest. The line form is `ArtifactRow.lines` of
# handover/src/handover/artifacts.py. A row holds the component, the target,
# the names of the two assets, the name of the install variable, three
# limits and each command, one word on each line. This script holds no copy
# of a target, a name or a limit.
#
# Variables:
#   ARTIFACT_REPO       the catalog name of the repository.
#   ARTIFACT_ROWS_FILE  a file of rows in the line form, for example the
#                       output of `handover artifacts --crate-row`. Give
#                       this variable or ARTIFACT_REPO, not both.
#   ARTIFACT_OUT        the output directory. It is absent or empty.
#   RELEASE_CLI         the `handover` command. Default: `uv run` against
#                       the `handover` project beside this script.
#   GITHUB_WORKSPACE, RUNNER_TEMP, HOME, GITHUB_OUTPUT: a runner sets them.
#   CARGO_HOME          the cargo home. Default: $HOME/.cargo.
#
# With no row the script starts no rustup and no cargo. It writes an empty
# SHA256SUMS and an empty step output `digests`.
#
# With a row or more it installs the toolchain of rust/rust-toolchain.toml
# and adds the target of each row. Then, for each row:
#   1. It runs each command of the row in the repository root, as a list of
#      words. The install variable of the row names an empty tree. The
#      remap flags replace the three paths of the runner in each program.
#   2. The tree holds one program or more in bin/, and nothing else.
#   3. `handover check-tree` walks the tree. No file holds the workspace,
#      the home, the cargo home or the temporary directory of the runner.
#   4. It packs the tree: each member is a regular file
#      `<component>/bin/<program>`. The pack gives the same bytes for the
#      same tree.
#   5. It refuses an archive that passes a limit of its row. Root refuses
#      such an archive, so CI must not sign it.
# At the end it writes SHA256SUMS in the form of `sha256sum`, one line for
# each archive. The step output `digests` holds the same text.
#
# `set -euo pipefail`: the first fault stops the run with one line, and the
# run then writes no SHA256SUMS.
#
# Bash 3.2-clean: its tests run it on a development machine.
set -euo pipefail
export LC_ALL=C

SELF_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)

# shellcheck source=lib/rustrule.sh
. "$SELF_ROOT/bin/lib/rustrule.sh"

#: The file of the digests, in the output directory.
DIGEST_FILE="SHA256SUMS"

#: The name of the step output, and the end line of its value.
DIGEST_OUTPUT="digests"
OUTPUT_END="END_OF_DIGESTS"

#: What the remap flags put in the place of the three paths of the runner.
REMAP_WORKSPACE="/build/workspace"
REMAP_CARGO_HOME="/build/cargo-home"
REMAP_TEMP="/build/temp"

#: The keys of a row before its first command, in their order.
HEAD_KEYS="component target archive bundle install-env max-archive-bytes max-members max-unpacked-bytes"

#: The grammar of a name that goes into a path or a command line: it starts
#: with a letter or a digit, so no program reads it as an option.
PLAIN_NAME='^[A-Za-z0-9][A-Za-z0-9._-]*$'

#: The grammar of the name of a variable, of a limit and of a word count.
VARIABLE_NAME='^[A-Za-z_][A-Za-z0-9_]*$'
LIMIT='^[1-9][0-9]{0,17}$'
WORD_COUNT='^[1-9][0-9]{0,3}$'

say() { printf 'build-artifacts: %s\n' "$*"; }
die() {
  printf 'build-artifacts: %s\n' "$*" >&2
  exit 1
}

# need NAME...: stops the run when a variable is not set or is empty.
need() {
  local name

  for name in "$@"; do
    if [[ -z "${!name:-}" ]]; then
      die "the variable $name is not set"
    fi
  done
}

# ------------------------------------------------------------------ rows ----

#: One entry for each row.
ROW_COMPONENT=()
ROW_TARGET=()
ROW_ARCHIVE=()
ROW_ENV=()
ROW_MAX_ARCHIVE=()
ROW_MAX_MEMBERS=()
ROW_MAX_UNPACKED=()
ROW_FIRST=()
ROW_COMMANDS=()

#: One entry for each command: the index of its first word in WORDS, and
#: its count of words.
COMMAND_FIRST=()
COMMAND_WORDS=()
WORDS=()

#: The lines of the rows, and the index of the next line to read.
LINES=()
POS=0
LINE=""

# next_line: sets LINE to the next line. The rows must not end there.
next_line() {
  [[ "$POS" -lt "${#LINES[@]}" ]] || die "the rows end inside a row"
  LINE="${LINES[$POS]}"
  POS=$((POS + 1))
}

# value_of KEY: the value of the next line, which must have the key KEY.
VALUE=""
value_of() {
  next_line
  [[ "$LINE" == "$1 "* ]] || die "a row has no key $1 where the line form puts it"
  VALUE="${LINE#"$1 "}"
}

# read_head: reads the keys of a row before its first command into the
# arrays of the rows.
read_head() {
  local key

  for key in $HEAD_KEYS; do
    value_of "$key"
    case "$key" in
      component | target | archive | bundle)
        [[ "$VALUE" =~ $PLAIN_NAME ]] || die "the $key of a row is not a plain name"
        ;;
      install-env)
        [[ "$VALUE" =~ $VARIABLE_NAME ]] || die "the install-env of a row is not a variable name"
        ;;
      *)
        [[ "$VALUE" =~ $LIMIT ]] || die "the $key of a row is not a count above 0"
        ;;
    esac

    case "$key" in
      component) ROW_COMPONENT+=("$VALUE") ;;
      target) ROW_TARGET+=("$VALUE") ;;
      archive) ROW_ARCHIVE+=("$VALUE") ;;
      install-env) ROW_ENV+=("$VALUE") ;;
      max-archive-bytes) ROW_MAX_ARCHIVE+=("$VALUE") ;;
      max-members) ROW_MAX_MEMBERS+=("$VALUE") ;;
      max-unpacked-bytes) ROW_MAX_UNPACKED+=("$VALUE") ;;
    esac
  done
}

# read_commands: reads the commands of a row and its `end` line. A row has
# one command or more, and a command has one word or more.
read_commands() {
  local count commands=0 index

  ROW_FIRST+=("${#COMMAND_FIRST[@]}")
  while true; do
    next_line
    if [[ "$LINE" == "end" && "$commands" -gt 0 ]]; then
      break
    fi

    [[ "$LINE" == "argv "* ]] || die "a row has no command where the line form puts one"
    count="${LINE#argv }"
    [[ "$count" =~ $WORD_COUNT ]] || die "a command of a row has no count of words"

    COMMAND_FIRST+=("${#WORDS[@]}")
    COMMAND_WORDS+=("$count")
    for ((index = 0; index < count; index++)); do
      next_line
      WORDS+=("$LINE")
    done
    [[ -n "${WORDS[${COMMAND_FIRST[${#COMMAND_FIRST[@]} - 1]}]}" ]] ||
      die "a command of a row has an empty first word"
    commands=$((commands + 1))
  done
  ROW_COMMANDS+=("$commands")
}

# read_rows FILE: reads each row of FILE. Two rows with one component, or
# with one archive name, stop the run: the second archive would replace the
# first. No archive has the name of the digest file.
read_rows() {
  local line row other

  LINES=()
  while IFS= read -r line || [[ -n "$line" ]]; do
    LINES+=("$line")
  done < "$1"

  POS=0
  while [[ "$POS" -lt "${#LINES[@]}" ]]; do
    read_head
    read_commands
  done

  for ((row = 0; row < ${#ROW_COMPONENT[@]}; row++)); do
    [[ "${ROW_ARCHIVE[$row]}" != "$DIGEST_FILE" ]] ||
      die "the archive of ${ROW_COMPONENT[$row]} has the name of the digest file"
    for ((other = 0; other < row; other++)); do
      [[ "${ROW_COMPONENT[$other]}" != "${ROW_COMPONENT[$row]}" ]] ||
        die "two rows name the component ${ROW_COMPONENT[$row]}"
      [[ "${ROW_ARCHIVE[$other]}" != "${ROW_ARCHIVE[$row]}" ]] ||
        die "two rows name the archive ${ROW_ARCHIVE[$row]}"
    done
  done
}

# ----------------------------------------------------------------- build ----

#: The name of the toolchain of rust/rust-toolchain.toml, and the remap
#: flags of each build.
TOOLCHAIN=""
REMAP_FLAGS=""

# install_toolchain: the toolchain of rust/rust-toolchain.toml, and the
# target of each row for it. rustup reads the toolchain file only in rust/.
install_toolchain() {
  local active row other seen

  cd "$WORKSPACE/$RUST_DIR" || die "the checkout has no $RUST_DIR directory"
  rustup toolchain install --no-self-update < /dev/null || die "rustup did not install the toolchain"
  active="$(rustup show active-toolchain < /dev/null)" || die "rustup named no active toolchain"
  TOOLCHAIN="${active%%[[:space:]]*}"
  [[ -n "$TOOLCHAIN" ]] || die "rustup named no active toolchain"
  say "toolchain: $TOOLCHAIN"

  for ((row = 0; row < ${#ROW_TARGET[@]}; row++)); do
    seen=no
    for ((other = 0; other < row; other++)); do
      if [[ "${ROW_TARGET[$other]}" == "${ROW_TARGET[$row]}" ]]; then
        seen=yes
      fi
    done
    if [[ "$seen" == no ]]; then
      rustup target add --toolchain "$TOOLCHAIN" "${ROW_TARGET[$row]}" < /dev/null ||
        die "rustup did not add the target ${ROW_TARGET[$row]}"
    fi
  done

  cd "$WORKSPACE" || die "cannot enter the checkout"
}

# run_words VARIABLE TREE TARGET WORD...: runs one command in the
# repository root. VARIABLE names TREE. No shell reads a word: the words go
# to `exec` as a list. CARGO_ENCODED_RUSTFLAGS would replace RUSTFLAGS, so
# no value of the caller reaches the build.
run_words() {
  local variable="$1" tree="$2" target="$3"

  shift 3
  (
    unset CARGO_ENCODED_RUSTFLAGS
    export "$variable=$tree"
    export CARGO_BUILD_TARGET="$target"
    export RUSTUP_TOOLCHAIN="$TOOLCHAIN"
    export CARGO_INCREMENTAL=0
    export RUSTFLAGS="$REMAP_FLAGS"
    exec -- "$@"
  ) < /dev/null
}

#: The members of one archive, sorted: `<component>/bin/<program>`. Their
#: bytes in sum.
MEMBERS=()
UNPACKED=0

# read_members COMPONENT TREE: fills MEMBERS and UNPACKED from TREE. The
# tree holds bin/ and one program or more directly in it: a regular file
# that its owner can run. Each other thing stops the run, because the
# archive holds programs only.
read_members() {
  local component="$1" tree="$2" path name

  MEMBERS=()
  UNPACKED=0
  while IFS= read -r path; do
    name="${path#"$tree"/}"
    case "$name" in
      bin)
        [[ -d "$path" && ! -L "$path" ]] || die "$component: bin in the tree is no directory"
        ;;
      bin/*/*)
        die "$component: the tree holds $name, which is no program directly in bin/"
        ;;
      bin/*)
        [[ -f "$path" && ! -L "$path" ]] ||
          die "$component: the tree holds $name, which is no regular file"
        [[ -x "$path" ]] || die "$component: the tree holds $name, which is no program"
        [[ "${name#bin/}" =~ $PLAIN_NAME ]] ||
          die "$component: the tree holds a program with a name that is not plain"
        MEMBERS+=("$component/$name")
        UNPACKED=$((UNPACKED + $(wc -c < "$path")))
        ;;
      *)
        die "$component: the tree holds $name, which is no program in bin/"
        ;;
    esac
  done < <(find "$tree" -mindepth 1 | sort)

  [[ "${#MEMBERS[@]}" -gt 0 ]] || die "$component: the build put no program in bin/ of its tree"
}

# build_row ROW: builds, walks, packs and checks the archive of one row.
build_row() {
  local row="$1" component tree archive command first count size

  component="${ROW_COMPONENT[$row]}"
  tree="$WORK/$component"
  archive="${ROW_ARCHIVE[$row]}"
  mkdir -- "$tree" || die "$component: cannot make an empty tree"

  # 1. Each command of the row.
  first="${ROW_FIRST[$row]}"
  for ((command = first; command < first + ROW_COMMANDS[row]; command++)); do
    count="${COMMAND_WORDS[$command]}"
    run_words "${ROW_ENV[$row]}" "$tree" "${ROW_TARGET[$row]}" \
      "${WORDS[@]:${COMMAND_FIRST[$command]}:$count}" ||
      die "$component: a build command failed"
  done

  # 2. The programs.
  read_members "$component" "$tree"

  # 3. The walk.
  "${CLI[@]}" check-tree --kind binary --tree "$tree" \
    --needle "$WORKSPACE" --needle "$HOME" --needle "$CARGO_DIR" --needle "$RUNNER_TEMP" \
    < /dev/null || die "$component: the walk found a fault in the tree"

  # 4. Two limits of the tree, then the pack.
  [[ "${#MEMBERS[@]}" -le "${ROW_MAX_MEMBERS[$row]}" ]] ||
    die "$component: the tree has ${#MEMBERS[@]} programs, more than ${ROW_MAX_MEMBERS[$row]}"
  [[ "$UNPACKED" -le "${ROW_MAX_UNPACKED[$row]}" ]] ||
    die "$component: the tree has $UNPACKED bytes, more than ${ROW_MAX_UNPACKED[$row]}"

  tar --create --file - --directory "$WORK" \
    --sort=name --owner=0 --group=0 --numeric-owner --mtime=@0 --mode=0755 \
    -- "${MEMBERS[@]}" | gzip --no-name > "$OUT/$archive" ||
    die "$component: the pack failed"

  # 5. The limit of the archive.
  size=$(($(wc -c < "$OUT/$archive")))
  if [[ "$size" -gt "${ROW_MAX_ARCHIVE[$row]}" ]]; then
    rm -f -- "$OUT/$archive"
    die "$component: the archive has $size bytes, more than ${ROW_MAX_ARCHIVE[$row]}"
  fi

  say "$archive: ${#MEMBERS[@]} programs, $UNPACKED bytes in the tree, $size bytes packed"
}

# write_digests TEXT: SHA256SUMS and the step output, each with TEXT.
write_digests() {
  if [[ -n "$1" ]]; then
    printf '%s\n' "$1" > "$OUT/$DIGEST_FILE" || die "cannot write $DIGEST_FILE"
  else
    : > "$OUT/$DIGEST_FILE" || die "cannot write $DIGEST_FILE"
  fi

  {
    printf '%s<<%s\n' "$DIGEST_OUTPUT" "$OUTPUT_END"
    if [[ -n "$1" ]]; then
      printf '%s\n' "$1"
    fi
    printf '%s\n' "$OUTPUT_END"
  } >> "$GITHUB_OUTPUT" || die "cannot write to GITHUB_OUTPUT"
}

# ------------------------------------------------------------------ main ----

need ARTIFACT_OUT GITHUB_WORKSPACE RUNNER_TEMP HOME GITHUB_OUTPUT
if [[ -n "${ARTIFACT_REPO:-}" && -n "${ARTIFACT_ROWS_FILE:-}" ]]; then
  die "give ARTIFACT_REPO or ARTIFACT_ROWS_FILE, not both"
fi
if [[ -z "${ARTIFACT_REPO:-}" && -z "${ARTIFACT_ROWS_FILE:-}" ]]; then
  die "the variable ARTIFACT_REPO is not set"
fi

WORKSPACE="$GITHUB_WORKSPACE"
CARGO_DIR="${CARGO_HOME:-$HOME/.cargo}"
OUT="$ARTIFACT_OUT"

# The resolver's own command, as handover/bin/allocate-tags.sh starts it.
if [[ -n "${RELEASE_CLI:-}" ]]; then
  # shellcheck disable=SC2206
  CLI=(${RELEASE_CLI})
else
  CLI=(uv run --project "$SELF_ROOT/handover" handover)
fi

if [[ -e "$OUT" ]]; then
  [[ -d "$OUT" && ! -L "$OUT" ]] || die "ARTIFACT_OUT names no directory"
  [[ -z "$(ls -A -- "$OUT")" ]] || die "ARTIFACT_OUT is not empty"
fi
mkdir -p -- "$OUT" || die "cannot make ARTIFACT_OUT"

cd "$WORKSPACE" || die "cannot enter the checkout"
ROWS="$(mktemp "$RUNNER_TEMP/build-artifacts-rows.XXXXXX")" || die "cannot make a file for the rows"
WORK=""
trap 'rm -rf -- "$ROWS" ${WORK:+"$WORK"}' EXIT

if [[ -n "${ARTIFACT_ROWS_FILE:-}" ]]; then
  cat -- "$ARTIFACT_ROWS_FILE" > "$ROWS" || die "cannot read ARTIFACT_ROWS_FILE"
else
  "${CLI[@]}" artifacts --repo "$ARTIFACT_REPO" < /dev/null > "$ROWS" ||
    die "handover gave no rows"
fi
read_rows "$ROWS"

if [[ "${#ROW_COMPONENT[@]}" -eq 0 ]]; then
  say "no binary component"
  write_digests ""
  exit 0
fi

# RUSTFLAGS is one text that cargo splits at each space.
case "$WORKSPACE$CARGO_DIR$RUNNER_TEMP" in
  *[[:space:]]*) die "a path of the runner holds a space, and RUSTFLAGS cannot carry it" ;;
esac
REMAP_FLAGS="--remap-path-prefix=$WORKSPACE=$REMAP_WORKSPACE"
REMAP_FLAGS="$REMAP_FLAGS --remap-path-prefix=$CARGO_DIR=$REMAP_CARGO_HOME"
REMAP_FLAGS="$REMAP_FLAGS --remap-path-prefix=$RUNNER_TEMP=$REMAP_TEMP"

install_toolchain
WORK="$(mktemp -d "$RUNNER_TEMP/build-artifacts.XXXXXX")" || die "cannot make a work directory"

NAMES=()
for ((ROW = 0; ROW < ${#ROW_COMPONENT[@]}; ROW++)); do
  build_row "$ROW"
  NAMES+=("${ROW_ARCHIVE[$ROW]}")
done

SUMS="$(cd -- "$OUT" && sha256sum -- "${NAMES[@]}")" || die "sha256sum did not read an archive"
write_digests "$SUMS"
say "$DIGEST_FILE: ${#NAMES[@]} archives"
