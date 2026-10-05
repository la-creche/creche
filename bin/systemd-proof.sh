#!/usr/bin/env bash
# CI: the proof of the restart rule of the daemon units (rust/AGENTS.md, "The
# config of a process"). The `systemd-proof` job of gate.yml and of
# release.yml runs it, against the systemd of the runner.
#   bin/systemd-proof.sh                      the proof
#   bin/systemd-proof.sh --unchanged FROM TO  exit 0 only when FROM..TO changes
#                                             no file of the proof. The scope
#                                             of each workflow asks this
#
# THE RESTART RULE. A daemon that refuses its start exits with 78. A unit
# with `Restart=always` stays stopped after that exit only when it holds
# `RestartPreventExitStatus=78`, and systemd reads that line for the main
# process alone. The proof starts three transient units. Each one holds
# `Restart=always`, `RestartSec=1`, `StartLimitIntervalSec=0` and
# `RestartPreventExitStatus=78`:
#   case 1  the main process exits 78          the unit is `failed`, and
#                                              NRestarts is 0
#   case 2  the main process exits 1           NRestarts is above 0
#   case 3  an `ExecStartPre=` process exits   NRestarts is above 0
#           78, before a main process that
#           sleeps
# Case 2 and case 3 also prove the measure. They show that on this machine
# a restart reaches NRestarts in the time that the unit of case 1 had.
#
# THE UNIT FILES. `systemd-analyze verify` then reads each unit file under
# systemd/. A runner holds no component tree, so the tool refuses a unit
# whose program is absent. For such a unit the proof is the syntax only: the
# tool prints no other line. One line of the proof says so.
#
# Needs systemd as process 1 and sudo with no password. A machine without
# one of the two fails the proof: nothing here stands in for systemd.
# The script names no unit of a deployment. It makes its own three units and
# removes them at its end, also after a check that failed.
set -euo pipefail
cd "$(dirname -- "${BASH_SOURCE[0]}")/.."

#: The directory of the unit files.
UNIT_DIR="systemd"

#: This file, as git names it.
SELF="bin/systemd-proof.sh"

#: The flag of the scope question.
UNCHANGED="--unchanged"

#: The exit status of a daemon that refuses its start: `EX_CONFIG` of
#: rust/crates/creche-contracts/src/config.rs.
EX_CONFIG=78

#: The line that keeps a unit stopped after that status: `NO_RESTART_LINE`
#: of the same Rust file. bin/tests/test_systemd_proof.py holds this line and
#: the status equal to the two Rust constants.
NO_RESTART_LINE="RestartPreventExitStatus=$EX_CONFIG"

#: What each test unit holds. Each daemon unit holds the first line and the
#: third line today (bin/tests/test_rust_config_units.py). The delay of a
#: restart is one second, so a restart shows soon.
RESTART_RULE=(
  --property=Restart=always
  --property=RestartSec=1
  --property=StartLimitIntervalSec=0
  "--property=$NO_RESTART_LINE"
)

#: The three test units, one for each case. The names are fixed, and no
#: deployment has a unit with one of them.
MAIN_REFUSES="creche-proof-main-78.service"
MAIN_FAILS="creche-proof-main-1.service"
CHECK_REFUSES="creche-proof-pre-78.service"
TEST_UNITS=("$MAIN_REFUSES" "$MAIN_FAILS" "$CHECK_REFUSES")

#: The seconds that each unit gets before the first read: five times the
#: delay of a restart.
WATCH_SECONDS=5

#: How many times the proof reads a unit that must start again, one second
#: apart. A loaded runner is slow. A longer wait can only show a restart
#: that occurred, so it cannot make a wrong proof pass.
RESTART_READS=60

#: How many times the proof reads a unit that it removed, one second apart.
REMOVE_READS=10

#: What process 1 must be.
INIT="systemd"

#: NRestarts as systemd prints it: a count with no sign and no leading zero.
COUNT='^(0|[1-9][0-9]*)$'

#: The end of the line that `systemd-analyze verify` prints for a program
#: that the machine does not hold.
NOT_THERE=" is not executable: No such file or directory"

usage() {
  echo "usage: $SELF [$UNCHANGED FROM TO]" >&2
  exit 2
}

