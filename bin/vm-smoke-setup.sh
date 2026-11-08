#!/usr/bin/env bash
# CI: makes a GitHub runner ready for the kvm_ tests and builds what they boot.
#   CRECHE_KVM_DIR=<dir> bin/vm-smoke-setup.sh
# The job `kvm` of gate.yml and of release.yml runs it. No hook and no unit
# runs it. It refuses to run outside GitHub Actions: it uses sudo, and CH36
# G20 allows sudo on GitHub's throwaway runners only, never on the host.
#
# The method is the one the C0 spike proved (design note fact 79):
#   1. /dev/kvm must exist. No KVM fails here, loudly (CH36 G19).
#   2. It installs musl-tools from apt, the pinned toolchain and the musl
#      target, the one target of the vmm row (CH7-4, CH7-5). apt and rustup
#      run at once. The job runs this script before its `toolchain` step,
#      so the script installs the toolchain itself (CH62 rule 3).
#   3. It starts the build of CH and ch-remote in the background (decision
#      CH75): the 13 words of bin/lib/chbuild.sh, the CH7-4 argv at
#      the rev of fact 78, with no cache. The build has its own
#      CARGO_TARGET_DIR, so no other build of the job waits for its lock.
#      `timeout` bounds it at CH_BUILD_LIMIT, so the wait in step 11
#      cannot hang. Steps 4 to 10 run beside it.
#   4. A udev rule with MODE 0666 opens /dev/kvm and /dev/vhost-vsock to the
#      runner user, who is not in the group kvm. `modprobe vhost_vsock`
#      makes the second node.
#   5. Linger starts the user manager, for a later `systemd-run --user`.
#      The script writes XDG_RUNTIME_DIR to $GITHUB_ENV, so the later steps
#      of the job reach the user manager, as the vmm unit does. First a
#      drop-in gives user@.service and the units of the user manager the
#      hard RLIMIT_NOFILE of Hyperion's user manager, 1048576. The runner
#      gives 65536, and `vmm serve` exits 78 below its need of 102720
#      (decision CH67 rule 4, which keeps the vmm unit free of a
#      LimitNOFILE= line). fs.nr_open must hold that limit. A user manager
#      that runs before the drop-ins gets the limit in place, by root, and
#      a re-exec. The script fails unless a unit of the user manager gets
#      that hard limit, and then prints each limit on the way.
#   6. It fetches the CH guest kernel (fact 53), checked by its sha256.
#   7. It builds guest-init and the stand-in facade static for musl. It
#      refuses a program with a PT_INTERP header, and first proves that the
#      check refuses the runner's dynamic /bin/bash. The stand-in is a test
#      program in no release row (CH62 rule 12), and the guest-init kvm
#      tests put it on their root disks (CH64 rule 2). It packs guest-init
#      as /init in guest-init.cpio, with dev/console 5:1, and fails unless
#      GNU cpio lists exactly those entries, owned by root.
#   8. It builds a busybox initramfs from `apt-get download busybox-static`.
#      Its init writes the line CRECHE-KVM-BOOTED to the serial port, then
#      `tick` once a second. It keeps the static busybox program, which a
#      kvm test packs into its own initramfs.
#   9. It loads the slot profiles of rust/crates/vmm-confine/apparmor/
#      cloud-hypervisor-slots.profile with the commands of design section
#      6.1 step 1 (decisions CH8-1, CH55, gap G28 of CH36). It fails unless
#      each creche-ch-slot-<k> label of the file is in enforce mode. The
#      launch stage enters a label by the AppArmor exec attribute, so it
#      also fails when kernel.apparmor_restrict_unprivileged_unconfined is
#      1 (decision CH55). No profile has an attachment path, so a CH that
#      no launch stage starts runs unconfined.
#  10. It builds the test programs of rust/ with `cargo test --no-run`, so
#      the steps of the job after it build nothing.
#  11. It waits for the CH build. A failed build prints its log and fails
#      the run. The install root must hold the two programs alone. Each is
#      static, and `cloud-hypervisor --version` must print CH_BUILD_VERSION
#      (fact 78), alone or after the program name, as the key check of the
#      vmm reads it (gap G10). Then it installs CH at
#      /opt/components/vmm/bin/cloud-hypervisor, the path of the slot
#      profiles.
#
# The CPUs: steps 7 and 10 build under `nice -n 19`, with as many jobs as
# the CH build, one per CPU. The CH build is the longest path of the job,
# so it takes each CPU that it can use. Its release profile has LTO and one
# codegen unit, so the last link of each of its two programs runs on one
# CPU, and the other builds take the free CPUs then. A fixed split of the
# jobs would leave CPUs idle in that link.
#
# The files land in CRECHE_KVM_DIR, which is absent or empty:
#   cloud-hypervisor  ch-remote  vmlinux  initramfs.cpio  busybox
#   guest-init  standin_facade  guest-init.cpio
# The kvm_ tests read the same variable.
#
# `set -euo pipefail`: the first fault stops the run.
set -euo pipefail
export LC_ALL=C

