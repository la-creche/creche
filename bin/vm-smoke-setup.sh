#!/usr/bin/env bash
# CI: makes a GitHub runner ready for the kvm_ tests and fetches what they boot.
#   CRECHE_KVM_DIR=<dir> bin/vm-smoke-setup.sh
# The job `kvm` of gate.yml and of release.yml runs it. No hook and no unit
# runs it. It refuses to run outside GitHub Actions: it uses sudo, and CH36
# G20 allows sudo on GitHub's throwaway runners only, never on the host.
#
# The method is the one the C0 spike proved (design note fact 79):
#   1. /dev/kvm must exist. No KVM fails here, loudly (CH36 G19).
#   2. A udev rule with MODE 0666 opens /dev/kvm and /dev/vhost-vsock to the
#      runner user, who is not in the group kvm. `modprobe vhost_vsock`
#      makes the second node.
#   3. Linger starts the user manager, for a later `systemd-run --user`.
#   4. It fetches the upstream static CH v54.0 and ch-remote (fact 71) and
#      the CH guest kernel (fact 53), each checked by its sha256.
#   5. It builds a busybox initramfs from `apt-get download busybox-static`.
#      Its init writes the line CRECHE-KVM-BOOTED to the serial port, then
#      `tick` once a second.
#   6. It installs CH at /opt/components/vmm/bin/cloud-hypervisor and loads
#      the AppArmor profile of CH with the commands of design section 6.1
#      step 1 (decision CH8-1, gap G28). A profile that does not load, or
#      that is not in enforce mode, fails here. The copy in CRECHE_KVM_DIR
#      stays unconfined, so each kvm_ test picks its path.
#   7. SPIKE (decision CH51): it makes 64 hard links of the installed CH at
#      /opt/components/vmm/bin/ch-slot/<k>/cloud-hypervisor. It renders
#      the slot profiles creche-ch-slot-<k> with the example
#      ch_slot_profiles and loads them. A slot profile that does not load,
#      or that is not in enforce mode, fails here.
# Phase 4 replaces the upstream files with the CI-built CH, kernel and disks
# (CH36 G19).
#
# The files land in CRECHE_KVM_DIR, which is absent or empty:
#   cloud-hypervisor  ch-remote  vmlinux  initramfs.cpio  ch-slots.profile
# The kvm_ tests read the same variable.
#
# `set -euo pipefail`: the first fault stops the run.
set -euo pipefail
export LC_ALL=C

REPO=$(cd "$(dirname "$0")/.." && pwd)
INSTALLED_CH=/opt/components/vmm/bin/cloud-hypervisor
PROFILE=/etc/apparmor.d/opt.components.vmm.bin.cloud-hypervisor
# SPIKE (decision CH51): the slot pool. SLOTS is SLOTS of slot_profile.rs.
SLOTS=64
SLOT_ROOT=/opt/components/vmm/bin/ch-slot
SLOT_PROFILE=/etc/apparmor.d/opt.components.vmm.bin.ch-slot
CH_URL=https://github.com/cloud-hypervisor/cloud-hypervisor/releases/download/v54.0
KERNEL_URL=https://github.com/cloud-hypervisor/linux/releases/download/ch-release-v7.2.8-20261009

say() { printf 'vm-smoke-setup: %s\n' "$*"; }
die() {
  say "$*" >&2
  exit 1
}