# proof_path PATH: whether PATH is a file of the proof: a path under
# systemd/, this script, and the CI files that hold the job and ask this
# rule. A change to one of them is proven only by a run on a runner. git
# writes a path with an unusual byte inside double quotes, and that one
# counts too.
proof_path() {
  case "$1" in
    "$UNIT_DIR"/* | \""$UNIT_DIR"/* | "$SELF" | \
      .github/workflows/gate.yml | .github/workflows/release.yml | \
      .github/actions/scope/*)
      return 0
      ;;
  esac

  return 1
}

# unchanged FROM TO: whether FROM..TO changes no file of the proof. A change
# that git cannot read is not unchanged: the proof is the safe answer.
unchanged() {
  local paths path

  if ! paths="$(git -c core.quotePath=false diff --name-only --no-renames \
    "$1" "$2" 2>/dev/null)"; then
    return 1
  fi

  while IFS= read -r path; do
    if proof_path "$path"; then
      return 1
    fi
  done <<< "$paths"

  return 0
}

# runner_ready: fails, with one line, on a machine that cannot run the
# proof. It prints the version of systemd, so the log says which one proved
# the rule.
runner_ready() {
  local tool first version

  for tool in sudo ps sleep systemctl systemd-run systemd-analyze; do
    if ! command -v "$tool" >/dev/null; then
      echo "systemd-proof: $tool not on PATH: the proof needs Linux with systemd" >&2
      return 1
    fi
  done

  first="$(ps -p 1 -o comm= 2>/dev/null || true)"
  first="${first//[[:space:]]/}"
  if [[ "$first" != "$INIT" ]]; then
    echo "systemd-proof: process 1 is '$first', not $INIT: no proof on this machine" >&2
    return 1
  fi

  if ! sudo -n true 2>/dev/null; then
    echo "systemd-proof: sudo asks for a password: the proof starts its units as root" >&2
    return 1
  fi

  version="$(systemctl --version)" || return 1
  echo "systemd-proof: ${version%%$'\n'*}"
}

# start_unit UNIT ARG...: starts one transient unit with the restart rule.
# ARG... is each further option of systemd-run, then the command of the main
# process. `--no-block`: the call does not wait for the start, so a start
# that fails on purpose does not fail the call.
start_unit() {
  local unit="$1"
  shift

  sudo -n systemd-run --no-block --unit="$unit" "${RESTART_RULE[@]}" "$@"
}

# remove_units: stops each test unit and clears its failed state. systemd
# then drops a transient unit and deletes its file. A unit that systemd does
# not hold is no error: the exit of the script calls this again.
remove_units() {
  local unit

  for unit in "${TEST_UNITS[@]}"; do
    sudo -n systemctl stop "$unit" >/dev/null 2>&1 || true
    sudo -n systemctl reset-failed "$unit" >/dev/null 2>&1 || true
  done
}

# unit_value UNIT PROPERTY: one property of a unit, as systemd holds it now.
unit_value() {
  systemctl show --property="$2" --value "$1"
}

# read_restarts UNIT: sets RESTARTS to NRestarts of the unit. A value that
# is no count fails: a compare must not read such a value as zero.
read_restarts() {
  RESTARTS="$(unit_value "$1" NRestarts)" || return 1

  if [[ ! "$RESTARTS" =~ $COUNT ]]; then
    echo "systemd-proof: $1: NRestarts reads '$RESTARTS', which is no count" >&2
    return 1
  fi
}

# await_restart UNIT: reads NRestarts of the unit until it is above 0, at
# most RESTART_READS times. RESTARTS holds the last read.
await_restart() {
  local reads=1

  read_restarts "$1" || return 1
  while [[ "$RESTARTS" == 0 && "$reads" -lt "$RESTART_READS" ]]; do
    sleep 1
    reads=$((reads + 1))
    read_restarts "$1" || return 1
  done
}

# restart_rule_proven: the three cases. It prints one line for each case
# first, so the log holds the three results also when a case fails.
restart_rule_proven() {
  local state sub status stopped failed checked wrong=0

  start_unit "$MAIN_REFUSES" /bin/sh -c "exit $EX_CONFIG"
  start_unit "$MAIN_FAILS" /bin/sh -c "exit 1"
  # The main process of case 3 never starts. If it did, it would sleep, and
  # NRestarts would stay 0.
  start_unit "$CHECK_REFUSES" \
    "--property=ExecStartPre=/bin/sh -c \"exit $EX_CONFIG\"" /bin/sleep 600

  sleep "$WATCH_SECONDS"

  await_restart "$MAIN_FAILS" || return 1
  failed="$RESTARTS"
  await_restart "$CHECK_REFUSES" || return 1
  checked="$RESTARTS"

  # Case 1 is read last. Its unit then had each second that the two other
  # units needed to show a restart.
  state="$(unit_value "$MAIN_REFUSES" ActiveState)" || return 1
  sub="$(unit_value "$MAIN_REFUSES" SubState)" || return 1
  status="$(unit_value "$MAIN_REFUSES" ExecMainStatus)" || return 1
  read_restarts "$MAIN_REFUSES" || return 1
  stopped="$RESTARTS"

  echo "systemd-proof: case 1: the main process exits $EX_CONFIG:" \
    "ActiveState=$state SubState=$sub ExecMainStatus=$status NRestarts=$stopped"
  echo "systemd-proof: case 2: the main process exits 1: NRestarts=$failed"
  echo "systemd-proof: case 3: an ExecStartPre= process exits $EX_CONFIG: NRestarts=$checked"

  if [[ "$state" != failed || "$sub" != failed || "$status" != "$EX_CONFIG" ||
    "$stopped" != 0 ]]; then
    echo "systemd-proof: case 1 FAILED: the unit must be failed after the status" \
      "$EX_CONFIG, with NRestarts=0" >&2
    wrong=$((wrong + 1))
  fi

  if [[ "$failed" == 0 ]]; then
    echo "systemd-proof: case 2 FAILED: systemd must start the unit again" >&2
    wrong=$((wrong + 1))
  fi

  if [[ "$checked" == 0 ]]; then
    echo "systemd-proof: case 3 FAILED: systemd must start the unit again" >&2
    wrong=$((wrong + 1))
  fi

  [[ "$wrong" -eq 0 ]]
}

# units_gone: fails when systemd still holds a test unit after the removal,
# with one line for each such unit.
units_gone() {
  local unit load reads left=0

  for unit in "${TEST_UNITS[@]}"; do
    reads=1
    load="$(unit_value "$unit" LoadState)" || return 1
    while [[ "$load" != not-found && "$reads" -lt "$REMOVE_READS" ]]; do
      sleep 1
      reads=$((reads + 1))
      load="$(unit_value "$unit" LoadState)" || return 1
    done

    if [[ "$load" != not-found ]]; then
      echo "systemd-proof: $unit: LoadState=$load after the removal" >&2
      left=$((left + 1))
    fi
  done

  [[ "$left" -eq 0 ]]
}

# file_verified FILE: one unit file through `systemd-analyze verify`. FILE is
# an absolute path.
#
# `--recursive-errors=no`: the tool loads no other unit, and it fails for a
# warning on a line of this file. `--man=no`: a manual page that the runner
# lacks says nothing about the file. No sudo: the tool starts nothing. It
# reads each file as a system unit, a user unit too, because the parser is
# the same.
#
# Each line that the tool prints must be the line for a program that the
# machine does not hold. Such a line names this unit, or an instance of it
# when the file is a template. With one line of another kind the unit is
# refused. With no line, the exit status of the tool decides.
file_verified() {
  local file="$1" name stem kind out line rest program status=0 lacks="" other=0

  name="${file##*/}"
  stem="${name%%@*}"
  kind="${name##*.}"
  out="$(systemd-analyze verify --man=no --recursive-errors=no "$file" 2>&1)" || status=$?

  while IFS= read -r line; do
    case "$line" in
      "")
        continue
        ;;
      "$name: Command "*"$NOT_THERE")
        rest="${line#"$name: Command "}"
        ;;
      "$stem@"*".$kind: Command "*"$NOT_THERE")
        rest="${line#*".$kind: Command "}"
        ;;
      *)
        other=$((other + 1))
        continue
        ;;
    esac

    program="${rest%"$NOT_THERE"}"
    case ", $lacks, " in
      *", $program, "*) ;;
      *) lacks="${lacks:+$lacks, }$program" ;;
    esac
  done <<< "$out"

  if [[ "$other" -gt 0 || ("$status" -ne 0 && -z "$lacks") ]]; then
    echo "systemd-proof: $name: REFUSED, with the status $status of the tool:" >&2
    printf '%s\n' "$out" >&2
    return 1
  fi

  if [[ -n "$lacks" ]]; then
    echo "systemd-proof: $name: the syntax only, because the machine lacks $lacks"
    return 0
  fi

  echo "systemd-proof: $name: verified"
}

