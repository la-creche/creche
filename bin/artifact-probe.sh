#!/usr/bin/env bash
# CI: the artifact probe. It measures each tool fact that a build of a binary
# component in CI, and a check of its signature on the host, depend on.
#   bin/artifact-probe.sh build    the job `build` of artifact-probe.yml
#   bin/artifact-probe.sh verify   the job `verify` of artifact-probe.yml
# Only .github/workflows/artifact-probe.yml runs it, on a GitHub runner. No
# hook and no unit runs it. A person or an agent starts that workflow by
# hand, on `main` only.
#
# `build` needs rustup, cargo, GNU tar, gzip, sha256sum and `file`. It reads
# PROBE_TARGET, and the variables that a runner sets.
# `verify` needs sudo, systemd-run and jq. It reads PROBE_TARGET, COSIGN (the
# cosign program, after the workflow compared its SHA-256), PROBE_SIGNED (the
# directory with the archive and its bundle), PROBE_TOOLCHAIN and
# PROBE_REMAP_NEEDED (two results of `build`, for the report).
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

#: The crate that the probe builds. It is also the name of the tree inside
#: the archive, and the name of the one program of that tree.
COMPONENT="agent-family"
CRATE_DIR="rust/crates/agent-family"

#: The one directory from which rustup reads the toolchain file.
RUST_DIR="rust"

#: The two forms of the build. The first one remaps each path of the runner.
WITH_REMAP="remap"
NO_REMAP="plain"

#: What the remap flags put in the place of the three paths.
REMAP_WORKSPACE="/probe/workspace"
REMAP_CARGO_HOME="/probe/cargo-home"
REMAP_TEMP="/probe/temp"

#: What the certificate of a signature must name. The workflow file is the
#: file that starts this script, and the ref is the one ref that the workflow
#: measures on.
WORKFLOW_FILE="artifact-probe.yml"
MAIN_REF="refs/heads/main"
CODE_HOST="https://github.com"
TOKEN_ISSUER="https://token.actions.githubusercontent.com"

#: Three values that no certificate of this run names.
ZERO_SHA="0000000000000000000000000000000000000000"
OTHER_REF="refs/heads/other"
OTHER_WORKFLOW="release.yml"

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

