#!/usr/bin/env bash
# CI: the artifact probe. It measures each tool fact that a build of a binary
# component in CI, and a check of its signature on the host, depend on.
#   bin/artifact-probe.sh build    the job `build` of artifact-probe.yml
#   bin/artifact-probe.sh verify   the job `verify` of artifact-probe.yml
# Only .github/workflows/artifact-probe.yml runs it, on a GitHub runner. No
# hook and no unit runs it. A person or an agent starts that workflow by
# hand, on `main` only.
#
# The probe uses the real parts. `handover artifacts --crate-row` gives the
# row of the crate: the target, the names of the two assets, the install
# variable and the build words. bin/lib/artifactrows.sh reads the row, the
# one reader of that line form. bin/build-artifacts.sh builds, walks and
# packs with that row, as the job `artifacts` of release.yml does for a
# binary component. `handover artifacts --verify-words` gives the words of
# each check, as the tag script and root use them. So the probe holds no copy
# of the target, of a limit or of the words of a check.
#
# `build` needs rustup, cargo, GNU tar, gzip, sha256sum, `file` and the
# `handover` command. It reads the variables that a runner sets.
# `verify` needs sudo, systemd-run, jq and the `handover` command. It reads
# COSIGN (the cosign program, after the workflow compared its SHA-256),
# PROBE_SIGNED (the directory with the archive and its bundle),
# PROBE_TOOLCHAIN and PROBE_REMAP_NEEDED (two results of `build`, for the
# report).
# RELEASE_CLI is the `handover` command. Default: `uv run` against the
# `handover` project beside this script.
#
# It attaches nothing to a Release, makes no tag and changes no file of the
# repository. `verify` writes below /usr/local/bin and /var/lib of the
# runner, which lives for one job.
#
# `set -uo pipefail`, without `-e`: a failed case must count and must not
# stop the run, so one run records each result (bin/AGENTS.md). A fault
# that leaves nothing to measure stops the run through `die`.
#
# Bash 3.2-clean: its tests run it on a development machine, with a stub for
# each program that such a machine does not have.
set -uo pipefail
export LC_ALL=C

#: The crate that the probe builds. It is also the name of the row, the name
#: of the tree inside the archive, and the name of the one program of that
#: tree.
COMPONENT="agent-family"
CRATE_DIR="rust/crates/agent-family"

#: The catalog's name for this repository. The words of a check name the
#: release workflow of that repository.
CATALOG_REPO="agent-control"

#: The one directory from which rustup reads the toolchain file.
RUST_DIR="rust"

#: The name of the build with no remap flag.
NO_REMAP="plain"

#: The workflow file that the words of `handover artifacts --verify-words`
#: name, and the file that starts this script. The certificate of the
#: signature of a probe run names the second one. The probe replaces the
#: first name with the second one in one word.
RELEASE_FILE="release.yml"
WORKFLOW_FILE="artifact-probe.yml"

#: The flag before the ref claim of a check.
REF_FLAG="--certificate-github-workflow-ref"

#: Two values that no certificate of this run names.
ZERO_SHA="0000000000000000000000000000000000000000"
OTHER_REF="refs/heads/other"

#: Where root puts the verifier and its copy of the two files. A dynamic
#: user cannot read below /home, so neither is in the directory of the job.
VERIFIER="/usr/local/bin/cosign"
COPY_DIR="/var/lib/creche-handover/work/PROBE/artifacts/$COMPONENT"

#: A program path that names no file, for the status of a start fault.
ABSENT_PROGRAM="/usr/local/bin/cosign-absent"

SYSTEMD_RUN="/usr/bin/systemd-run"

#: The state directory of the transient unit. systemd makes it for the
#: dynamic user, and cosign keeps the trust root there.
STATE_NAME="creche-verify"
STATE_DIR="/var/lib/$STATE_NAME"
TRUSTED_ROOT_NAME="trusted_root.json"

