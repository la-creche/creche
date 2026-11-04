#!/usr/bin/env bash
# SPIKE (decision CH51, Hyperion check): the probes T0 to T6 of the slot
# pool on Hyperion's own kernel, once for each mechanism.
#   /home/david/c3-spike/probes.sh
# Runs as david, with no sudo, after setup.sh and David's sudo load. It
# writes only under /home/david/c3-spike and starts no unit.
#
# The two mechanisms run one after the other, with the same tests:
#   H  hard-link attach. Each CH runs through `setpriv --no-new-privs` at
#      the slot link bin/ch-slot/<k>/cloud-hypervisor, as in the job kvm,
#      so the profile creche-c3spike-slot-<k> attaches by the exec path
#      under no_new_privs. Run root run/.
#   X  change_onexec. Each CH runs through xlaunch.py, which writes
#      `exec creche-c3spike-xslot-<k>` to the exec attr, sets
#      no_new_privs, and execs the one installed bin/cloud-hypervisor. The
#      X profiles have no attachment. Run root runx/.
#
# Slots, in each run root: VM A boots in slot 0 (vm/vm-a), VM B in slot 1
# (vm/vm-b). The probe CH runs in slot 2 (vm/vm-p) and targets slot 3
# (vm/vm-q). Slot 4 (vm/vm-c) takes the wrong-slot bind. Slot 63
# (vm/vm-z) takes the far attach.
#   T0  the vmm.sock stand-in in the run root is refused (CH20, CH24).
#   T1  H: each hard link of one inode gets its own label. X: one path
#       gets one label per onexec write, and a write of an unloaded name
#       starts no CH (fail closed).
#   T2  a CH whose run dir is the vm/<id> symlink binds its sockets in its
#       slot, boots and serves its API.
#   T3  a cross-slot connect to vsock.sock_<port> is refused (attack 1).
#   T4  cross-slot connects to api.sock and vsock.sock are refused, for
#       test listeners and for a live VM (attacks 2 and 3). The live
#       control runs the same probe under slot 0, which owns VM A.
# Each control also needs eacces=no and the label of its slot, so a stray
# EACCES in the CH log cannot pass for a refusal.
#   T5  a CH label cannot signal another slot's CH (attack 4).
#   T6  all 128 profiles load in enforce mode, and the last slot attaches.
# Each test prints one PASS or FAIL line, named H-T<n> or X-T<n>, with its
# control result next to it. The exit is 1 if any test fails. The logs
# stay in results/ until cleanup.sh.
set -euo pipefail

FAILS=0
PASSES=0
A_PID=
B_PID=
# The mechanism of the running suite: H or X. suite() sets it, with its
# label prefix LP and its run root R.
M=
LP=
R=
LAUNCHER=()

report() {
  local verdict=$1 name=$2 result=$3 control=$4
  if [[ "$verdict" == PASS ]]; then
    PASSES=$((PASSES + 1))
  else
    FAILS=$((FAILS + 1))
  fi
  printf '%s %s-%s: %s | control: %s\n' "$verdict" "$M" "$name" "$result" "$control" | tee -a "$RESULTS/probes.txt"
}

# The word of key in a sockprobe line: `word connected "connected=no ..."`.
word() {
  local key=$1 line=$2 part
  for part in $line; do
    [[ "$part" == "$key="* ]] && echo "${part#*=}" && return 0
  done
  echo missing
}

# The CH path that a slot runs: its link (H) or the one installed CH (X).
ch_exe() {
  if [[ "$M" == H ]]; then
    echo "$BIN/ch-slot/$1/cloud-hypervisor"
  else
    echo "$BIN/cloud-hypervisor"
  fi
}

# Sets LAUNCHER to the words that run a program under the label of a
# slot, with no_new_privs. H: setpriv, and the program must be the slot
# link. X: xlaunch.py. boot runs the words as a plain command, not a
# function, so $! is the pid that execs CH.
set_launcher() {
  if [[ "$M" == H ]]; then
    LAUNCHER=(setpriv --no-new-privs)
  else
    LAUNCHER=(python3 -I "$XLAUNCH" "$X_PREFIX$1")
  fi
}

