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
- `stage::run` is the mode `vmm launch-stage <plan>` (mechanism X,
  decision CH55, which amends CH30). The launch step execs `systemd-run`
  with the stage as the scope payload: the resolved path of
  `/proc/self/exe` (decision CH57 rule 5, no new config key),
  `stage::MODE` and the stage plan as one JSON argument. After a release
  renames the old binary to `.prev` (fact 56), the path names `.prev`. A
  deleted binary fails the launch closed. The stage then runs four steps
  in this order, and the first failure starts no CH:
  1. It writes `exec creche-ch-slot-<k>` to
     `/proc/self/attr/apparmor/exec`, or to `/proc/self/attr/exec` when
     the first is missing. Any other error fails the launch closed.
  2. It sets no_new_privs and reads it back.
  3. It applies the Landlock ruleset of the VM.
  4. It execs CH.
  Before step 1 it checks that the death signal is still SIGKILL. A
  launch needs `Launch::in_slot`, and the launch step refuses a launch
  with no slot (`LaunchError::NoSlot`). `stage::run` is the one entry,
  and it always writes the real exec attribute (decision CH58 rule 1).
  No entry skips the slot label, in production or in a test.
- The pid stays the same through the four execs: the launch step,
  `systemd-run`, the stage and CH (fact 80). The kernel keeps the death
  signal through an exec that is not a secure exec and changes no uid,
  gid or capability, but AppArmor clears it at the exec into the slot
  label (Known gaps).
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
  (decision CH45 rule 2). Decision CH50 rule 9 sets `READ_TRIES` (5),
  `RETRY_PAUSE` (10 ms) and `SHAPE_CAP` (4 KiB). A read blocks its thread
  in `thread::sleep`, so a caller in tokio runs it in `spawn_blocking`.
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
  devices from the snapshot (decisions CH2, CH29). The restore passes
  `resume=false`, so the VM stays paused (decision CH50 rule 10). A
  restore consumes the claim. A path with a comma, a bracket or a quote
  fails, because the CH option parser reads them as syntax.
- Every launch step forks from the one spawner thread (decision CH19 (4),
  fact 76). The vmm keeps its `Spawner` for its whole life. A drop ends
  the thread, and the kernel then kills every payload it started.
- The launch step reads its plan only from a pipe that already holds it.
  The spawner writes the whole plan, at most 64 KiB, before the spawn,
  and never blocks on a full pipe.
- The spawner gives each launch step one pipe for stdout and stderr in
  place of the fds of the vmm, so CH inherits no journal socket (decision
  CH50 rule 12). `output::start` reads it on one thread per VM and gives
  each line to the sink tagged `ch <session id>`, under `LINE_CAP` bytes
  per line and `RATE_LINES` lines per `RATE_WINDOW` (decision CH52 rule
  6). It escapes each control char, so CH cannot write a vmm line. The vmm sink is the
  stderr of the vmm with the prefix `vmm: `. The crate has no log crate,
  and `rust/Cargo.lock` holds none.
- `leftovers::stop_all` stops every user unit with the prefix
  `creche-vm-` and adopts none (decision CH19 (4), fact 76). The vmm calls
  it at start, before it binds or unlinks any per-VM socket path. A unit
  that is still loaded after the stop gets `reset-failed`, because a
  failed scope blocks `systemd-run --unit` with its name. The last list
  decides: a unit still loaded then is an error. The vmm config gives the
  path of `systemctl`, like the path of `systemd-run`.
- The Landlock ruleset of a VM confines the launch stage from its step 3
  on, then Cloud Hypervisor (decisions CH8-3, CH30, CH55). `systemd-run`
  runs before it, so the ruleset has no rule for it. The vmm process is
  never confined, so the `zstd` and `mke2fs`
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
- `admission::decide` is a pure decision (decisions CH8-6, CH8-7, CH58
  rule 2). Free bytes below the floor give `HostFull`. The configured
  guest RAM of the running VMs plus the RAM wanted at or below the cap,
  with a free slot, gives `Admit`. Else the idle VMs, least recently used
  first, either free enough RAM and one slot when none is free
  (`EvictFirst`) or not (`HostFull`, and no VM stops). A full slot pool
  counts like a full RAM cap: it needs one eviction at least, and with no
  idle VM it gives `HostFull` with `Full::Slots`. The floor must
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
- `slot_profile::render_file` makes
  `apparmor/cloud-hypervisor-slots.profile`, one AppArmor profile
  `creche-ch-slot-<k>` for each slot of `creche_vmm_store::layout::SLOTS`
  (mechanism X, decisions CH51, CH55). The crate never defines its own
  `Slot`. No profile has an attachment path. Each names the one installed
  CH, the run dir `/run/user/[0-9]*/vmm/slot/<k>/` and its own signal
  label only. The state root rules name the default
  `@{HOME}/.local/state/creche-vmm/` through `include <tunables/global>`
  (decision CH50 rule 11). The test `tests/slot_profile.rs` holds the
  committed file equal to the generator. After a change, run
  `CRECHE_SLOT_PROFILES=write cargo test -p creche-vmm-confine --test
  slot_profile` in rust/ and commit the file. David installs it at
  `/etc/apparmor.d/opt.components.vmm.bin.cloud-hypervisor` (design
  section 6.1 step 1, decision CH36 gap G28 option A). Each change of
  the file needs a sudo reload on the host by David (design section 6.1
  step 2).
- The process tests `tests/launch.rs` and `tests/landlock.rs` run the
  real launch stage on a host with no slot profile, as the job `rust` of
  gate.yml is. There each launch fails closed at the label step with
  `StageError::Enter`, and no payload starts (decision CH58 rule 1).