#: The words that start each child, without `sudo`. No `--pipe`: the probe
#: reads the exit status of a child and no text.
RUN=(
  "$SYSTEMD_RUN" --quiet --wait --collect
  -p DynamicUser=yes
  -p "StateDirectory=$STATE_NAME"
  -p NoNewPrivileges=yes
  -p ProtectSystem=strict
  -p ProtectHome=yes
  -p PrivateTmp=yes
  -p PrivateDevices=yes
  -p CapabilityBoundingSet=
  -p MemoryMax=512M
  -p RuntimeMaxSec=120
  "--setenv=HOME=$STATE_DIR"
  "--setenv=TUF_ROOT=$STATE_DIR/tuf"
)

#: The property that takes the network from a child.
NO_NETWORK="PrivateNetwork=yes"

#: The unit of the release executor, and each sandbox line of it. The nested
#: case reads the value of each line from the file.
#: bin/tests/test_artifact_probe_workflow.py holds this list equal to the
#: file.
HANDOVER_UNIT="systemd/creche-handover.service"
SANDBOX_LINES=(
  UMask
  LockPersonality
  RestrictRealtime
  SystemCallArchitectures
  ProtectClock
  ProtectKernelModules
  ProtectKernelTunables
  ProtectKernelLogs
  ProtectHostname
  RestrictSUIDSGID
  RestrictAddressFamilies
)

#: The answers of a question that a run can leave open.
YES="yes"
NO="no"
UNKNOWN="unknown"

#: The `handover` command, as bin/build-artifacts.sh starts it.
SELF_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
if [[ -n "${RELEASE_CLI:-}" ]]; then
  # shellcheck disable=SC2206
  CLI=(${RELEASE_CLI})
else
  CLI=(uv run --project "$SELF_ROOT/handover" handover)
fi

#: The one reader of the line form of `handover artifacts`.
# shellcheck source=lib/artifactrows.sh
. "$SELF_ROOT/bin/lib/artifactrows.sh"