# Runs busybox under the label of a slot. H: aa-exec, because no slot
# profile attaches to busybox. X: xlaunch.py, the mechanism itself.
as_label() {
  local slot=$1
  shift
  if [[ "$M" == H ]]; then
    aa-exec -p "$PREFIX$slot" -- "$BB" "$@"
  else
    python3 -I "$XLAUNCH" "$X_PREFIX$slot" "$BB" "$@"
  fi
}

# The sockprobe flags that select the launcher of the suite.
launch_flags() {
  [[ "$M" == H ]] || printf '%s\n' --onexec "$X_PREFIX$1"
}

probe_connect() {
  local slot=$1 target=$2 listen=$3 log=$4 flags
  flags=$(launch_flags "$slot")
  # shellcheck disable=SC2206 # flags holds two plain words or none.
  local args=(connect --exe "$(ch_exe "$slot")" --kernel "$KERNEL"
    --initramfs "$INITRAMFS" --target "$target" --log "$LOGS/$M-$log.log" $flags)
  [[ "$listen" == listen ]] && args+=(--listen)
  python3 -I "$K/sockprobe.py" "${args[@]}"
}

# The idle CH of a slot, under the launcher of the suite.
probe_idle() {
  local slot=$1 api=$2 log=$3 flags
  flags=$(launch_flags "$slot")
  # shellcheck disable=SC2086 # flags holds two plain words or none.
  python3 -I "$K/sockprobe.py" idle --exe "$(ch_exe "$slot")" --api "$api" --log "$LOGS/$M-$log.log" $flags
}

# The idle CH at a path, with setpriv only and no onexec write.
probe_plain() {
  local exe=$1 api=$2 log=$3
  python3 -I "$K/sockprobe.py" idle --exe "$exe" --api "$api" --log "$LOGS/$M-$log.log"
}

# Whether a connect line says: refused with EACCES and no connect.
refused() {
  [[ "$(word connected "$1")" == no && "$(word eacces "$1")" == yes ]]
}

# Whether a same-slot control line passes: the probe ran under the label
# of its slot and logged no EACCES. With a listener, the connect must come.
control_ok() {
  local line=$1 slot=$2 mode=$3
  [[ "$(word label "$line")" == "${LP}${slot}_(enforce)" && "$(word eacces "$line")" == no ]] || return 1
  [[ "$mode" != listen || "$(word connected "$line")" == yes ]]
}

boot() {
  local id=$1 slot=$2 cid=$3 dir=$R/vm/$1 exe
  exe=$(ch_exe "$slot")
  set_launcher "$slot"
  "${LAUNCHER[@]}" "$exe" \
    --api-socket "path=$dir/api.sock" \
    --kernel "$KERNEL" --initramfs "$INITRAMFS" \
    --cmdline "console=ttyS0 rdinit=/init" \
    --cpus boot=1 --memory size=256M \
    --vsock "cid=$cid,socket=$dir/vsock.sock" \
    --serial "file=$dir/serial.log" --console off \
    < /dev/null > "$LOGS/$M-boot-$id.log" 2>&1 &
  BOOTED=$!
}

booted() {
  [[ -f "$1" ]] && tr -d '\r' < "$1" | grep -qx "$MARKER"
}

wait_boot() {
  local pid=$1 serial=$2 tries
  for ((tries = 0; tries < 1500; tries++)); do
    booted "$serial" && return 0
    alive "$pid" || return 1
    sleep 0.02
  done
  return 1
}

stop_all() {
  local pid
  for pid in $A_PID $B_PID; do
    alive "$pid" && kill -KILL "$pid" 2> /dev/null
    wait "$pid" 2> /dev/null || true
  done
  for pid in $(spike_pids); do
    kill -KILL "$pid" 2> /dev/null || true
  done
  A_PID=
  B_PID=
}

# T1 of H: labels by exec path.
t1_h() {
  local la=$1 lb=$2 ia ib ich ctl
  ia=$(stat -L -c %i "/proc/$A_PID/exe" 2> /dev/null || echo none)
  ib=$(stat -L -c %i "/proc/$B_PID/exe" 2> /dev/null || echo none)
  ich=$(stat -c %i "$BIN/cloud-hypervisor")
  ctl=$(probe_plain "$BIN/cloud-hypervisor" "$K/tmp/t1-control-api.sock" t1-control)
  if [[ "$la" == "${LP}0 (enforce)" && "$lb" == "${LP}1 (enforce)" &&
    "$ia" == "$ich" && "$ib" == "$ich" && "$(word label "$ctl")" == unconfined ]]; then
    report PASS T1 "one inode $ich, slot 0 is '$la', slot 1 is '$lb'" "base path runs $(word label "$ctl")"
  else
    report FAIL T1 "inodes $ia $ib $ich, slot 0 is '$la', slot 1 is '$lb'" "base path: $ctl"
  fi
}