[[ "${GITHUB_ACTIONS:-}" == true ]] || die "runs only on a GitHub runner (CH36 G20)"
dir=${CRECHE_KVM_DIR:?CRECHE_KVM_DIR names the output directory}
[[ "$dir" == /* ]] || die "CRECHE_KVM_DIR is not absolute: $dir"
[[ ! -e "$dir" ]] || [[ -z "$(ls -A "$dir")" ]] || die "$dir is not empty"
[[ -e /dev/kvm ]] || die "no /dev/kvm on this runner: the kvm_ tests cannot run"

say "open /dev/kvm and /dev/vhost-vsock to the runner user"
printf '%s\n' \
  'KERNEL=="kvm", GROUP="kvm", MODE="0666", OPTIONS+="static_node=kvm"' \
  'KERNEL=="vhost-vsock", GROUP="kvm", MODE="0666"' |
  sudo tee /etc/udev/rules.d/99-creche-kvm-smoke.rules > /dev/null
sudo udevadm control --reload-rules
sudo modprobe vhost_vsock
sudo udevadm trigger --name-match=kvm
sudo udevadm trigger --name-match=vhost-vsock
sudo udevadm settle
for node in /dev/kvm /dev/vhost-vsock; do
  [[ -r "$node" && -w "$node" ]] || die "$node is not open to $(id -un)"
done

say "start the user manager"
sudo loginctl enable-linger "$(id -un)"
bus="/run/user/$(id -u)/bus"
for _ in $(seq 1 50); do
  [[ -S "$bus" ]] && break
  sleep 0.1
done
[[ -S "$bus" ]] || die "no user manager: $bus is missing"

mkdir -p "$dir"
cd "$dir"

say "fetch CH v54.0, ch-remote and the guest kernel"
curl -fsSL -o cloud-hypervisor "$CH_URL/cloud-hypervisor-static"
curl -fsSL -o ch-remote "$CH_URL/ch-remote-static"
curl -fsSL -o vmlinux "$KERNEL_URL/vmlinux-x86_64"
sha256sum --check --strict <<'EOF'
4f4f98a3d661a40f3e5272627cc895a1b2e525f2f6bedf5f895f9cd21ba5c65b  cloud-hypervisor
acd5be240d5c73373effbe818908fc03e313ec8486237904bff11d93b71b1c83  ch-remote
edfeb8a8a6c04ff2979e9f1c700feaa29b8f564f952261834ef0b7d2e673a853  vmlinux
EOF
chmod 0755 cloud-hypervisor ch-remote

say "build the busybox initramfs"
sudo apt-get update -qq
work=$(mktemp -d "$dir/busybox.XXXXXX")
(cd "$work" && apt-get download busybox-static)
dpkg-deb -x "$work"/busybox-static_*.deb "$work/deb"
mkdir -p "$work/root/bin" "$work/root/proc"
# The package may put the program in bin/ or in usr/bin/.
busybox=$(find "$work/deb" -type f -name busybox | head -n 1)
[[ -n "$busybox" ]] || die "busybox-static holds no busybox program"
cp "$busybox" "$work/root/bin/busybox"
printf '%s\n' '#!/bin/busybox sh' '/bin/busybox --install -s /bin' \
  'mount -t proc proc /proc' 'echo CRECHE-KVM-BOOTED' \
  'while :; do echo tick; sleep 1; done' > "$work/root/init"
chmod 0755 "$work/root/init"
(cd "$work/root" && find . | cpio -o -H newc --quiet) > initramfs.cpio
rm -rf "$work"

say "load the AppArmor profile of CH"
[[ "$(cat /sys/module/apparmor/parameters/enabled)" == Y ]] || die "AppArmor is off on this runner"
sudo install -D -m 0755 cloud-hypervisor "$INSTALLED_CH"
sudo install -m 0644 "$REPO/rust/crates/vmm-confine/apparmor/cloud-hypervisor.profile" "$PROFILE"
sudo apparmor_parser -r "$PROFILE"
sudo grep -Fqx 'creche-cloud-hypervisor (enforce)' /sys/kernel/security/apparmor/profiles ||
  die "the profile creche-cloud-hypervisor is not in enforce mode"

# SPIKE (decision CH51, branch spike/c3-slot-pool, never merges): step 7.
say "make the CH slot pool and load its profiles"
(cd "$REPO/rust" && rustup toolchain install --no-self-update > /dev/null &&
  cargo run --locked -q -p creche-vmm-confine --example ch_slot_profiles -- set "$SLOTS") > ch-slots.profile
for ((k = 0; k < SLOTS; k++)); do
  sudo install -d -m 0755 "$SLOT_ROOT/$k"
  sudo ln "$INSTALLED_CH" "$SLOT_ROOT/$k/cloud-hypervisor"
done
sudo install -m 0644 ch-slots.profile "$SLOT_PROFILE"
sudo apparmor_parser -r "$SLOT_PROFILE"
loaded=$(sudo cat /sys/kernel/security/apparmor/profiles)
for ((k = 0; k < SLOTS; k++)); do
  grep -Fqx "creche-ch-slot-$k (enforce)" <<< "$loaded" ||
    die "the profile creche-ch-slot-$k is not in enforce mode"
done
# No step unloads the slot profiles, as no step unloads the base profile:
# the runner is a throwaway VM. An unload here would also run before the
# tests, because this script is the second step of the job.

say "ready in $dir"
