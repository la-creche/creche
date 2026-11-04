# vmm-confine

The crate `creche-vmm-confine`: the launch step, the limits and the access
rules of each Cloud Hypervisor process of the vmm. The root `AGENTS.md`
and `rust/crates/vmm/AGENTS.md` apply here too. The second file has the
table of the three vmm crates.

## Rules

- The code is synchronous. The crate uses no async runtime.
- The crate reads the types of `creche-vmm-store`. It never calls
  `creche-vmm`.
- `launch::run` is the mode `vmm launch`. `rust/crates/vmm/src/main.rs`
  calls it, so a change here never edits that file. `launch::MODE` holds
  the same word as main.rs. Lifecycle may make main.rs read it.
- The crate reads no env key. Lifecycle (plan PR L20) passes each host
  path from the vmm config (owner env rule, decision CH36 G5): the
  `systemd-run` program of `launch::Launch`, from the existing key
  `AGENT_SYSTEMD_RUN_PROGRAM`.
- Every launch step forks from the one spawner thread (decision CH19 (4),
  fact 76). The vmm keeps its `Spawner` for its whole life. A drop ends
  the thread, and the kernel then kills every payload it started.
- The launch step reads its plan only from a pipe that already holds it.
  The spawner writes the whole plan, at most 64 KiB, before the spawn,
  and never blocks on a full pipe.
- The Landlock ruleset of a VM confines the launch step from its last
  step on, then `systemd-run` and Cloud Hypervisor (decisions CH8-3,
  CH30). The vmm process is never confined, so the `zstd` and `mke2fs`
  that the store runs need no rule. Every path of a rule exists before
  the launch. A missing path or a kernel below Landlock ABI 4 fails the
  launch. All Landlock calls sit behind `cfg(target_os = "linux")`
  (decision CH38).
- The process tests need Landlock ABI 4 and `/dev/kvm`. The host and the
  ubuntu-24.04 runner have both (facts 77, 79).

## Known gaps

- Landlock ABI 4 covers neither UDP nor Unix sockets (fact 77). Until the
  AppArmor profile of PR C15, CH can reach both.
- Lifecycle must make two dirs before each launch, because the ruleset
  opens each path and a missing one fails the launch with
  `RulesetError::Open`: the VM run dir `<run root>/vm/<id>/` and the
  snapshot dir `<state root>/sessions/<id>/snapshots/`. No code on main
  makes either today (`SessionStore::for_boot` makes only the session
  dir). The ruleset grants no right to make a dir, so the vmm must also
  make each `snapshots/<generation>/` before CH writes a snapshot there.
  Plan PRs L16 (cold boot) and D9 (snapshot) own these steps.