REPO=$(cd "$(dirname "$0")/.." && pwd)
# shellcheck source=lib/chbuild.sh
. "$REPO/bin/lib/chbuild.sh"
INSTALLED_CH=/opt/components/vmm/bin/cloud-hypervisor
PROFILE=/etc/apparmor.d/opt.components.vmm.bin.cloud-hypervisor
SLOT_PROFILES=$REPO/rust/crates/vmm-confine/apparmor/cloud-hypervisor-slots.profile
RESTRICT=/proc/sys/kernel/apparmor_restrict_unprivileged_unconfined
KERNEL_URL=https://github.com/cloud-hypervisor/linux/releases/download/ch-release-v7.2.8-20261009
MUSL=x86_64-unknown-linux-musl
# The hard RLIMIT_NOFILE of the user manager on Hyperion.
NOFILE_HARD=1048576
# About twice the 5 min 34 s of fact 78.
CH_BUILD_LIMIT=12m

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
mkdir -p "$dir"
say "$(nproc) CPUs"

say "install musl-tools, the pinned toolchain and the $MUSL target"
# musl-gcc for any C that a dependency of CH builds, as in the spike of
# fact 78. The busybox download of step 8 reads the same index.
(sudo apt-get update -qq && sudo apt-get install --yes --no-install-recommends -qq musl-tools) &
apt_pid=$!
# In rust/, rustup reads the pinned toolchain from rust-toolchain.toml.
(
  cd "$REPO/rust"
  rustup toolchain install --no-self-update
  rustup target add "$MUSL"
)
wait "$apt_pid" || die "apt-get did not install musl-tools"

say "start the build of CH and ch-remote, at most $CH_BUILD_LIMIT"
ch_work=$(mktemp -d "$dir/ch-build.XXXXXX")
ch_log=$ch_work/build.log
ch_start=$SECONDS
(
  cd "$REPO/rust"
  export CARGO_TARGET_DIR=$ch_work/target
  export CARGO_INSTALL_ROOT=$ch_work/root
  export CARGO_BUILD_TARGET=$MUSL
  exec timeout --kill-after=30s "$CH_BUILD_LIMIT" "${CH_BUILD[@]}"
) < /dev/null > "$ch_log" 2>&1 &
ch_pid=$!
# A fault in steps 4 to 10 stops the build too.
trap 'kill "$ch_pid" 2> /dev/null || true' EXIT

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

say "give the user manager the hard RLIMIT_NOFILE of Hyperion, $NOFILE_HARD"
# No process gets a hard limit above fs.nr_open, so it must hold the limit.
nr_open=$(cat /proc/sys/fs/nr_open)
say "fs.nr_open is $nr_open"
if ((nr_open < NOFILE_HARD)); then
  sudo sysctl -q -w "fs.nr_open=$NOFILE_HARD"
fi
sudo mkdir -p /etc/systemd/system/user@.service.d /etc/systemd/user.conf.d
printf '[Service]\nLimitNOFILE=%s\n' "$NOFILE_HARD" |
  sudo tee /etc/systemd/system/user@.service.d/90-creche-nofile.conf > /dev/null
printf '[Manager]\nDefaultLimitNOFILE=1024:%s\n' "$NOFILE_HARD" |
  sudo tee /etc/systemd/user.conf.d/90-creche-nofile.conf > /dev/null
