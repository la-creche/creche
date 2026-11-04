#!/usr/bin/env bash
# SPIKE (decision CH51, Hyperion check): makes the scratch dir ready.
#   /home/david/c3-spike/setup.sh
# Runs as david, with no sudo (CH36 G31). It writes only under
# /home/david/c3-spike, and starts no unit and no VM.
#   1. It checks the user, the dir, the kernel, /dev/kvm and the tools.
#   2. It fetches CH v54.0, ch-remote and the guest kernel with the pins of
#      bin/vm-smoke-setup.sh, each checked by its sha256.
#   3. It fetches busybox-static with `apt-get download`, as the job kvm
#      does, and builds the same initramfs.
#   4. It makes 64 hard links bin/ch-slot/<k>/cloud-hypervisor of CH for
#      mechanism H. It makes the scratch run roots run/ (H) and runx/ (X),
#      each with slot/<k>/ dirs and the vm/<id> symlinks, and the scratch
#      state store. Mechanism X needs no link: it runs bin/cloud-hypervisor.
#   5. It checks c3spike-slots.profile against the generator, compiles it
#      without loading it (apparmor_parser -Q), and prints David's command.
set -euo pipefail

main() {
  # shellcheck source=lib.sh
  source "$(dirname "$0")/lib.sh"
  guard
  for sub in bin dl run runx state results tmp; do
    [[ ! -e "$K/$sub" ]] || die "$K/$sub exists: run cleanup.sh, copy the kit again, then setup.sh"
  done

  say "check the host"
  # shellcheck disable=SC1091 # /etc/os-release is on the host only.
  say "kernel $(uname -r), $(. /etc/os-release && echo "$PRETTY_NAME")"
  [[ "$(cat /sys/module/apparmor/parameters/enabled)" == Y ]] || die "AppArmor is off"
  [[ -r /dev/kvm && -w /dev/kvm ]] || die "/dev/kvm is not open to $KIT_USER"
  local tool
  for tool in curl sha256sum python3 setpriv aa-exec apparmor_parser apt-get dpkg-deb cpio stat; do
    command -v "$tool" > /dev/null || die "no $tool on this host"
  done
  say "apparmor_parser $(apparmor_parser --version | head -n 1)"
  local knob
  for knob in apparmor_restrict_unprivileged_unconfined apparmor_restrict_unprivileged_userns; do
    [[ -r /proc/sys/kernel/$knob ]] && say "kernel.$knob = $(cat "/proc/sys/kernel/$knob")"
  done
  if loaded_from_policy_dir | grep -q .; then
    die "creche-c3spike profiles are loaded already: David unloads them first"
  fi

  mkdir -m 0700 "$K/tmp" "$K/results"
  mkdir -p "$K/dl" "$BIN" "$STORE"

  say "fetch CH v54.0, ch-remote and the guest kernel"
  (
    cd "$K/dl"
    curl -fsSL -o cloud-hypervisor "$CH_URL/cloud-hypervisor-static"
    curl -fsSL -o ch-remote "$CH_URL/ch-remote-static"
    curl -fsSL -o vmlinux "$KERNEL_URL/vmlinux-x86_64"
    printf '%s  %s\n' "$CH_SHA" cloud-hypervisor "$CH_REMOTE_SHA" ch-remote \
      "$VMLINUX_SHA" vmlinux | sha256sum --check --strict
  )
  install -m 0755 "$K/dl/cloud-hypervisor" "$BIN/cloud-hypervisor"
  install -m 0755 "$K/dl/ch-remote" "$BIN/ch-remote"

  say "fetch busybox-static and build the initramfs"
  local work=$K/dl/busybox
  mkdir -p "$work/root/bin" "$work/root/proc"
  (cd "$work" && apt-get download busybox-static 2>&1 | tail -n 1)
  dpkg-deb -x "$work"/busybox-static_*.deb "$work/deb"
  local busybox
  busybox=$(find "$work/deb" -type f -name busybox | head -n 1)
  [[ -n "$busybox" ]] || die "busybox-static holds no busybox program"
  install -m 0755 "$busybox" "$BB"
  say "busybox $(basename "$work"/busybox-static_*.deb), sha256 $(sha256sum "$BB" | cut -c1-64)"
  local program
  for program in "$BB" "$BIN/cloud-hypervisor" "$BIN/ch-remote"; do
    # Not ldd: it runs a static-pie program to trace it.
    python3 -I "$K/sockprobe.py" static "$program" ||
      die "$program is not static: the profile maps no library"
  done
  cp "$BB" "$work/root/bin/busybox"
  printf '%s\n' '#!/bin/busybox sh' '/bin/busybox --install -s /bin' \
    'mount -t proc proc /proc' "echo $MARKER" \
    'while :; do echo tick; sleep 1; done' > "$work/root/init"
  chmod 0755 "$work/root/init"
  (cd "$work/root" && find . | cpio -o -H newc --quiet) > "$K/dl/initramfs.cpio"

  say "fill the scratch state store"
  # The store names each file by its SHA-256 (fact 56). The profile
  # matches `/**/store/*` only, so fixed names stand in, as in the kvm test.
  cp "$K/dl/vmlinux" "$KERNEL"
  cp "$K/dl/initramfs.cpio" "$INITRAMFS"

  say "make $SLOTS hard links of CH"
  local k
  for ((k = 0; k < SLOTS; k++)); do
    mkdir -p "$BIN/ch-slot/$k"
    ln "$BIN/cloud-hypervisor" "$BIN/ch-slot/$k/cloud-hypervisor"
  done
  local links inode
  links=$(stat -c %h "$BIN/cloud-hypervisor")
  [[ "$links" == $((SLOTS + 1)) ]] || die "CH has $links names, not $((SLOTS + 1))"
  inode=$(stat -c %i "$BIN/cloud-hypervisor")
  for ((k = 0; k < SLOTS; k++)); do
    [[ "$(stat -c %i "$BIN/ch-slot/$k/cloud-hypervisor")" == "$inode" ]] ||
      die "slot $k is not a link of CH"
  done
  say "inode $inode has $links names"

  say "make the scratch run roots of H and X"
  # Slots 0 and 1 hold the booted VMs. Slot 2 holds the probe CH, slot 3
  # the cross-slot targets, slot 4 the wrong-slot bind of T2, and the last
  # slot the far attach of T6. Each vm/<id> is a symlink to ../slot/<k>.
  # Both roots have the same layout. Only the X profiles name runx/.
  local root pair
  for root in "$RUN" "$RUNX"; do
    mkdir -m 0700 "$root" "$root/slot" "$root/vm"
    for pair in vm-a:0 vm-b:1 vm-p:2 vm-q:3 vm-c:4 vm-z:$((SLOTS - 1)); do
      mkdir -m 0700 "$root/slot/${pair#*:}"
      ln -s "../slot/${pair#*:}" "$root/vm/${pair%%:*}"
    done
  done
  find "$RUN/vm" "$RUNX/vm" -mindepth 1 -printf '%p -> %l\n'

  say "check the profile file and compile it without loading it"
  if [[ "${C3_SPIKE_AUDIT:-0}" == 1 ]]; then
    # CI diagnosis: the file has each deny rule marked audit (c3-slot-spike.yml).
    python3 -I "$K/gen-profiles.py" --slots "$SLOTS" --root "$K" |
      sed 's/^  deny /  audit deny /' | cmp - "$PROFILE_FILE"
    say "$PROFILE_FILE equals the generator output with audit deny rules"
  else
    python3 -I "$K/gen-profiles.py" --slots "$SLOTS" --root "$K" --check "$PROFILE_FILE"
  fi
  local start end out
  start=$(date +%s%N)
  if out=$(apparmor_parser -Q -K -o "$K/tmp/c3spike-slots.bin" "$PROFILE_FILE" 2>&1); then
    end=$(date +%s%N)
    say "T6 data: compiled $PROFILES profiles in $(((end - start) / 1000000)) ms, no load"
    echo "compile_ms=$(((end - start) / 1000000)) profiles=$PROFILES" > "$RESULTS/compile.txt"
  elif grep -q 'root privileges' <<< "$out"; then
    say "T6 data: apparmor_parser refuses -Q without root, so no compile time"
    echo "compile_ms=none" > "$RESULTS/compile.txt"
  else
    printf '%s\n' "$out" >&2
    die "the profile file does not compile on this host"
  fi

  say "ready. David runs this now:"
  printf '\n    sudo apparmor_parser -r %s\n\n' "$PROFILE_FILE"
  say "then run $K/probes.sh"
}

main "$@"
exit