# T1 of X: labels by onexec write, at one path. The controls: the same
# path with no write runs unconfined, so no X profile attaches to it. A
# write of a name that no profile has fails, and xlaunch.py then starts
# no CH: it exits 126 before the exec.
t1_x() {
  local la=$1 lb=$2 pa pb ctl knob out rc=0 unloaded=$X_PREFIX$SLOTS
  pa=$(readlink "/proc/$A_PID/exe" 2> /dev/null || echo none)
  pb=$(readlink "/proc/$B_PID/exe" 2> /dev/null || echo none)
  knob=$(cat /proc/sys/kernel/apparmor_restrict_unprivileged_unconfined 2> /dev/null || echo none)
  ctl=$(probe_plain "$BIN/cloud-hypervisor" "$K/tmp/t1-control-api.sock" t1-control)
  out=$(python3 -I "$XLAUNCH" "$unloaded" "$BIN/cloud-hypervisor" --version 2>&1) || rc=$?
  if [[ "$la" == "${LP}0 (enforce)" && "$lb" == "${LP}1 (enforce)" &&
    "$pa" == "$BIN/cloud-hypervisor" && "$pb" == "$BIN/cloud-hypervisor" &&
    "$(word label "$ctl")" == unconfined && "$rc" == 126 && "$out" == *ENOENT* ]]; then
    report PASS T1 "one path bin/cloud-hypervisor, slot 0 is '$la', slot 1 is '$lb', restrict_unprivileged_unconfined=$knob" \
      "the same path with no write runs $(word label "$ctl"). xlaunch.py to unloaded $unloaded: rc=$rc '$out', no CH"
  else
    report FAIL T1 "exe $pa $pb, slot 0 is '$la', slot 1 is '$lb', restrict_unprivileged_unconfined=$knob" \
      "no write: $ctl. xlaunch.py to unloaded $unloaded: rc=$rc '$out'"
  fi
}

