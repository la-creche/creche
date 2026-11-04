# SPIKE (decision CH51, Hyperion check): the names that every kit script
# shares. setup.sh, check.sh, probes.sh and cleanup.sh source this file.
# Nothing here writes a file.
# shellcheck disable=SC2034 # the scripts that source it use each name.
# shellcheck shell=bash # sourced only, so it has no shebang.

# The scratch dir (CH36 G31). The kit lives here and writes only here.
# On Hyperion it is /home/david/c3-spike. The CI spike sets C3_SPIKE_ROOT
# to a dir in the runner's home, and runs gen-profiles.py with --root.
K=${C3_SPIKE_ROOT:-/home/david/c3-spike}
# The user the kit must run as: david on Hyperion, the runner user in CI.
KIT_USER=${C3_SPIKE_USER:-david}
# The slot count of each mechanism in c3spike-slots.profile (item 45 C3:
# about 64). The file holds 2 * SLOTS profiles.
SLOTS=64
PROFILES=$((2 * SLOTS))
# The prefix of every spike profile, of both mechanisms.
SPIKE=creche-c3spike-
# Mechanism H: the slot profile attaches by the path of a hard link.
PREFIX=creche-c3spike-slot-
# Mechanism X: the slot profile has no attachment. xlaunch.py enters it
# with an onexec write before the exec of the one installed CH.
X_PREFIX=creche-c3spike-xslot-
XLAUNCH=$K/xlaunch.py
PROFILE_FILE=$K/c3spike-slots.profile
BIN=$K/bin
BB=$BIN/busybox
RUN=$K/run
RUNX=$K/runx
STORE=$K/state/store
KERNEL=$STORE/$(printf '%064d' 1)
INITRAMFS=$STORE/$(printf '%064d' 2)
RESULTS=$K/results
MARKER=CRECHE-KVM-BOOTED

# The pins of bin/vm-smoke-setup.sh on origin/vmm/c-kvm-vm (the job kvm).
CH_URL=https://github.com/cloud-hypervisor/cloud-hypervisor/releases/download/v54.0
KERNEL_URL=https://github.com/cloud-hypervisor/linux/releases/download/ch-release-v7.2.8-20261009
CH_SHA=4f4f98a3d661a40f3e5272627cc895a1b2e525f2f6bedf5f895f9cd21ba5c65b
CH_REMOTE_SHA=acd5be240d5c73373effbe818908fc03e313ec8486237904bff11d93b71b1c83
VMLINUX_SHA=edfeb8a8a6c04ff2979e9f1c700feaa29b8f564f952261834ef0b7d2e673a853

export LC_ALL=C
export TMPDIR=$K/tmp
export PYTHONDONTWRITEBYTECODE=1

say() { printf '%s: %s\n' "${0##*/}" "$*"; }
die() {
  say "$*" >&2
  exit 1
}

# Refuses to run as anyone but david, or from anywhere but the scratch dir.
guard() {
  [[ "$(id -un)" == "$KIT_USER" ]] || die "run as $KIT_USER, not $(id -un)"
  [[ "$(id -u)" != 0 ]] || die "never run this as root"
  [[ "$K" == "$HOME"/* ]] || die "the scratch dir $K is not under HOME $HOME"
  local here
  here=$(cd "$(dirname "$0")" && pwd -P)
  [[ "$here" == "$K" ]] || die "the kit is at $here: it must be at $K (the profiles name that path)"
}

# The AppArmor label of a pid, or "gone".
label_of() {
  local attr=/proc/$1/attr/apparmor/current
  [[ -r "$attr" ]] || attr=/proc/$1/attr/current
  cat "$attr" 2> /dev/null || echo gone
}

# The names of all spike profiles, H then X, one per line.
all_labels() {
  local k
  for ((k = 0; k < SLOTS; k++)); do echo "$PREFIX$k"; done
  for ((k = 0; k < SLOTS; k++)); do echo "$X_PREFIX$k"; done
}

# The names of the loaded creche-c3spike profiles with their modes, one
# "name (mode)" per line, from the policy dir. Prints nothing if the dir is
# not readable without root.
loaded_from_policy_dir() {
  local dir
  for dir in /sys/kernel/security/apparmor/policy/profiles/"$SPIKE"*; do
    [[ -r "$dir/name" ]] || continue
    printf '%s (%s)\n' "$(cat "$dir/name")" "$(cat "$dir/mode" 2> /dev/null || echo '?')"
  done
}

# The pids of david's processes whose program sits under the scratch dir:
# the spike CHs and busybox. Other users' processes are not readable.
# xlaunch.py runs python3 for a few ms before its exec, so it is not
# listed. After the exec its pid shows the CH or busybox program.
spike_pids() {
  local proc exe
  for proc in /proc/[0-9]*; do
    exe=$(readlink "$proc/exe" 2> /dev/null) || continue
    [[ "$exe" == "$K"/* ]] && echo "${proc#/proc/}"
  done
  return 0
}

# Whether a pid runs and is not a zombie.
alive() {
  local state
  state=$(awk '{print $3}' "/proc/$1/stat" 2> /dev/null) || return 1
  [[ -n "$state" && "$state" != Z ]]
}