FAILS=0
say() { printf 'artifact-probe: %s\n' "$*"; }
pass() { say "PASS: $*"; }
fail() {
  say "FAIL: $*"
  FAILS=$((FAILS + 1))
}
die() {
  printf 'artifact-probe: %s\n' "$*" >&2
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

# note TEXT: one line in the log and one line of the step summary. It stops
# the run when the summary takes no line: a result that no page shows is not
# a recorded result.
note() {
  say "$1"
  printf -- '- %s\n' "$1" >> "$GITHUB_STEP_SUMMARY" ||
    die "cannot write to GITHUB_STEP_SUMMARY"
}

#: The arguments of the one jq call that writes the report: each result is
#: one key.
REPORT=()

# text KEY VALUE: one result that is text.
text() {
  REPORT+=(--arg "$1" "$2")
  note "$1: $2"
}

# value KEY JSON: one result that is a JSON number or `null`.
value() {
  REPORT+=(--argjson "$1" "$2")
  note "$1: $2"
}

# read_row FILE: asks `handover artifacts --crate-row` for the row of the
# crate and writes it to FILE. bin/lib/artifactrows.sh reads the file into
# its arrays. The file holds exactly one row: the row of the crate, with one
# command. Status 1, with the reason in ROWS_FAULT, for a command that fails
# and for each other text.
read_row() {
  local file="$1"

  if ! "${CLI[@]}" artifacts --crate-row "$COMPONENT" --crate-dir "$CRATE_DIR" \
    < /dev/null > "$file"; then
    ROWS_FAULT="the command failed"
    return 1
  fi

  read_rows "$file" || return 1
  if [[ "${#ROW_COMPONENT[@]}" -ne 1 ]]; then
    ROWS_FAULT="the text holds ${#ROW_COMPONENT[@]} rows, not 1"
    return 1
  fi
  if [[ "${ROW_COMPONENT[0]}" != "$COMPONENT" ]]; then
    ROWS_FAULT="the row names another component"
    return 1
  fi
  if [[ "${ROW_COMMANDS[0]}" -ne 1 ]]; then
    ROWS_FAULT="the row holds ${ROW_COMMANDS[0]} commands, not 1"
    return 1
  fi
}

# bytes_of FILE: the size of FILE.
bytes_of() {
  printf '%s\n' "$(($(wc -c < "$1")))"
}

# ---------------------------------------------------------------- build ----

#: The four paths of the runner that no file of a tree can hold, and the name
#: of each one. `build` fills the two lists. The build with no remap flag
#: reads them. bin/build-artifacts.sh holds the same four needles for its
#: walk.
NEEDLES=()
NEEDLE_NAMES=()

#: The name of the toolchain that rust/rust-toolchain.toml gives.
TOOLCHAIN=""

# run_build ROWS OUT STEP_OUTPUT: one run of bin/build-artifacts.sh with the
# row of ROWS, into the empty directory OUT. The script writes its step
# output to STEP_OUTPUT, so the outputs of this step stay the four of the
# probe.
run_build() {
  env -u ARTIFACT_REPO \
    ARTIFACT_ROWS_FILE="$1" \
    ARTIFACT_OUT="$2" \
    GITHUB_OUTPUT="$3" \
    "$SELF_ROOT/bin/build-artifacts.sh"
}

# install_plain ROOT: builds the crate into ROOT with no remap flag, from the
# repository root. The words, the target and the install variable come from
# the row. The row has one command, so COMMAND_WORD holds its words. cargo
# reads the toolchain from RUSTUP_TOOLCHAIN: a rustup proxy does not read
# the toolchain file of rust/ from the repository root. No shell reads a
# word: the words go to `exec` as a list.
install_plain() {
  local root="$1"

  mkdir -p "$root" || return 1
  (
    unset CARGO_ENCODED_RUSTFLAGS RUSTFLAGS
    export "${ROW_ENV[0]}=$root"
    export CARGO_BUILD_TARGET="${ROW_TARGET[0]}"
    export RUSTUP_TOOLCHAIN="$TOOLCHAIN"
    export CARGO_INCREMENTAL=0
    exec -- "${COMMAND_WORD[@]}"
  ) < /dev/null
}

# path_hits TREE: prints one line for each file of TREE that holds a path of
# the runner, with the name of that path. Status 1 when grep cannot read the
# tree: a tree that nothing read is not a clean tree.
path_hits() {
  local tree="$1" index found status file

  for index in 0 1 2 3; do
    found="$(grep -r -l -F -e "${NEEDLES[$index]}" -- "$tree")"
    status=$?
    if [[ "$status" -gt 1 ]]; then
      return 1
    fi

    while IFS= read -r file; do
      if [[ -n "$file" ]]; then
        printf '%s holds %s\n' "${file#"$tree"/}" "${NEEDLE_NAMES[$index]}"
      fi
    done <<< "$found"
  done
}

# show_paths TREE: prints up to 20 texts of TREE that start with a path of
# the runner. `path_hits` says which file holds a path, and this says which
# path it is: the log of a run then names the cause. It fails nothing.
show_paths() {
  local tree="$1" index needle

  for index in 0 1 2 3; do
    needle="$(printf '%s' "${NEEDLES[$index]}" | sed 's/[][\\.*^$]/\\&/g')"
    grep -r -a -o -h -e "$needle[ -~]\{0,160\}" -- "$tree"
  done | sort -u | head -n 20
}

# members_hold ARCHIVE: whether ARCHIVE holds `<component>/bin/<component>`
# and no other member, as a regular file with the mode 0755 and the owner 0.
# The probe reads the archive that the build script packed. It does not
# trust the words that the script gave to tar.
members_hold() {
  local archive="$1" names

  names="$(tar --list --gzip --file "$archive")" || return 1
  [[ "$names" == "$COMPONENT/bin/$COMPONENT" ]] || return 1

  tar --list --verbose --numeric-owner --gzip --file "$archive" |
    awk '$1 != "-rwxr-xr-x" || $2 != "0/0" { bad = 1 } END { exit bad }'
}

build() {
  need GITHUB_WORKSPACE RUNNER_TEMP GITHUB_OUTPUT GITHUB_STEP_SUMMARY HOME

  local workspace="$GITHUB_WORKSPACE" work="$RUNNER_TEMP/artifact-probe"
  local cargo_home="${CARGO_HOME:-$HOME/.cargo}"
  local row="$work/row" plain="$work/$NO_REMAP" unpacked="$work/unpacked"
  local pack_one="$work/pack-1" pack_two="$work/pack-2"
  local active name program kind hits remap_needed line

  NEEDLES=("$workspace" "$HOME" "$RUNNER_TEMP" "$cargo_home")
  NEEDLE_NAMES=(GITHUB_WORKSPACE HOME RUNNER_TEMP "the cargo home")

  [[ ! -e "$work" ]] || die "$work exists: each install root must start empty"
  mkdir -p "$unpacked" || die "cannot make $work"
  cd "$workspace" || die "cannot enter the checkout"

  # 1. The row of the crate.
  read_row "$row" || die "handover gave no row of $COMPONENT in the line form: $ROWS_FAULT"
  name="${ROW_ARCHIVE[0]}"
  note "target: ${ROW_TARGET[0]}"

  # 2. The real build: bin/build-artifacts.sh with the row. It installs the
  # toolchain of rust/rust-toolchain.toml and the target. It builds from the
  # repository root with each path of the runner remapped. `handover
  # check-tree` walks the tree, and one file that holds a path of the runner
  # stops the run: the archive of such a tree gets no signature. A toolchain
  # with the component `rust-src` puts its own directory, below HOME, into a
  # program, and no remap flag remaps it. The toolchain file of rust/ names
  # no such component.
  run_build "$row" "$pack_one" "$work/output-1" || die "the build failed"
  active="$(cd "$RUST_DIR" && rustup show active-toolchain)" || die "rustup named no active toolchain"
  TOOLCHAIN="${active%%[[:space:]]*}"
  [[ -n "$TOOLCHAIN" ]] || die "rustup named no active toolchain"
  note "toolchain: $TOOLCHAIN"
  note "tree: no file holds a path of the runner"

  # 3. A second run of the build script gives the same bytes. cargo takes the
  # build of the first run, so the two archives pack one program. The archive
  # holds the one program as a regular file with mode 0755.
  run_build "$row" "$pack_two" "$work/output-2" || die "the second build failed"
  cmp -s "$pack_one/$name" "$pack_two/$name" || die "two runs of the build script gave two archives"
  members_hold "$pack_one/$name" ||
    die "a member of the archive is not a regular file $COMPONENT/bin/$COMPONENT with mode 0755"
  note "archive: $name, $(bytes_of "$pack_one/$name") bytes, and two runs give equal archives"

  # 4. The program of the archive has a static link: a build for the runner
  # itself makes a program that needs the C library of the runner. And the
  # program runs.
  tar --extract --gzip --file "$pack_one/$name" --directory "$unpacked" ||
    die "cannot unpack the archive"
  program="$unpacked/$COMPONENT/bin/$COMPONENT"
  [[ -f "$program" && ! -L "$program" && -x "$program" ]] ||
    die "the archive holds no program at $COMPONENT/bin/$COMPONENT"
  kind="$(file --brief -- "$program")" || die "file did not read the program"
  say "file: $kind"
  [[ "$kind" == *x86-64* ]] || die "the program is not an x86-64 program"
  [[ "$kind" == *"statically linked"* || "$kind" == *"static-pie linked"* ]] ||
    die "the program has no static link"
  "$program" --help > /dev/null || die "the program did not run: --help failed"
  note "program: a static x86-64 program, and --help ended with status 0"

  # 5. The same build with no remap flag. This step stops nothing: it says
  # whether the flags are necessary.
  remap_needed="$UNKNOWN"
  if install_plain "$plain/$COMPONENT"; then
    if hits="$(path_hits "$plain/$COMPONENT")"; then
      remap_needed="$NO"
      if [[ -n "$hits" ]]; then
        remap_needed="$YES"
        printf '%s\n' "$hits"
        show_paths "$plain/$COMPONENT"
      fi
    fi
  else
    say "the build with no remap flag failed: no answer"
  fi
  note "remap_needed: $remap_needed"

  # 6. The digest of the build script: one line of `sha256sum`, for the job
  # `sign`.
  line="$(cat -- "$pack_one/SHA256SUMS")" || die "the build gave no SHA256SUMS"
  [[ "$line" == *"  $name" && "$line" != *$'\n'* ]] ||
    die "SHA256SUMS of the build does not name the one archive $name"
  {
    printf 'digest=%s\n' "$line"
    printf 'archive=%s\n' "$pack_one/$name"
    printf 'toolchain=%s\n' "$TOOLCHAIN"
    printf 'remap_needed=%s\n' "$remap_needed"
  } >> "$GITHUB_OUTPUT" || die "cannot write to GITHUB_OUTPUT"
  say "digest: $line"
}

# --------------------------------------------------------------- verify ----

#: The program words of one check, and of the check that passed.
WORDS=()
PASSING=()

#: The words before the program words of the check that passed, without
#: `sudo`: RUN, and the property NO_NETWORK when the check with two children
#: passed. The trusted root file of that check, or nothing.
PREFIX=()
PASSING_ROOT=""

#: The exit status of the last child, and the second at which it started.
STATUS=0
STARTED=0

# now_ms: the time in milliseconds. A bash older than 5 has no clock of its
# own, and gives whole seconds.
now_ms() {
  local now="${EPOCHREALTIME:-}"

  if [[ "$now" =~ ^[0-9]+\.[0-9]{6}$ ]]; then
    printf '%s\n' "$((${now%.*} * 1000 + 10#${now#*.} / 1000))"

    return
  fi

  printf '%s\n' "$(($(date +%s) * 1000))"
}

# run_case NAME WORD...: starts one command and waits for it. It records
# `status_<NAME>` and `seconds_<NAME>`, and it reads no text of the command.
run_case() {
  local name="$1" start end spent

  shift
  STARTED="$(date +%s)"
  start="$(now_ms)"
  "$@" < /dev/null
  STATUS=$?
  end="$(now_ms)"
  spent=$((end - start))

  value "status_$name" "$STATUS"
  value "seconds_$name" "$(printf '%d.%03d' "$((spent / 1000))" "$((spent % 1000))")"
}

# no_case NAME: records a case that did not run.
no_case() {
  value "status_$1" null
  value "seconds_$1" null
}

# journal: prints what the journal holds of the last child, for a case with a
# result that the probe did not expect: the lines of systemd, of cosign and
# of a nested systemd-run. The text goes to the log of the job and never to
# the report. The step fails nothing.
journal() {
  say "the journal since the start of that case:"
  sudo journalctl --no-pager --since "@$STARTED" --lines 200 \
    --identifier systemd --identifier cosign --identifier systemd-run || true
}

#: The file that takes the words of `handover artifacts --verify-words`.
WORDS_FILE=""

# release_words ROOT SHA BLOB BUNDLE: sets WORDS to the program words of one
# check, as `handover artifacts --verify-words` gives them after the program
# name. They name the release workflow of the repository. ROOT is a trusted
# root file, or empty: cosign then reads the trust root itself. Status 1 when
# the command gives no word.
release_words() {
  local root="$1" sha="$2" blob="$3" bundle="$4" word
  local args=(
    artifacts --verify-words --repo "$CATALOG_REPO"
    --github-repository "$GITHUB_REPOSITORY" --sha "$sha"
    --archive "$blob" --bundle "$bundle"
  )

  if [[ -n "$root" ]]; then
    args+=(--trusted-root "$root")
  fi

  "${CLI[@]}" "${args[@]}" < /dev/null > "$WORDS_FILE" || return 1
  WORDS=("$VERIFIER")
  while IFS= read -r word; do
    WORDS+=("$word")
  done < "$WORDS_FILE"

  [[ "${#WORDS[@]}" -gt 1 ]]
}

# probe_words ROOT SHA BLOB BUNDLE: the words of `release_words`, with the
# name of this workflow file in the place of the release workflow. A probe
# run signs as WORKFLOW_FILE. Status 1 when not one word names the release
# workflow: the words then are not the words of the check that root makes.
probe_words() {
  local index word changed=0

  release_words "$@" || return 1
  for ((index = 1; index < ${#WORDS[@]}; index++)); do
    word="${WORDS[$index]}"
    if [[ "$word" == *"/$RELEASE_FILE@"* ]]; then
      WORDS[$index]="${word//\/$RELEASE_FILE@//$WORKFLOW_FILE@}"
      changed=$((changed + 1))
    fi
  done

  [[ "$changed" -eq 1 ]]
}

# other_ref: the word after REF_FLAG in WORDS becomes OTHER_REF. The
# identity still names the ref of `main`, so the case measures the one claim
# of the ref. Status 1 when not one word is REF_FLAG.
other_ref() {
  local index found=0

  for ((index = 1; index + 1 < ${#WORDS[@]}; index++)); do
    if [[ "${WORDS[$index]}" == "$REF_FLAG" ]]; then
      WORDS[index + 1]="$OTHER_REF"
      found=$((found + 1))
    fi
  done

  [[ "$found" -eq 1 ]]
}

# no_words NAME: a case whose words the probe did not get. It fails the
# probe, and the report names the case as one that did not run.
no_words() {
  fail "$1: handover gave no words for the check"
  no_case "$1"
}

# must_fail NAME: one check with the words of WORDS. Each such check differs
# from the check that passed in one word. Status 0 fails the probe.
must_fail() {
  local name="$1"

  run_case "$name" sudo "${PREFIX[@]}" -- "${WORDS[@]}"
  if [[ "$STATUS" -eq 0 ]]; then
    fail "$name: a check that must fail ended with status 0"
    journal

    return
  fi

  pass "$name: status $STATUS"
}

# change_byte SOURCE COPY: makes COPY from SOURCE with another first byte.
# Status 0 only when cmp says that the two files differ.
change_byte() {
  local source="$1" copy="$2" byte

  byte="$(od -An -tu1 -N1 -- "$source" | tr -d '[:space:]')"
  [[ "$byte" =~ ^[0-9]+$ ]] || return 1
  byte="$(printf '%03o' "$(((byte + 1) % 256))")"

  sudo install -o root -g root -m 0644 -- "$source" "$copy" || return 1
  printf "\\$byte" | sudo dd of="$copy" bs=1 count=1 conv=notrunc status=none || return 1

  sudo cmp -s -- "$source" "$copy"
  [[ "$?" -eq 1 ]]
}

# nested_words UNIT_FILE: sets NESTED to the words that start a transient
# unit with each sandbox line of the release executor. Status 1 when the
# file does not hold a line one time.
NESTED=()
nested_words() {
  local unit_file="$1" name line

  NESTED=("$SYSTEMD_RUN" --quiet --wait --collect)
  for name in "${SANDBOX_LINES[@]}"; do
    line="$(grep -E "^$name=" -- "$unit_file")" || return 1
    [[ "$line" != *$'\n'* ]] || return 1
    NESTED+=(-p "$line")
  done
}

verify() {
  need GITHUB_WORKSPACE RUNNER_TEMP GITHUB_OUTPUT GITHUB_STEP_SUMMARY \
    GITHUB_REPOSITORY GITHUB_SHA PROBE_SIGNED COSIGN

  local name archive bundle copy copy_bundle changed report roots root_file
  local root_count two_children systemd_version cosign_version words one_child

  [[ -x "$COSIGN" ]] || die "COSIGN names no program"
  [[ "$GITHUB_SHA" =~ ^[0-9a-f]{40}$ ]] || die "GITHUB_SHA is not a commit id"
  [[ "$GITHUB_REPOSITORY" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]] ||
    die "GITHUB_REPOSITORY is not an owner and a repository"

  read_row "$RUNNER_TEMP/probe-row" ||
    die "handover gave no row of $COMPONENT in the line form: $ROWS_FAULT"
  name="${ROW_ARCHIVE[0]}"
  archive="$PROBE_SIGNED/$name"
  bundle="$PROBE_SIGNED/${ROW_BUNDLE[0]}"
  copy="$COPY_DIR/$name"
  copy_bundle="$COPY_DIR/${ROW_BUNDLE[0]}"
  changed="$copy.changed"
  report="$RUNNER_TEMP/probe-report.json"
  WORDS_FILE="$RUNNER_TEMP/probe-words"

  [[ -f "$archive" ]] || die "the job sign gave no file $name"
  [[ -f "$bundle" ]] || die "the job sign gave no file ${ROW_BUNDLE[0]}"

  # The words of the first check. A command that gives no such words stops
  # the run here, before the first child.
  probe_words "" "$GITHUB_SHA" "$copy" "$copy_bundle" ||
    die "handover gave no words in which one word names $RELEASE_FILE"
  one_child=("${WORDS[@]}")

  # 1. Root owns the verifier and the copy of the two files.
  sudo install -o root -g root -m 0755 -- "$COSIGN" "$VERIFIER" ||
    die "cannot install the verifier"
  sudo install -d -o root -g root -m 0755 -- "$COPY_DIR" || die "cannot make $COPY_DIR"
  sudo install -o root -g root -m 0644 -- "$archive" "$copy" || die "cannot copy the archive"
  sudo install -o root -g root -m 0644 -- "$bundle" "$copy_bundle" ||
    die "cannot copy the bundle"

  # From here each failure counts, and the report is written. Without this
  # line the upload step has no path, so the run stops when the write fails.
  printf 'report=%s\n' "$report" >> "$GITHUB_OUTPUT" ||
    die "cannot write to GITHUB_OUTPUT"

  # 2. The words that start each child are RUN, above.

  text target "${ROW_TARGET[0]}"
  text toolchain "${PROBE_TOOLCHAIN:-$UNKNOWN}"
  text remap_needed "${PROBE_REMAP_NEEDED:-$UNKNOWN}"

  # 3. One child: cosign reads the trust root from the network and checks
  # the signature.
  run_case one_child sudo "${RUN[@]}" -- "${one_child[@]}"
  if [[ "$STATUS" -eq 0 ]]; then
    pass "one_child"
    PASSING=("${one_child[@]}")
    PREFIX=("${RUN[@]}")
  else
    fail "one_child: the check ended with status $STATUS"
    journal
  fi

  # 4. The refresh child: cosign writes the trust root into the state
  # directory. The path of the file there is a fact that the probe reports.
  run_case refresh sudo "${RUN[@]}" -- "$VERIFIER" initialize
  if [[ "$STATUS" -eq 0 ]]; then
    pass "refresh"
  else
    fail "refresh: cosign initialize ended with status $STATUS"
    journal
  fi

  roots="$(sudo find "$STATE_DIR/" -type f -name "$TRUSTED_ROOT_NAME" | sort)"
  root_file="${roots%%$'\n'*}"
  root_count=0
  if [[ -n "$roots" ]]; then
    root_count="$(($(printf '%s\n' "$roots" | wc -l)))"
  fi

  if [[ -n "$root_file" ]]; then
    text trusted_root_path "$root_file"
  else
    value trusted_root_path null
  fi
  value trusted_root_count "$root_count"

  # 5. Two children: the second one has no network and reads the trust root
  # from the file of step 4. A status that is not 0 fails nothing: the probe
  # then reports that the way with one child is the way that works.
  two_children="$NO"
  if [[ -z "$root_file" ]]; then
    no_case two_children
  elif ! probe_words "$root_file" "$GITHUB_SHA" "$copy" "$copy_bundle"; then
    no_words two_children
  else
    run_case two_children sudo "${RUN[@]}" -p "$NO_NETWORK" -- "${WORDS[@]}"
    if [[ "$STATUS" -eq 0 ]]; then
      two_children="$YES"
      PASSING=("${WORDS[@]}")
      PREFIX=("${RUN[@]}" -p "$NO_NETWORK")
      PASSING_ROOT="$root_file"
    else
      say "two_children: the check with no network ended with status $STATUS"
      journal
    fi
  fi
  text two_children "$two_children"

  if [[ "${#PASSING[@]}" -gt 0 ]]; then
    # 6. Four checks that must fail. Each one differs from the check that
    # passed in one word. The words of the release workflow, as root makes
    # them, are the check with another workflow.
    if probe_words "$PASSING_ROOT" "$ZERO_SHA" "$copy" "$copy_bundle"; then
      must_fail zero_sha
    else
      no_words zero_sha
    fi
    if probe_words "$PASSING_ROOT" "$GITHUB_SHA" "$copy" "$copy_bundle" && other_ref; then
      must_fail other_ref
    else
      no_words other_ref
    fi
    if release_words "$PASSING_ROOT" "$GITHUB_SHA" "$copy" "$copy_bundle"; then
      must_fail other_workflow
    else
      no_words other_workflow
    fi
    if ! change_byte "$archive" "$changed"; then
      fail "changed_byte: cannot make a copy of the archive with one changed byte"
      no_case changed_byte
    elif probe_words "$PASSING_ROOT" "$GITHUB_SHA" "$changed" "$copy_bundle"; then
      must_fail changed_byte
    else
      no_words changed_byte
    fi
  else
    say "no check passed: the four checks that must fail did not run"
    no_case zero_sha
    no_case other_ref
    no_case other_workflow
    no_case changed_byte
  fi

  # 7. The status of two fault classes: a program that does not start, and
  # a child that passes its time limit. The probe records each status.
  run_case absent_program sudo "${RUN[@]}" -- "$ABSENT_PROGRAM"
  run_case time_limit sudo "${RUN[@]}" -p RuntimeMaxSec=1 -- /usr/bin/sleep 5

  # 8. The nested case: the release executor starts the child from inside
  # its own unit. That unit has sandbox lines of its own.
  if [[ "${#PASSING[@]}" -eq 0 ]]; then
    say "no check passed: the nested case did not run"
    no_case nested
  elif nested_words "$GITHUB_WORKSPACE/$HANDOVER_UNIT"; then
    run_case nested sudo "${NESTED[@]}" -- "${PREFIX[@]}" -- "${PASSING[@]}"
    if [[ "$STATUS" -eq 0 ]]; then
      pass "nested"
    else
      fail "nested: the check inside the sandbox of the executor ended with status $STATUS"
      journal
    fi
  else
    fail "nested: $HANDOVER_UNIT does not hold each sandbox line one time"
    no_case nested
  fi

  # 9. The versions and the sizes.
  systemd_version="$(systemctl --version 2> /dev/null | sed -n '1p')"
  cosign_version="$("$COSIGN" version 2>&1 | sed -n 's/^GitVersion:[[:space:]]*//p' | sed -n '1p')"
  text systemd_version "$systemd_version"
  text cosign_version "$cosign_version"
  value archive_bytes "$(bytes_of "$archive")"
  value bundle_bytes "$(bytes_of "$bundle")"

  # 10. The report: one jq call writes one strict JSON text. It holds no
  # token and no text of a child.
  # jq reads a word that starts with `-` as an option of its own, so the
  # words go in as one text with one word on each line. No word holds a
  # newline: each one is a constant, a value that this script checked, or
  # one line of `find`.
  note "verify_words: ${PASSING[*]:-}"
  words="$(printf '%s\n' ${PASSING[@]+"${PASSING[@]}"})"
  if jq --null-input --sort-keys "${REPORT[@]}" --arg words "$words" \
    '$ARGS.named | del(.words) | .verify_words = ($words | split("\n"))' > "$report"; then
    cat -- "$report"
  else
    rm -f -- "$report"
    fail "jq did not write the report"
  fi

  if [[ "$FAILS" -eq 0 ]]; then
    say "PROBE: PASS"

    return 0
  fi

  say "PROBE: FAIL ($FAILS)"

  return 1
}

case "${1:-}" in
  build)
    build
    ;;
  verify)
    verify
    ;;
  *)
    die "usage: bin/artifact-probe.sh build|verify"
    ;;
esac