sudo systemctl daemon-reload
# A user manager that already runs keeps its old limit until it restarts.
if systemctl is-active --quiet "user@$(id -u).service"; then
  sudo systemctl restart "user@$(id -u).service"
fi

say "start the user manager"
sudo loginctl enable-linger "$(id -un)"
bus="/run/user/$(id -u)/bus"
for _ in $(seq 1 50); do
  [[ -S "$bus" ]] && break
  sleep 0.1
done
[[ -S "$bus" ]] || die "no user manager: $bus is missing"
XDG_RUNTIME_DIR="/run/user/$(id -u)"
export XDG_RUNTIME_DIR
echo "XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR" >> "${GITHUB_ENV:?GITHUB_ENV names the env file of the job}"
manager=$(systemctl show -p MainPID --value "user@$(id -u).service")
manager_hard() { awk '/^Max open files/ { print $5 }' "/proc/$manager/limits"; }
say "the user manager (pid $manager) has the hard RLIMIT_NOFILE $(manager_hard)"
# A user manager that started before the drop-ins keeps its old limit.
# Root raises it in place, and a re-exec makes the manager read
# DefaultLimitNOFILE of user.conf.d again for its new units.
if (($(manager_hard) < NOFILE_HARD)); then
  soft=$(awk '/^Max open files/ { print $4 }' "/proc/$manager/limits")
  sudo prlimit --pid "$manager" --nofile="$soft:$NOFILE_HARD"
  systemctl --user daemon-reexec
  say "raised the user manager to the hard RLIMIT_NOFILE $(manager_hard)"
fi
hard=$(systemd-run --user --pipe --quiet --collect sh -c 'ulimit -Hn')
if [[ "$hard" != "$NOFILE_HARD" ]]; then
  systemctl show -p LimitNOFILE "user@$(id -u).service" >&2
  systemctl --user show -p DefaultLimitNOFILE >&2
  grep '^Max open files' "/proc/$manager/limits" >&2
  die "a user unit gets the hard RLIMIT_NOFILE $hard, not $NOFILE_HARD"
fi

cd "$dir"

say "fetch the guest kernel"
curl -fsSL -o vmlinux "$KERNEL_URL/vmlinux-x86_64"
sha256sum --check --strict <<'EOF'
edfeb8a8a6c04ff2979e9f1c700feaa29b8f564f952261834ef0b7d2e673a853  vmlinux
EOF

say "build guest-init and the stand-in facade static for $MUSL"
(
  cd "$REPO/rust"
  nice -n 19 cargo build --locked --release --target "$MUSL" \
    -p creche-guest-init --bin guest-init --example standin_facade
)
built=$REPO/rust/target/$MUSL/release
install -m 0755 "$built/guest-init" guest-init
install -m 0755 "$built/examples/standin_facade" standin_facade
# A static program has no PT_INTERP header. The guest has no loader.
# require_static dies when the program names a program interpreter.
require_static() {
  local headers
  headers=$(readelf -lW "$1") || die "readelf cannot read $1"
  if grep -Eq '^[[:space:]]+INTERP[[:space:]]' <<< "$headers"; then
    die "$1 names a program interpreter: it is not static"
  fi
}
# The positive control: the check must refuse a dynamic program. A broken
# pattern or a lost `die` makes the subshell pass, and this line fails.
if (require_static /bin/bash) 2> /dev/null; then
  die "the static check is broken: it passes the dynamic /bin/bash"
fi
for program in guest-init standin_facade; do
  require_static "$program"
done
say "guest-init and standin_facade are static"
work=$(mktemp -d "$dir/guest-init.XXXXXX")
install -d -m 0755 "$work/root" "$work/root/dev"
install -m 0755 guest-init "$work/root/init"
# The kernel opens /dev/console for PID 1 before any devtmpfs mount.
sudo mknod -m 0600 "$work/root/dev/console" c 5 1
# `-R 0:0`: root owns each entry, as on a booted root disk.
(cd "$work/root" && find . | cpio -o -H newc -R 0:0 --quiet) > guest-init.cpio
rm -rf "$work"
# GNU cpio with `-H newc` refuses an archive in another format. The kernel
# reads newc only. awk keeps the mode, the numeric owner, a device's
# major,minor and the name.
listing=$(cpio -itvn -H newc --quiet < guest-init.cpio |
  awk '$1 ~ /^c/ { print $1, $3, $4, $5 $6, $NF; next } { print $1, $3, $4, $NF }' |
  sort) || die "GNU cpio cannot list guest-init.cpio as a newc archive"