- A test that boots CH has a name that holds `kvm_` and carries
  `#[ignore]` (decisions CH8-4, CH36 G19). The job `kvm` of gate.yml runs
  every such test of the workspace after `bin/vm-smoke-setup.sh`. The test
  reads CH, ch-remote, the kernel and the busybox initramfs from the
  directory in `CRECHE_KVM_DIR`, and fails when it is not set.
- The job `kvm` installs CH at `slot_profile::CH_BINARY` and loads the
  slot profiles at the install path above with the runner's sudo
  (decision CH36 G20). It fails unless each label of the file is in
  enforce mode. `tests/kvm_slot.rs` starts each CH through the real chain
  of the vmm: the spawner, the launch step, `systemd-run --user --scope`,
  the launch stage and CH. Each VM takes a slot through
  `RunRoot::link_vm`, under the real run root and the default state
  root. Each test has a control, and together they prove decision CH58
  rule 1:
  1. CH runs under `creche-ch-slot-<k>`, with no_new_privs set, the core
     limit 0 and the scope of its VM. CH with no chain runs unconfined.
  2. The ruleset lets CH read the root disk and write the session disk.
     It denies a write of the root disk and a read outside the VM files,
     at paths that the slot profile allows (decisions CH8-3, CH8-8). CH
     makes no TCP connect, and a slot label opens no inet socket, TCP or
     UDP (decision CH8-1, fact 77).
  3. CH in one slot connects to no listener of another slot, to no
     `api.sock` or `vsock.sock` of a live CH in another slot, and not to
     `vmm.sock` or the user bus. One slot cannot signal the CH of another
     slot (decision CH51).
  4. CH dies with the spawner thread and with a killed vmm (fact 80).
     These two tests fail today: see the first Known gap.
     `Launched::terminate` sends SIGTERM.
  5. The output of each CH reaches the sink tagged `ch <session id>`.
  `tests/kvm_boot.rs` runs the copy in `CRECHE_KVM_DIR` with no chain.

## Known gaps

- CONTRACT-QUESTION (decisions CH19 (4), CH55, CH56 rule 2, facts 76
  and 80): AppArmor sets the death signal to 0 at each exec that changes
  the label (`apparmor_bprm_committing_creds` in
  `security/apparmor/lsm.c`). The exec of CH into `creche-ch-slot-<k>`
  is one, so CH under its label has no death signal. In the job `kvm`, CH
  runs on after a drop of the spawner and after a SIGKILL of the vmm
  (`kvm_ch_dies_with_the_spawner_thread`, `kvm_ch_dies_with_a_killed_vmm`
  fail). The single profile of #443 attached at the same exec, so it had
  the same gap. No row picks the replacement: for example a scope bound to
  the vmm unit (`BindsTo=`), a resident stage that kills CH when the vmm
  dies, or a cgroup that the vmm unit delegates. Until David decides, the
  two tests stay red and CH56 rule 2 does not hold.

- Closed by plan PRs L16b and L16c: lifecycle passes the slot of each
  start with `Launch::in_slot`, so no launch of the vmm fails with
  `LaunchError::NoSlot`. `rust/crates/vmm/src/main.rs` routes
  `stage::MODE` with its one plan argument to `stage::run`. C18, L16b
  and L16c are in one PR.
- Decision CH50 rule 11 wants a test that holds the state root of the
  slot profiles equal to the env default. The env key comes with plan PR
  L20, so that test comes with it. Until then `slot_profile::STATE_ROOT`
  holds the default alone.
- Lifecycle must make two dirs before each launch, because the ruleset
  opens each path and a missing one fails the launch with
  `RulesetError::Open`: the VM run dir `<run root>/vm/<id>/` and the
  snapshot dir `<state root>/sessions/<id>/snapshots/`. No code on main
  makes either today (`SessionStore::for_boot` makes only the session
  dir). The ruleset grants no right to make a dir, so the vmm must also
  make each `snapshots/<generation>/` before CH writes a snapshot there.
  Plan PRs L16 (cold boot) and D9 (snapshot) own these steps.
- Decisions CH42 rule 2 and CH50 rule 10: on a restore the CH command
  line shows no device, so `deny::check` cannot see a device that the
  snapshot holds. The restore runs with `resume=false`. Plan PR L17
  (restore) must then run these steps in order, and no code runs them
  yet:
  1. `ch-remote info`, whose `state` must be `Paused`.
  2. The device check. It refuses `net`, `generic_vhost_user`, `fs`,
     `user_devices`, `vdpa`, `devices`, `pmem` and `tpm`, any disk
     beyond the fixed two, and a vsock path other than the VM's own.
  3. `ch-remote resume`.
  4. The wait for `ControlReady`.
- CONTRACT-QUESTION (decisions CH21-1, CH36 G18): contract 05 §3.3.1 has
  no row for the vmm until its amendment. Open until then: the group of
  `faults/vmm/` and its files (the vmm sets modes 0750 and 0640 only),
  the value `vmm` in the `source` enum of §3.3, and the reader. caregiver
  reads only the `attendance` and `pep` files today, so it ignores the
  vmm file.
- The slot profiles read only `store/*` for a kernel or an initramfs.
  Decision CH7-6 has the vmm write the initramfs of guest init at VM
  start, in a place that no row names yet. A file outside the store needs
  a rule in `slot_profile.rs` and David's sudo reload.
- Gap G23 sets `serial.log` to mode 0600. CH makes the file with the
  umask of the launch step, and the crate sets no umask. A `UMask=` in
  the unit of plan PR C13, or a file that lifecycle makes first in plan
  PR L16, must set it. Neither plan entry names the step yet. Until then
  `RuntimeDirectoryMode=0700` of the run root keeps other users out.