# One mechanism: boot VMs A and B, then T1, T2, T3, T4, T0, T5 and T6.
suite() {
  M=$1
  if [[ "$M" == H ]]; then
    LP=$PREFIX
    R=$RUN
    say "mechanism H: hard links, labels $LP<k>, run root $R"
  else
    LP=$X_PREFIX
    R=$RUNX
    say "mechanism X: change_onexec by xlaunch.py, labels $LP<k>, run root $R"
  fi

  say "$M: boot VM A in slot 0 and VM B in slot 1"
  boot vm-a 0 3
  A_PID=$BOOTED
  boot vm-b 1 4
  B_PID=$BOOTED
  local a_ok=no b_ok=no
  wait_boot "$A_PID" "$R/vm/vm-a/serial.log" && a_ok=yes
  wait_boot "$B_PID" "$R/vm/vm-b/serial.log" && b_ok=yes
  say "$M: VM A booted=$a_ok pid=$A_PID, VM B booted=$b_ok pid=$B_PID"

  # T1: one label per slot.
  local la lb ctl
  la=$(label_of "$A_PID")
  lb=$(label_of "$B_PID")
  if [[ "$M" == H ]]; then
    t1_h "$la" "$lb"
  else
    t1_x "$la" "$lb"
  fi

  # T2: the symlinked run dir.
  local t2_ok=yes detail="" id slot pair
  for pair in vm-a:0 vm-b:1; do
    id=${pair%%:*}
    slot=${pair#*:}
    [[ "$(readlink "$R/vm/$id")" == "../slot/$slot" ]] || { t2_ok=no; detail+="$id is no symlink to slot $slot. "; }
    [[ -S "$R/slot/$slot/api.sock" ]] || { t2_ok=no; detail+="no api.sock in slot $slot. "; }
    [[ -S "$R/slot/$slot/vsock.sock" ]] || { t2_ok=no; detail+="no vsock.sock in slot $slot. "; }
    booted "$R/slot/$slot/serial.log" || { t2_ok=no; detail+="$id did not boot. "; }
    "$BIN/ch-remote" --api-socket "$R/vm/$id/api.sock" ping > "$LOGS/$M-t2-ping-$id.log" 2>&1 ||
      { t2_ok=no; detail+="$id ping failed. "; }
  done
  ctl=$(probe_idle 0 "$R/vm/vm-c/api.sock" t2-control)
  local slot4
  slot4=$(find "$R/slot/4" -mindepth 1 | wc -l)
  if [[ "$t2_ok" == yes && "$(word bound "$ctl")" == no && "$(word eacces "$ctl")" == yes && "$slot4" == 0 ]]; then
    report PASS T2 "VMs A and B boot, bind api.sock and vsock.sock in slot/0 and slot/1 through vm/<id>, and ping" \
      "slot 0 CH binding in slot/4 refused (bound=no eacces=yes)"
  else
    report FAIL T2 "${detail:-ok}" "slot 0 CH binding in slot/4: $ctl, $slot4 files in slot/4"
  fi

  # T3: cross-slot vsock.sock_<port>.
  local t3 t3c
  t3=$(probe_connect 2 "$R/vm/vm-q/vsock.sock_1100" listen t3)
  t3c=$(probe_connect 2 "$R/vm/vm-p/vsock.sock_1100" listen t3-control)
  if refused "$t3" && control_ok "$t3c" 2 listen; then
    report PASS T3 "slot 2 to slot/3/vsock.sock_1100 refused ($t3)" "slot 2 to slot/2/vsock.sock_1100 connected ($t3c)"
  else
    report FAIL T3 "slot 2 to slot/3/vsock.sock_1100: $t3" "slot 2 to slot/2/vsock.sock_1100: $t3c"
  fi

  # T4: cross-slot api.sock and vsock.sock, test listeners and live VM A.
  local t4_ok=yes t4_detail="" t4_ctl_ok=yes t4_ctl="" line target
  for target in vm-q/api.sock:listen vm-q/vsock.sock:listen vm-a/api.sock:live vm-a/vsock.sock:live; do
    line=$(probe_connect 2 "$R/vm/${target%%:*}" "${target#*:}" "t4-${target//\//-}")
    refused "$line" || t4_ok=no
    t4_detail+="${target%%:*} $(word connected "$line")/$(word eacces "$line"). "
  done
  "$BIN/ch-remote" --api-socket "$R/vm/vm-a/api.sock" ping > "$LOGS/$M-t4-ping-after.log" 2>&1 ||
    { t4_ok=no; t4_detail+="VM A no longer pings. "; }
  # The controls: slot 2 connects to its own listeners, and slot 0 to its
  # own live VM A. No listener sees a live connect, so connected=no tells
  # nothing there. The live control is that the same probe, at the same
  # path, logs no EACCES under the slot that owns it. Then the EACCES of a
  # cross-slot live case comes from the slot profile, not a stray denial.
  local path mode
  for target in 2:vm-p/api.sock:listen 2:vm-p/vsock.sock:listen 0:vm-a/api.sock:live 0:vm-a/vsock.sock:live; do
    slot=${target%%:*}
    path=${target#*:}
    mode=${path#*:}
    path=${path%%:*}
    line=$(probe_connect "$slot" "$R/vm/$path" "$mode" "t4-control-$slot-${path//\//-}")
    control_ok "$line" "$slot" "$mode" || t4_ctl_ok=no
    t4_ctl+="slot $slot $path $(word connected "$line")/$(word eacces "$line"). "
  done
  if [[ "$t4_ok" == yes && "$t4_ctl_ok" == yes ]]; then
    report PASS T4 "slot 2 refused (connected/eacces): $t4_detail" "(connected/eacces) $t4_ctl"
  else
    report FAIL T4 "slot 2 (connected/eacces): $t4_detail" "(connected/eacces) $t4_ctl"
  fi

  # T0: the vmm.sock stand-in in the run root. The control is T3's.
  local t0
  t0=$(probe_connect 2 "$R/vmm.sock" listen t0)
  if refused "$t0" && control_ok "$t3c" 2 listen; then
    report PASS T0 "slot 2 to ${R##*/}/vmm.sock refused ($t0)" "T3 control connected with eacces=no"
  else
    report FAIL T0 "slot 2 to ${R##*/}/vmm.sock: $t0" "T3 control: $t3c"
  fi

  # T5: signals between slots. VM B in slot 1 is the target.
  local t5_out t5_rc=0 b_after ctl_rc=0 b_end a_end
  if [[ "$AA_OK" != ok ]]; then
    report FAIL T5 "aa-exec cannot enter a slot label (see check.sh)" "not run"
  elif ! alive "$B_PID"; then
    report FAIL T5 "VM B is not running" "not run"
  else
    t5_out=$(as_label 0 kill -KILL "$B_PID" 2>&1) || t5_rc=$?
    sleep 0.2
    alive "$B_PID" && b_after=alive || b_after=dead
    as_label 1 kill -KILL "$B_PID" > "$LOGS/$M-t5-control.log" 2>&1 || ctl_rc=$?
    sleep 0.5
    alive "$B_PID" && b_end=alive || b_end=dead
    alive "$A_PID" && a_end=alive || a_end=dead
    if [[ "$t5_rc" != 0 && "$t5_out" == *"Permission denied"* && "$b_after" == alive &&
      "$ctl_rc" == 0 && "$b_end" == dead && "$a_end" == alive ]]; then
      report PASS T5 "slot 0 kill -KILL of slot 1 CH refused (rc=$t5_rc, B $b_after)" \
        "slot 1 kill -KILL of slot 1 CH worked (rc=$ctl_rc, B $b_end), VM A $a_end"
    else
      report FAIL T5 "slot 0 kill: rc=$t5_rc '$t5_out', B $b_after" "slot 1 kill: rc=$ctl_rc, B $b_end, VM A $a_end"
    fi
  fi

  # T6: all profiles in enforce mode, and the far slot attaches.
  local far last=$((SLOTS - 1))
  far=$(probe_idle "$last" "$R/vm/vm-z/api.sock" t6-far)
  if [[ "$AA_OK" == ok && "$(word bound "$far")" == yes &&
    "$(word label "$far")" == "${LP}${last}_(enforce)" && "$T6_CONTROL" == read ]]; then
    report PASS T6 "$PROFILES labels enforce, checked in $CHECK_MS ms, $COMPILE, slot $last attaches ($far)" \
      "unconfined busybox cat README.md: $T6_CONTROL"
  else
    report FAIL T6 "aa-exec=$AA_OK, $COMPILE, slot $last: $far" "unconfined busybox cat README.md: $T6_CONTROL"
  fi

  stop_all
}

main() {
  # shellcheck source=lib.sh
  source "$(dirname "$0")/lib.sh"
  guard
  [[ -d "$RUN/vm" && -d "$RUNX/vm" ]] || die "no scratch run roots: run setup.sh first"
  [[ -z "$(spike_pids)" ]] || die "spike processes run already: $(spike_pids | tr '\n' ' ')"

  LOGS=$RESULTS/logs
  rm -rf "$LOGS" "$RESULTS/probes.txt"
  mkdir -p "$LOGS"
  # A failed earlier run may leave sockets in the slot dirs.
  find "$RUN/slot" "$RUNX/slot" -mindepth 2 ! -type d -delete
  rm -f "$RUN/vmm.sock" "$RUNX/vmm.sock"

  local start end check_out
  start=$(date +%s%N)
  check_out=$("$K/check.sh") || {
    printf '%s\n' "$check_out"
    die "check.sh refused: the probes do not start"
  }
  end=$(date +%s%N)
  printf '%s\n' "$check_out"
  CHECK_MS=$(((end - start) / 1000000))
  AA_OK=$(grep -o 'aa-exec=[a-z]*' <<< "$check_out" | cut -d= -f2)
  COMPILE=$(cat "$RESULTS/compile.txt" 2> /dev/null || echo compile_ms=none)
  "$BB" cat "$K/README.md" > /dev/null 2>&1 && T6_CONTROL='read' || T6_CONTROL='refused'
  say "host: kernel $(uname -r), $(apparmor_parser --version | head -n 1)"

  trap stop_all EXIT
  suite H
  local h_pass=$PASSES h_fail=$FAILS
  suite X
  trap - EXIT
  say "H: $h_pass PASS, $h_fail FAIL. X: $((PASSES - h_pass)) PASS, $((FAILS - h_fail)) FAIL. Logs: $LOGS"
  say "next: David runs: sudo apparmor_parser -R $PROFILE_FILE"
  say "then run $K/cleanup.sh"
  ((FAILS == 0))
}

main "$@"
exit