expected=$(printf '%s\n' \
  'drwxr-xr-x 0 0 .' \
  'drwxr-xr-x 0 0 dev' \
  'crw------- 0 0 5,1 dev/console' \
  '-rwxr-xr-x 0 0 init' | sort)
[[ "$listing" == "$expected" ]] ||
  die "guest-init.cpio is not /init, dev and dev/console 5:1 owned by root:"$'\n'"$listing"
say "guest-init.cpio holds /init, dev and dev/console 5:1, owned by root"

say "build the busybox initramfs"
work=$(mktemp -d "$dir/busybox.XXXXXX")
(cd "$work" && apt-get download busybox-static)
dpkg-deb -x "$work"/busybox-static_*.deb "$work/deb"
mkdir -p "$work/root/bin" "$work/root/proc"
# The package may put the program in bin/ or in usr/bin/.
busybox=$(find "$work/deb" -type f -name busybox | head -n 1)
[[ -n "$busybox" ]] || die "busybox-static holds no busybox program"
cp "$busybox" "$work/root/bin/busybox"
install -m 0755 "$busybox" busybox
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
sudo install -m 0644 "$SLOT_PROFILES" "$PROFILE"
sudo apparmor_parser -r "$PROFILE"
mapfile -t labels < <(sed -n 's/^profile \(creche-ch-slot-[0-9]*\) {$/\1/p' "$SLOT_PROFILES")
((${#labels[@]} > 0)) || die "$SLOT_PROFILES holds no slot profile"
loaded=$(sudo cat /sys/kernel/security/apparmor/profiles)
for label in "${labels[@]}"; do
  grep -Fqx "$label (enforce)" <<< "$loaded" || die "the profile $label is not in enforce mode"
done
say "${#labels[@]} slot labels in enforce mode"

say "build the test programs of rust/"
(cd "$REPO/rust" && nice -n 19 cargo test --locked --no-run)

say "wait for the build of CH and ch-remote"
wait_start=$SECONDS
ch_status=0
wait "$ch_pid" || ch_status=$?
trap - EXIT
if ((ch_status != 0)); then
  cat "$ch_log" >&2
  if ((ch_status == 124)); then
    die "the build of CH and ch-remote passed its limit of $CH_BUILD_LIMIT"
  fi
  die "the build of CH and ch-remote failed with status $ch_status"
fi
# A passed build keeps its log in a group that the run page folds.
echo "::group::the log of the CH build"
cat "$ch_log"
echo "::endgroup::"
say "CH and ch-remote built in $((SECONDS - ch_start)) s, after a wait of $((SECONDS - wait_start)) s"
found=$(cd "$ch_work/root" && find . -mindepth 1 | sort)
[[ "$found" == "$(printf '%s\n' ./bin ./bin/ch-remote ./bin/cloud-hypervisor)" ]] ||
  die "the install root of CH holds other files:"$'\n'"$found"
install -m 0755 "$ch_work/root/bin/cloud-hypervisor" cloud-hypervisor
install -m 0755 "$ch_work/root/bin/ch-remote" ch-remote
rm -rf "$ch_work"
for program in cloud-hypervisor ch-remote; do
  require_static "$program"
done
version=$(./cloud-hypervisor --version) || die "cloud-hypervisor --version failed"
[[ "$version" == "$CH_BUILD_VERSION" || "$version" == "cloud-hypervisor $CH_BUILD_VERSION" ]] ||
  die "cloud-hypervisor prints $version, not $CH_BUILD_VERSION (fact 78)"
say "cloud-hypervisor and ch-remote are static, and CH prints $version"
sudo install -D -m 0755 cloud-hypervisor "$INSTALLED_CH"

df -h /
say "ready in $dir"
