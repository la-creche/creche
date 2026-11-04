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
  `AGENT_SYSTEMD_RUN_PROGRAM`, the `systemctl` program of
  `leftovers::stop_all`, from the existing key `AGENT_SYSTEMCTL_PROGRAM`,
  and the faults root of `faults::FaultDir::open`, the `faults` dir of
  the creche state root that caregiver reads (contract 05 §3.3.1).
- `limits::VmLimits` maps the family VM shape to the VM sizes and the
  scope limits (decision CH8-5): `--cpus boot=N`, `CPUQuota=N×100%`,
  `--memory size=<guest RAM>M`, `MemoryMax` = guest RAM + 512 MiB, the
  balloon with free page reporting and deflate on OOM, and
  `TasksMax=256`. The 512 MiB is a first value. Phase 8 measures it.
- `family::FamilyRoot` reads a family's `config/` as caregiver's one
  writer leaves it (decisions CH5b, CH23 (3)). Each read opens `config/`
  with `O_NOFOLLOW`, reads through that one handle, then compares its
  device and inode with `fstatat(family_dir, "config")`. A swap or an
  absent `config/` (the rename window) starts the read again. An absent
  `config/` after every try is `Unknown`, which create answers with
  `unknown_family` (decision CH33 rule 3). A bad `config/shape.json` is
  `NotValid` (decisions CH36 G6, CH43 rule 3). The root has no default.
  Lifecycle passes it, and caregiver writes no `shape.json` before the cut
  (decision CH45 rule 2).
- `command::ChCommand` renders the CH argv from the checked store types
  only: `CheckedImage` (PR D6), `SessionDisk` (PR D7) and
  `ClaimedSnapshot` (PR D11). No input names a network card, and no
  argument names `--seccomp` (decisions CH6-3, CH8-1, CH8-4). A boot
  gives the root disk `readonly=on` as the first `--disk` (decision
  CH8-8), the session disk the `DiskLimit::SESSION` rate limiter
  (decisions CH8-7, CH36 G15), and `--serial file=` the `serial.log` of
  the VM run dir with `--console off` (decisions CH36 G23, CH41 rule 2).
  A restore gives only `--api-socket` and `--restore` with
  `memory_restore_mode=copyonwrite`, because CH v54 takes the sizes and
  devices from the snapshot (decisions CH2, CH29). A restore consumes the
  claim. A path with a comma, a bracket or a quote fails, because the CH
  option parser reads them as syntax.
- Every launch step forks from the one spawner thread (decision CH19 (4),
  fact 76). The vmm keeps its `Spawner` for its whole life. A drop ends
  the thread, and the kernel then kills every payload it started.
- The launch step reads its plan only from a pipe that already holds it.
  The spawner writes the whole plan, at most 64 KiB, before the spawn,
  and never blocks on a full pipe.
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
  first argument of the command line is the payload program, so a pid
  that still runs the launch step or `systemd-run`, or a CH zombie, fails
  with `NotCh`. The CH command line has no `--net` argument. A boot has exactly one
  `--vsock` and a restore none, because CH takes the devices of a restore
  from the snapshot. The check trusts that a snapshot comes only from a
  boot that passed the check (decision CH42 rule 2). The run dir holds
  exactly the `vsock.sock_<port>` listeners of the host ports that the
  vmm bound for the family. Any failure is the fault
  `egress_assert_failed`.
- `admission::decide` is a pure decision (decisions CH8-6, CH8-7). Free
  bytes below the floor give `HostFull`. The configured guest RAM of the
  running VMs plus the RAM wanted at or below the cap gives `Admit`. Else
  the idle VMs, least recently used first, either free enough RAM
  (`EvictFirst`) or not (`HostFull`, and no VM stops). The floor must
  hold after the writes that admission causes (decision CH42 rule 3).
  The snapshot of each evicted VM writes its whole guest RAM (fact 72),
  so admission subtracts that RAM from the free bytes before it compares
  with the floor. An eviction that breaks the floor gives `HostFull`, and
  no VM stops. Lifecycle passes the sleep-gate result of each VM and runs
  the evictions. The vmm never queues (decision CH21-3).
- `faults::FaultBook` writes the fault file of the vmm,
  `<faults root>/vmm/<family>.json`, in the shape of contract 05 §3.3.1
  with `source` `vmm` (decisions CH21-1, CH36 G18). It holds the two
  start faults of decision CH6-3. Each change rewrites the whole file
  atomically. Lifecycle calls `refresh` every `faults::REFRESH` (30 s)
  while a fault is open, `clear` on a `ControlReady` of the family, and
  `start` at vmm start, which writes an empty list into every file. Each
  time is RFC 3339 in UTC to the second, from `rfc3339::format`.
- The process tests need Landlock ABI 4 and `/dev/kvm`. The host and the
  ubuntu-24.04 runner have both (facts 77, 79).
- A test that boots CH has a name that holds `kvm_` and carries
  `#[ignore]` (decisions CH8-4, CH36 G19). The job `kvm` of gate.yml runs
  every such test of the workspace after `bin/vm-smoke-setup.sh`. The test
  reads CH, ch-remote, the kernel and the busybox initramfs from the
  directory in `CRECHE_KVM_DIR`, and fails when it is not set.