# note TEXT: one line in the log and one line of the step summary.
note() {
  say "$1"
  printf -- '- %s\n' "$1" >> "$GITHUB_STEP_SUMMARY"
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

# archive_name: the name of the archive for the target. It stops the run
# for a target that is not one plain word: the name goes into a path and
# into a command line.
archive_name() {
  [[ "$PROBE_TARGET" =~ ^[A-Za-z0-9_-]+$ ]] || die "PROBE_TARGET is not the name of a target"
  printf '%s-%s.tar.gz\n' "$COMPONENT" "$PROBE_TARGET"
}

# bytes_of FILE: the size of FILE.
bytes_of() {
  printf '%s\n' "$(($(wc -c < "$1")))"
}

# ---------------------------------------------------------------- build ----

#: The four paths of the runner that no file of a tree can hold, and the name
#: of each one. `build` fills the two lists.
NEEDLES=()
NEEDLE_NAMES=()

#: The name of the toolchain that rust/rust-toolchain.toml gives, and the
#: remap flags of the first build.
TOOLCHAIN=""
REMAP_FLAGS=""

# install_tree ROOT FORM: builds the crate into ROOT, from the repository
# root. cargo reads the target from CARGO_BUILD_TARGET, and the toolchain
# from RUSTUP_TOOLCHAIN: a rustup proxy does not read the toolchain file of
# rust/ from the repository root.
install_tree() {
  local root="$1" form="$2"

  mkdir -p "$root" || return 1

  if [[ "$form" == "$WITH_REMAP" ]]; then
    env -u CARGO_ENCODED_RUSTFLAGS \
      CARGO_INSTALL_ROOT="$root" \
      CARGO_BUILD_TARGET="$PROBE_TARGET" \
      RUSTUP_TOOLCHAIN="$TOOLCHAIN" \
      CARGO_INCREMENTAL=0 \
      RUSTFLAGS="$REMAP_FLAGS" \
      cargo install --locked --no-track --path "$CRATE_DIR"

    return
  fi

  env -u CARGO_ENCODED_RUSTFLAGS -u RUSTFLAGS \
    CARGO_INSTALL_ROOT="$root" \
    CARGO_BUILD_TARGET="$PROBE_TARGET" \
    RUSTUP_TOOLCHAIN="$TOOLCHAIN" \
    CARGO_INCREMENTAL=0 \
    cargo install --locked --no-track --path "$CRATE_DIR"
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

#: The members of the archive, sorted: `<component>/bin/<program>`.
MEMBERS=()

# read_members ROOT: fills MEMBERS from the tree at ROOT. Status 1 when the
# tree holds a thing that is no regular file directly in bin/.
read_members() {
  local root="$1" path name

  MEMBERS=()
  while IFS= read -r path; do
    name="${path#"$root"/}"
    case "$name" in
      bin)
        [[ -d "$path" && ! -L "$path" ]] || return 1
        ;;
      bin/*/*)
        return 1
        ;;
      bin/*)
        [[ -f "$path" && ! -L "$path" ]] || return 1
        [[ "${name#bin/}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || return 1
        MEMBERS+=("$COMPONENT/$name")
        ;;
      *)
        return 1
        ;;
    esac
  done < <(find "$root" -mindepth 1 | sort)

  [[ "${#MEMBERS[@]}" -gt 0 ]]
}

# pack PARENT OUT: writes the archive of the tree PARENT/<component> to OUT.
# Each input that can differ between two runs has a fixed value: the order,
# the owner, the group, the time and the mode of a member, and the name and
# the time that gzip can keep.
pack() {
  local parent="$1" out="$2"

  tar --create --file - --directory "$parent" \
    --sort=name --owner=0 --group=0 --numeric-owner --mtime=@0 --mode=0755 \
    -- "${MEMBERS[@]}" | gzip --no-name > "$out"
}

# members_hold ARCHIVE: whether ARCHIVE holds each name of MEMBERS and no
# other, each one as a regular file with the mode 0755 and the owner 0.
members_hold() {
  local archive="$1" names wanted

  wanted="$(printf '%s\n' "${MEMBERS[@]}")"
  names="$(tar --list --gzip --file "$archive")" || return 1
  [[ "$names" == "$wanted" ]] || return 1

  tar --list --verbose --numeric-owner --gzip --file "$archive" |
    awk '$1 != "-rwxr-xr-x" || $2 != "0/0" { bad = 1 } END { exit bad }'
}

build() {
  need GITHUB_WORKSPACE RUNNER_TEMP GITHUB_OUTPUT GITHUB_STEP_SUMMARY HOME PROBE_TARGET

  local workspace="$GITHUB_WORKSPACE" work="$RUNNER_TEMP/artifact-probe"
  local cargo_home="${CARGO_HOME:-$HOME/.cargo}"
  local remapped="$work/$WITH_REMAP" plain="$work/$NO_REMAP"
  local pack_one="$work/pack-1" pack_two="$work/pack-2"
  local name active program kind hits remap_needed line

  name="$(archive_name)" || exit 1
  NEEDLES=("$workspace" "$HOME" "$RUNNER_TEMP" "$cargo_home")
  NEEDLE_NAMES=(GITHUB_WORKSPACE HOME RUNNER_TEMP "the cargo home")

  # RUSTFLAGS is one text that cargo splits at each space.
  case "$workspace$cargo_home$RUNNER_TEMP" in
    *[[:space:]]*) die "a path of the runner holds a space, and RUSTFLAGS cannot carry it" ;;
  esac
  REMAP_FLAGS="--remap-path-prefix=$workspace=$REMAP_WORKSPACE"
  REMAP_FLAGS="$REMAP_FLAGS --remap-path-prefix=$cargo_home=$REMAP_CARGO_HOME"
  REMAP_FLAGS="$REMAP_FLAGS --remap-path-prefix=$RUNNER_TEMP=$REMAP_TEMP"

  [[ ! -e "$work" ]] || die "$work exists: each install root must start empty"
  mkdir -p "$pack_one" "$pack_two" || die "cannot make $work"

  # 1. The toolchain of rust/rust-toolchain.toml, and the target for it.
  cd "$workspace/$RUST_DIR" || die "the checkout has no $RUST_DIR directory"
  rustup toolchain install --no-self-update || die "rustup did not install the toolchain"
  active="$(rustup show active-toolchain)" || die "rustup named no active toolchain"
  TOOLCHAIN="${active%%[[:space:]]*}"
  [[ -n "$TOOLCHAIN" ]] || die "rustup named no active toolchain"
  rustup target add --toolchain "$TOOLCHAIN" "$PROBE_TARGET" ||
    die "rustup did not add the target $PROBE_TARGET"
  note "toolchain: $TOOLCHAIN"
  note "target: $PROBE_TARGET"

  # 2. The build, from the repository root, with each path remapped.
  cd "$workspace" || die "cannot enter the checkout"
  install_tree "$remapped/$COMPONENT" "$WITH_REMAP" || die "the build failed"

  # 3. cargo read the two variables. The program is in bin/ of the install
  # root. It has a static link: a build for the runner itself makes a
  # program that needs the C library of the runner. And the program runs.
  program="$remapped/$COMPONENT/bin/$COMPONENT"
  [[ -f "$program" && ! -L "$program" && -x "$program" ]] ||
    die "the build made no program at bin/$COMPONENT of its tree"
  kind="$(file --brief -- "$program")" || die "file did not read the program"
  say "file: $kind"
  [[ "$kind" == *x86-64* ]] || die "the program is not an x86-64 program"
  [[ "$kind" == *"statically linked"* || "$kind" == *"static-pie linked"* ]] ||
    die "the program has no static link"
  "$program" --help > /dev/null || die "the program did not run: --help failed"
  note "program: a static x86-64 program, and --help ended with status 0"

  # 4. No file of the tree holds a path of the runner. One hit stops the
  # run: the archive of such a tree gets no signature. A toolchain with the
  # component `rust-src` puts its own directory, below HOME, into a program,
  # and no flag above remaps it. The toolchain file of rust/ names no such
  # component.
  hits="$(path_hits "$remapped/$COMPONENT")" || die "grep did not read the tree"
  if [[ -n "$hits" ]]; then
    printf '%s\n' "$hits"
    show_paths "$remapped/$COMPONENT"
    die "a file of the tree holds a path of the runner"
  fi
  note "tree: no file holds a path of the runner"

  # 5. The same build with no remap flag. This step stops nothing: it says
  # whether the flags are necessary.
  remap_needed="$UNKNOWN"
  if install_tree "$plain/$COMPONENT" "$NO_REMAP"; then
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

  # 6. Two packs of the first tree are equal, byte for byte.
  read_members "$remapped/$COMPONENT" ||
    die "the tree holds a thing that is no regular file directly in bin/"
  pack "$remapped" "$pack_one/$name" || die "the first pack failed"
  pack "$remapped" "$pack_two/$name" || die "the second pack failed"
  cmp -s "$pack_one/$name" "$pack_two/$name" || die "two packs of one tree differ"
  members_hold "$pack_one/$name" ||
    die "a member of the archive is not a regular file $COMPONENT/bin/<program> with mode 0755"
  note "archive: $name, $(bytes_of "$pack_one/$name") bytes, and two packs are equal"

  line="$(cd "$pack_one" && sha256sum -- "$name")" || die "sha256sum did not read the archive"
  {
    printf 'digest=%s\n' "$line"
    printf 'archive=%s\n' "$pack_one/$name"
    printf 'toolchain=%s\n' "$TOOLCHAIN"
    printf 'remap_needed=%s\n' "$remap_needed"
  } >> "$GITHUB_OUTPUT"
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
# result that the probe did not expect. The text goes to the log of the job
# and never to the report. The step fails nothing.
journal() {
  say "the journal since the start of that case:"
  sudo journalctl --no-pager --since "@$STARTED" --lines 200 \
    --identifier systemd --identifier cosign || true
}

# verify_words BUNDLE ROOT WORKFLOW REF SHA BLOB: sets WORDS to the program
# words of one check. ROOT is a trusted root file, or empty: cosign then
# reads the trust root itself. REF is the ref of the workflow claim. The
# identity always names MAIN_REF, so the case with another ref measures that
# one claim.
verify_words() {
  local bundle="$1" root="$2" workflow="$3" ref="$4" sha="$5" blob="$6"

  WORDS=("$VERIFIER" verify-blob --bundle "$bundle")
  if [[ -n "$root" ]]; then
    WORDS+=(--trusted-root "$root")
  fi

  WORDS+=(
    --certificate-oidc-issuer "$TOKEN_ISSUER"
    --certificate-identity
    "$CODE_HOST/$GITHUB_REPOSITORY/.github/workflows/$workflow@$MAIN_REF"
    --certificate-github-workflow-repository "$GITHUB_REPOSITORY"
    --certificate-github-workflow-ref "$ref"
    --certificate-github-workflow-sha "$sha"
    "$blob"
  )
}

# must_fail NAME BUNDLE WORKFLOW REF SHA BLOB: one check that differs from
# the check that passed in one word. Status 0 fails the probe.
must_fail() {
  local name="$1"

  verify_words "$2" "$PASSING_ROOT" "$3" "$4" "$5" "$6"
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
    GITHUB_REPOSITORY GITHUB_SHA PROBE_TARGET PROBE_SIGNED COSIGN

  local name archive bundle copy changed report roots root_file root_count
  local two_children systemd_version cosign_version words

  name="$(archive_name)" || exit 1
  archive="$PROBE_SIGNED/$name"
  bundle="$archive.sigstore.json"
  copy="$COPY_DIR/$name"
  changed="$copy.changed"
  report="$RUNNER_TEMP/probe-report.json"

  [[ -f "$archive" ]] || die "the job sign gave no file $name"
  [[ -f "$bundle" ]] || die "the job sign gave no file $name.sigstore.json"
  [[ -x "$COSIGN" ]] || die "COSIGN names no program"
  [[ "$GITHUB_SHA" =~ ^[0-9a-f]{40}$ ]] || die "GITHUB_SHA is not a commit id"
  [[ "$GITHUB_REPOSITORY" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]] ||
    die "GITHUB_REPOSITORY is not an owner and a repository"

  # 1. Root owns the verifier and the copy of the two files.
  sudo install -o root -g root -m 0755 -- "$COSIGN" "$VERIFIER" ||
    die "cannot install the verifier"
  sudo install -d -o root -g root -m 0755 -- "$COPY_DIR" || die "cannot make $COPY_DIR"
  sudo install -o root -g root -m 0644 -- "$archive" "$copy" || die "cannot copy the archive"
  sudo install -o root -g root -m 0644 -- "$bundle" "$copy.sigstore.json" ||
    die "cannot copy the bundle"

  # From here each failure counts, and the report is written.
  printf 'report=%s\n' "$report" >> "$GITHUB_OUTPUT"

  # 2. The words that start each child are RUN, above.

  text target "$PROBE_TARGET"
  text toolchain "${PROBE_TOOLCHAIN:-$UNKNOWN}"
  text remap_needed "${PROBE_REMAP_NEEDED:-$UNKNOWN}"

  # 3. One child: cosign reads the trust root from the network and checks
  # the signature.
  verify_words "$copy.sigstore.json" "" "$WORKFLOW_FILE" "$MAIN_REF" "$GITHUB_SHA" "$copy"
  run_case one_child sudo "${RUN[@]}" -- "${WORDS[@]}"
  if [[ "$STATUS" -eq 0 ]]; then
    pass "one_child"
    PASSING=("${WORDS[@]}")
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
  if [[ -n "$root_file" ]]; then
    verify_words "$copy.sigstore.json" "$root_file" "$WORKFLOW_FILE" "$MAIN_REF" \
      "$GITHUB_SHA" "$copy"
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
  else
    no_case two_children
  fi
  text two_children "$two_children"

  if [[ "${#PASSING[@]}" -gt 0 ]]; then
    # 6. Four checks that must fail. Each one differs from the check that
    # passed in one word.
    must_fail zero_sha "$copy.sigstore.json" "$WORKFLOW_FILE" "$MAIN_REF" "$ZERO_SHA" "$copy"
    must_fail other_ref "$copy.sigstore.json" "$WORKFLOW_FILE" "$OTHER_REF" "$GITHUB_SHA" "$copy"
    must_fail other_workflow "$copy.sigstore.json" "$OTHER_WORKFLOW" "$MAIN_REF" \
      "$GITHUB_SHA" "$copy"
    if change_byte "$archive" "$changed"; then
      must_fail changed_byte "$copy.sigstore.json" "$WORKFLOW_FILE" "$MAIN_REF" \
        "$GITHUB_SHA" "$changed"
    else
      fail "changed_byte: cannot make a copy of the archive with one changed byte"
      no_case changed_byte
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
