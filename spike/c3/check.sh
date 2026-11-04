#!/usr/bin/env bash
# SPIKE (decision CH51, Hyperion check): refuses to go on unless all 128
# spike profiles are loaded and in enforce mode: the 64 H profiles
# creche-c3spike-slot-<k> and the 64 X profiles creche-c3spike-xslot-<k>.
#   /home/david/c3-spike/check.sh
# probes.sh runs it first. It runs as david, with no sudo, and writes
# nothing. Two sources, because the profile list in securityfs needs root:
#   1. The policy dir, if it is readable without root: each name and mode.
#   2. aa-exec into each label: `busybox true` must run, which proves
#      the profile is loaded, and `busybox cat README.md` must fail, which
#      proves enforce mode (the profile has no rule for that file).
# aa-exec enters a label by an onexec write, the call of mechanism X.
# When aa-exec fails for a label, `sockprobe.py onexec` gives the cause:
# ENOENT means the profile is not loaded. Any other cause is a refusal,
# for example under apparmor_restrict_unprivileged_unconfined=1.
# Exit 1 when a spike profile is not loaded, is not in enforce mode, or the
# policy dir shows fewer than 128 in enforce mode. Exit 0 otherwise. When
# aa-exec is refused for every failed label and no profile is missing, the
# exit is 0 with `aa-exec=no`: H-T0 to H-T4 run, and H-T5, H-T6 and every
# X test FAIL with the cause. Line `aa-exec=ok` or `aa-exec=no` tells probes.sh which case.
set -euo pipefail

main() {
  # shellcheck source=lib.sh
  source "$(dirname "$0")/lib.sh"
  guard
  [[ -x "$BB" ]] || die "no $BB: run setup.sh first"

  local listed enforced
  listed=$(loaded_from_policy_dir)
  enforced=$(grep -c ' (enforce)$' <<< "$listed" || true)
  if [[ -n "$listed" ]]; then
    say "policy dir: $(grep -c . <<< "$listed") creche-c3spike profiles, $enforced in enforce mode"
  else
    say "policy dir: not readable without root, or no creche-c3spike profile"
  fi

  local name failed=() complain=()
  while read -r name; do
    if ! aa-exec -p "$name" -- "$BB" true 2> /dev/null; then
      failed+=("$name")
    elif aa-exec -p "$name" -- "$BB" cat "$K/README.md" > /dev/null 2>&1; then
      complain+=("${name#"$SPIKE"}")
    fi
  done < <(all_labels)

  # Split the aa-exec failures by cause.
  local missing=() refused=() causes="" part out="" cause k
  if ((${#failed[@]} > 0)); then
    out=$(python3 -I "$K/sockprobe.py" onexec "${failed[@]}") || die "sockprobe.py onexec failed"
    for part in $out; do
      k=${part%%=*}
      k=${k#"$SPIKE"}
      cause=${part#*=}
      # ok: the kernel took the onexec write, so the exec itself failed.
      [[ "$cause" != ok ]] || cause=exec_failed
      if [[ "$cause" == ENOENT ]]; then
        missing+=("$k")
      else
        refused+=("$k")
        [[ " $causes " == *" $cause "* ]] || causes+="${causes:+ }$cause"
      fi
    done
    ((${#missing[@]} + ${#refused[@]} == ${#failed[@]})) || die "sockprobe.py onexec: no cause for each label: $out"
  fi

  local exec_ok=no
  if ((${#failed[@]} == 0 && ${#complain[@]} == 0)); then
    exec_ok=ok
    say "aa-exec: all $PROFILES labels run busybox true, and each refuses to read README.md"
  else
    say "aa-exec: profile does not exist (ENOENT) for: ${missing[*]:-none}"
    say "aa-exec: refused (${causes:-no cause}) for: ${refused[*]:-none}"
    say "aa-exec: not in enforce mode for: ${complain[*]:-none}"
  fi
  echo "aa-exec=$exec_ok"

  if ((${#missing[@]} > 0)); then
    die "not all $PROFILES creche-c3spike profiles are loaded. David runs: sudo apparmor_parser -r $PROFILE_FILE"
  fi
  if ((${#complain[@]} > 0)); then
    die "not all $PROFILES creche-c3spike profiles are in enforce mode. David checks the modes with: sudo aa-status"
  fi
  if [[ -n "$listed" ]] && ((enforced != PROFILES)); then
    die "the policy dir shows $enforced of $PROFILES in enforce mode"
  fi
  if [[ "$exec_ok" == ok ]]; then
    say "all $PROFILES creche-c3spike profiles are loaded in enforce mode"
  elif [[ -n "$listed" ]]; then
    say "the policy dir shows all $PROFILES in enforce mode, but aa-exec cannot enter them ($causes): H-T5, H-T6 and the X tests will FAIL"
  else
    say "no spike profile is missing, but aa-exec cannot enter them ($causes): H-T0 to H-T4 run, H-T5, H-T6 and the X tests will FAIL"
  fi
}

main "$@"
exit
