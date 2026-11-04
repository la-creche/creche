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
- Every launch step forks from the one spawner thread (decision CH19 (4),
  fact 76). The vmm keeps its `Spawner` for its whole life. A drop ends
  the thread, and the kernel then kills every payload it started.
- The launch step reads its plan only from a pipe that already holds it.
  The spawner writes the whole plan, at most 64 KiB, before the spawn.
- `leftovers::stop_all` stops every user unit with the prefix
  `creche-vm-` and adopts none (decision CH19 (4), fact 76). The vmm calls
  it at start, before it binds or unlinks any per-VM socket path. A unit
  that is still loaded after the stop gets `reset-failed`, because a
  failed scope blocks `systemd-run --unit` with its name. The last list
  decides: a unit still loaded then is an error. The vmm config gives the
  path of `systemctl`, like the path of `systemd-run`.
- The Landlock ruleset of a VM confines the launch step from its last
  step on, then `systemd-run` and Cloud Hypervisor (decisions CH8-3,
  CH30). The vmm process is never confined, so the `zstd` and `mke2fs`
  that the store runs need no rule. Every path of a rule exists before
  the launch. A missing path or a kernel below Landlock ABI 4 fails the
  launch. All Landlock calls sit behind `cfg(target_os = "linux")`
  (decision CH38).
- `deny::check` is the proof of deny-by-default (decision CH6-3). The
  CH command line has no `--net` argument. A boot has exactly one
  `--vsock` and a restore none, because CH takes the devices of a restore
  from the snapshot. The run dir holds exactly the `vsock.sock_<port>`
  listeners of the host ports that the vmm bound for the family. Any
  failure is the fault `egress_assert_failed`.
- `admission::decide` is a pure decision (decisions CH8-6, CH8-7). Free
  bytes below the floor give `HostFull`. The configured guest RAM of the
  running VMs plus the RAM wanted at or below the cap gives `Admit`. Else
  the idle VMs, least recently used first, either free enough RAM
  (`EvictFirst`) or not (`HostFull`, and no VM stops). Lifecycle passes
  the sleep-gate result of each VM and runs the evictions. The vmm never
  queues (decision CH21-3).
- The process tests need Landlock ABI 4 and `/dev/kvm`. The host and the
  ubuntu-24.04 runner have both (facts 77, 79).

## Known gaps

- Landlock ABI 4 covers neither UDP nor Unix sockets (fact 77). Until the
  AppArmor profile of PR C15, CH can reach both.
- CONTRACT-QUESTION (decision CH6-3): on a restore the CH command line
  shows no device, so `deny::check` cannot see a network card that the
  snapshot holds. It relies on the snapshot of a boot that passed the
  check. No row says whether the check must also read the device set of
  a restored VM, for example from `ch-remote info`.
- CONTRACT-QUESTION (decision CH8-7): `admission::decide` checks the
  free-space floor against the free bytes before the start. The snapshot
  of an evicted VM first writes its whole guest RAM (fact 72), and the
  new session disk grows later. No row says whether the floor must still
  hold after those writes.
