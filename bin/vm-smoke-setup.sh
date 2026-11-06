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
#      The script writes XDG_RUNTIME_DIR to $GITHUB_ENV, so the later steps
#      of the job reach the user manager, as the vmm unit does.
#   4. It fetches the upstream static CH v54.0 and ch-remote (fact 71) and
#      the CH guest kernel (fact 53), each checked by its sha256.
#   5. It builds a busybox initramfs from `apt-get download busybox-static`.
#      Its init writes the line CRECHE-KVM-BOOTED to the serial port, then
#      `tick` once a second.
#   6. It installs CH at /opt/components/vmm/bin/cloud-hypervisor and loads
#      the slot profiles of rust/crates/vmm-confine/apparmor/
#      cloud-hypervisor-slots.profile with the commands of design section
#      6.1 step 1 (decisions CH8-1, CH55, gap G28 of CH36). It fails unless
#      each creche-ch-slot-<k> label of the file is in enforce mode. The
#      launch stage enters a label by the AppArmor exec attribute, so it
#      also fails when kernel.apparmor_restrict_unprivileged_unconfined is
#      1 (decision CH55). No profile has an attachment path, so a CH that
#      no launch stage starts runs unconfined.
# Phase 4 replaces the upstream files with the CI-built CH, kernel and disks
# (CH36 G19).
#
# The files land in CRECHE_KVM_DIR, which is absent or empty:
#   cloud-hypervisor  ch-remote  vmlinux  initramfs.cpio
# The kvm_ tests read the same variable.
#
# `set -euo pipefail`: the first fault stops the run.
set -euo pipefail
export LC_ALL=C

REPO=$(cd "$(dirname "$0")/.." && pwd)
INSTALLED_CH=/opt/components/vmm/bin/cloud-hypervisor
PROFILE=/etc/apparmor.d/opt.components.vmm.bin.cloud-hypervisor
SLOT_PROFILES=$REPO/rust/crates/vmm-confine/apparmor/cloud-hypervisor-slots.profile
RESTRICT=/proc/sys/kernel/apparmor_restrict_unprivileged_unconfined
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
echo "XDG_RUNTIME_DIR=/run/user/$(id -u)" >> "${GITHUB_ENV:?GITHUB_ENV names the env file of the job}"

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

say "load the slot profiles of CH"
[[ "$(cat /sys/module/apparmor/parameters/enabled)" == Y ]] || die "AppArmor is off on this runner"
[[ ! -e "$RESTRICT" || "$(cat "$RESTRICT")" == 0 ]] ||
  die "$RESTRICT is not 0: the launch stage cannot enter a slot label (decision CH55)"
sudo install -D -m 0755 cloud-hypervisor "$INSTALLED_CH"
sudo install -m 0644 "$SLOT_PROFILES" "$PROFILE"
sudo apparmor_parser -r "$PROFILE"
mapfile -t labels < <(sed -n 's/^profile \(creche-ch-slot-[0-9]*\) {$/\1/p' "$SLOT_PROFILES")
((${#labels[@]} > 0)) || die "$SLOT_PROFILES holds no slot profile"
loaded=$(sudo cat /sys/kernel/security/apparmor/profiles)
for label in "${labels[@]}"; do
  grep -Fqx "$label (enforce)" <<< "$loaded" || die "the profile $label is not in enforce mode"
done
say "${#labels[@]} slot labels in enforce mode"

say "ready in $dir"