# units_verified: each file under systemd/ that is no Markdown file. It
# reads each file also after one that the tool refused, so one run shows
# each refused unit. A run that read no file checked nothing, and fails too.
units_verified() {
  local file read=0 refused=0

  for file in "$UNIT_DIR"/*; do
    if [[ ! -f "$file" || "$file" == *.md ]]; then
      continue
    fi

    read=$((read + 1))
    if ! file_verified "$PWD/$file"; then
      refused=$((refused + 1))
    fi
  done

  if [[ "$read" -eq 0 ]]; then
    echo "systemd-proof: no unit file under $UNIT_DIR/" >&2
    return 1
  fi

  echo "systemd-proof: $read unit files read, $refused refused"
  [[ "$refused" -eq 0 ]]
}

MODE="${1:-}"
case "$MODE" in
  "")
    [[ "$#" -eq 0 ]] || usage
    ;;
  "$UNCHANGED")
    [[ "$#" -eq 3 ]] || usage
    if unchanged "$2" "$3"; then
      exit 0
    fi

    exit 1
    ;;
  *)
    usage
    ;;
esac

runner_ready

# From here the script holds units. The exit removes them, also the exit of
# a check that failed. The first call removes what an earlier run left.
trap remove_units EXIT
remove_units

restart_rule_proven
remove_units
units_gone
units_verified

echo "systemd-proof: PASS"