- `apparmor/cloud-hypervisor.profile` is the AppArmor profile
  `creche-cloud-hypervisor` of `/opt/components/vmm/bin/cloud-hypervisor`
  (decision CH8-1, gap G28). It allows Unix stream sockets, denies every
  abstract address, and has no `network` rule, so CH gets no inet socket.
  The file rules mediate a pathname socket, so the write rules are the
  socket allow list: the VM run dir files of PR D1 under
  `/run/user/<uid>/vmm/vm/*/`, with one `vsock.sock_*` pattern (decision
  CH24), and the state names of decision CH39 rule 3. `tests/profile.rs`
  holds each rule equal to the layout names. A rename there needs an edit
  of the profile and David's sudo reload (design section 6.1 step 2).
  Agents never install it on the host.
- The job `kvm` installs CH at that path and loads the profile with the
  runner's sudo (decision CH36 G20). `tests/kvm_apparmor.rs` starts the
  installed CH through `setpriv --no-new-privs`: CH boots under the
  profile, and its `--tpm socket=` connect reaches a `vsock.sock_<port>`
  listener, but not `vmm.sock` or the user bus. The other kvm_ tests run
  the copy in `CRECHE_KVM_DIR`, which no profile confines.

## Known gaps

- Landlock ABI 4 covers neither UDP nor Unix sockets (fact 77). The
  AppArmor profile closes both, but on the host only after David's sudo
  install (design section 6.1 step 1). Phase 8 checks it with
  `aa-status`. Until then CH on the host can reach both.
- CONTRACT-QUESTION (decisions CH8-1, CH24): the run dir rules of the
  AppArmor profile match `vm/*/`, because one static profile cannot tell
  VMs apart. So each CH may connect to the `api.sock`, `vsock.sock` and
  `vsock.sock_<port>` listeners of every VM, also of another family.
  Landlock ABI 4 does not cover AF_UNIX (fact 77), so no other layer
  stops it. CH8-1 says "its own" sockets, and CH24 decides only the port
  wildcard. David decides whether this reading of "its own" holds.
- CONTRACT-QUESTION (decisions CH8-1, CH39 rule 3): no row sets the
  default of the state root (plan PR L20). The state rules of the profile
  match `store/*`, `sessions/*/disk.ext4` and `sessions/*/snapshots/**`
  under any root. A rule that names the root needs a sudo reload.
- CONTRACT-QUESTION (decision CH8-1): the launch step passes the stdout
  and stderr of the vmm to CH. Under the unit they may be a stream socket
  of the journal. AppArmor checks each inherited file at the exec, and no
  rule names a journal socket, so the kernel may replace them with a null
  file and drop the error lines of CH. No row decides whether CH may
  write to the journal. The runner does not test it.
- The profile reads only `store/*` for a kernel or an initramfs. Phase 3
  writes the initramfs at VM start (decision CH7-6) in a place that no row
  names yet. A file outside the store needs a rule and a sudo reload.
- Lifecycle must make two dirs before each launch, because the ruleset
  opens each path and a missing one fails the launch with
  `RulesetError::Open`: the VM run dir `<run root>/vm/<id>/` and the
  snapshot dir `<state root>/sessions/<id>/snapshots/`. No code on main
  makes either today (`SessionStore::for_boot` makes only the session
  dir). The ruleset grants no right to make a dir, so the vmm must also
  make each `snapshots/<generation>/` before CH writes a snapshot there.
  Plan PRs L16 (cold boot) and D9 (snapshot) own these steps.
- Decision CH42 rule 2: on a restore the CH command line shows no
  device, so `deny::check` cannot see a network card that the snapshot
  holds. The check trusts that a snapshot comes only from a boot that
  passed the check. Once the CH API (plan PR D8, `ch_remote`) is in, the
  vmm also reads the restored VM's device set through `ch-remote info`
  before the VM is ready. That device-set read is the step of plan PR
  L17 (restore), under decision CH42 rule 2. No code does the
  device-set read yet.
- CONTRACT-QUESTION (decisions CH21-1, CH36 G18): contract 05 §3.3.1 has
  no row for the vmm until its amendment. Open until then: the group of
  `faults/vmm/` and its files (the vmm sets modes 0750 and 0640 only),
  the value `vmm` in the `source` enum of §3.3, and the reader. caregiver
  reads only the `attendance` and `pep` files today, so it ignores the
  vmm file.
- CONTRACT-QUESTION (decisions CH5b, CH23 (3)): no row sets how often
  `family::FamilyRoot` reads `config/` again, the pause between two
  tries, or the size cap of `shape.json`. The code uses `READ_TRIES` (5),
  `RETRY_PAUSE` (10 ms) and `SHAPE_CAP` (4 KiB).
- CONTRACT-QUESTION (decisions CH2, CH29): `command::ChCommand::restore`
  passes `resume=true`, so the restored VM runs at once, as in the spikes
  of facts 74 and 79. No row decides whether the vmm resumes the VM
  itself through `ch-remote` after the restore. Plan PR L17 reads it.
- Gap G23 sets `serial.log` to mode 0600. CH makes the file with the
  umask of the launch step, and the crate sets no umask. A `UMask=` in
  the unit of plan PR C13, or a file that lifecycle makes first in plan
  PR L16, must set it. Neither plan entry names the step yet. Until then
  `RuntimeDirectoryMode=0700` of the run root keeps other users out.
