#!/usr/bin/env bash
# SPIKE (decision CH51, Hyperion check): removes the scratch dir.
#   /home/david/c3-spike/cleanup.sh
# Runs as david, with no sudo, after David's sudo unload.
#   1. It kills each process of david whose program sits under the scratch
#      dir (a CH or busybox that a failed probe left, H or X).
#   2. It stops if a creche-c3spike profile is still loaded: the unload
#      command needs c3spike-slots.profile, so the file must stay until
#      David has run it. It stops too if it cannot tell. `cleanup.sh
#      --force` removes the dir anyway.
#   3. It removes /home/david/c3-spike and checks that it is gone.
# The load state has three values. yes: the policy dir lists a profile, or
# aa-exec enters H slot 0, or the kernel takes an onexec write for a
# label. no: the kernel answers ENOENT for all 128 labels (H and X), so it
# holds none of them.
# unknown: any other answer, for example a refusal under
# apparmor_restrict_unprivileged_unconfined=1, or an unreadable attr.
set -euo pipefail

main() {
  # shellcheck source=lib.sh
  source "$(dirname "$0")/lib.sh"
  guard
  local force=no
  [[ "${1:-}" == --force ]] && force=yes

  local pid pids
  pids=$(spike_pids)
  for pid in $pids; do
    say "kill $pid ($(readlink "/proc/$pid/exe" 2> /dev/null || echo gone))"
    kill -KILL "$pid" 2> /dev/null || true
  done
  [[ -z "$pids" ]] || sleep 0.5
  [[ -z "$(spike_pids)" ]] || die "spike processes still run: $(spike_pids | tr '\n' ' ')"

  local loaded
  loaded=$(load_state)
  if [[ "$loaded" == yes && "$force" == no ]]; then
    say "a creche-c3spike profile is still loaded. David runs this first:"
    printf '\n    sudo apparmor_parser -R %s\n\n' "$PROFILE_FILE"
    die "nothing removed. Run cleanup.sh again after the unload"
  fi
  if [[ "$loaded" == unknown* && "$force" == no ]]; then
    say "load state unknown: ${loaded#unknown }. If David has not unloaded yet, he runs this first:"
    printf '\n    sudo apparmor_parser -R %s\n\n' "$PROFILE_FILE"
    die "nothing removed. After the unload, run cleanup.sh --force"
  fi
  [[ "$loaded" != yes ]] || say "--force: a creche-c3spike profile stays loaded, with no file left to unload it"

  cd /
  rm -rf -- "$K"
  [[ ! -e "$K" ]] || die "$K is still there"
  say "removed $K"
  if [[ "$loaded" == no ]]; then
    say "no creche-c3spike profile is loaded"
  elif [[ "$loaded" == unknown* ]]; then
    say "--force: load state unknown (${loaded#unknown }), so a creche-c3spike profile may stay loaded"
  fi
}

# yes, no, or "unknown <causes>": see the head of this file.
load_state() {
  if loaded_from_policy_dir | grep -q .; then
    echo yes
    return
  fi
  if [[ -x "$BB" ]] && aa-exec -p "${PREFIX}0" -- "$BB" true 2> /dev/null; then
    echo yes
    return
  fi
  local names=() name out part causes=""
  while read -r name; do
    names+=("$name")
  done < <(all_labels)
  if ! out=$(python3 -I "$K/sockprobe.py" onexec "${names[@]}" 2> /dev/null); then
    echo "unknown sockprobe.py onexec failed"
    return
  fi
  for part in $out; do
    case ${part#*=} in
      ok)
        echo yes
        return
        ;;
      ENOENT) ;;
      *) [[ " $causes " == *" ${part#*=} "* ]] || causes+="${causes:+ }${part#*=}" ;;
    esac
  done
  if [[ -n "$causes" ]]; then
    echo "unknown $causes"
  elif [[ "$(wc -w <<< "$out")" -eq "$PROFILES" ]]; then
    echo no
  else
    echo "unknown no answer for each label"
  fi
}

main "$@"
exit
