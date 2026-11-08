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
  gid or capability. AppArmor clears it at the exec into the slot label
  (`apparmor_bprm_committing_creds` in `security/apparmor/lsm.c`), so CH
  has none (decision CH60 rule 1).
- Two links end each launch with the vmm (decision CH61, which amends
  CH60 rule 1, CH19 (4) and CH56 rule 2). The death signal ends the
  launch step, `systemd-run` and the stage when the vmm dies before CH
  runs. The scope binds to the vmm unit: `Launch::new` takes a
  `vmm_unit::VmmUnit`, and `scope::Scope::systemd_run_args` adds `-p
  BindsTo=<unit>` and `-p Before=<unit>` (CH61 rule 2). `Before=` keeps
  each VM running while the vmm unit stops, so the shutdown of
  `rust/crates/vmm/src/shutdown.rs` runs before any VM stops. The
  `ExecStopPost=` step of `systemd/creche-vmm.service` then kills every
  `creche-vm-*` scope that is left, after a stop, a restart and a crash
  (CH61 rule 1). It is the kill path of CH: systemd passes no stop of a
  restart on to a bound scope (CI run 42283795398). The step queues no
  job, because a stop job of a scope waits for the stop of the vmm unit.
  The leftover sweep of the next start still runs (CH19 (4)).
- `vmm_unit::VmmUnit::of_self` reads the unit of the vmm from
  `/proc/self/cgroup`, with no config key: the first `.service` part of
  the `0::` path after `user@<uid>.service`. A vmm in a scope, in a
  system service or in no user manager gets a `UnitError`, and lifecycle
  then refuses every launch with it and starts nothing. A unit name has
  only the characters of a systemd unit name, so it is one word in
  `BindsTo=`.
- The crate reads no env key. Lifecycle (plan PR L20) passes each host
  path from the vmm config (owner env rule, decision CH36 G5): the
  `systemd-run` program of `launch::Launch`, from the existing key
  `AGENT_SYSTEMD_RUN_PROGRAM`, the `systemctl` program of
  `leftovers::stop_all` and `leftovers::kill_all`, from the existing key
  `AGENT_SYSTEMCTL_PROGRAM`,
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
- `family::FamilyRoot::read_state` reads the family state of one pull on
  host port 1100 (decisions CH5a, CH5b, CH23). It reads
  `creds/creds.json` as one capped `O_NOFOLLOW` read, then `config/`
  through `with_config`, with no cross-check of the two (decision CH62
  rule 21). Both JSON files go through `creche_wire::strict` (decision
  CH65 rule 2), and each refuses a key that caregiver does not write. The
  reply drops `previous_pep_token`, `previous_expires_at`, `epoch`,
  `written_at` and `shell` (decision CH23 rule 1). `creds_missing` wins
  over every fault of `config/` (decision CH31 rule 4). A state over
  `FAMILY_STATE_CAP` is `too_large` (decision CH31 rule 3), and the
  instructions and skills share one budget of that cap, so no read holds
  more. `StateRead` holds the checked reply text and `model_live_at` of
  `runtime.json` (decision CH23-1), and its `Debug` writes no secret.
  Nothing keeps a last good state (decision CH53 rule 2). No live host
  writes `model_live_at` before the cut (decision CH45 rule 2), so the
  tests read the fixture root `tests/fixtures/family`.
- `family::FamilyRoot::read_egress` reads the egress allowlist
  `egress.json` of a family (decisions CH6-4, CH69 rules 3 and 5). The
  file sits in the family dir beside `config/`, so the read needs no
  swap retry. It opens the family dir with `O_NOFOLLOW`, then the file
  with one `openat` (`O_NOFOLLOW`, `O_NONBLOCK`). It checks a regular
  file, reads at most `EGRESS_CAP` (64 KiB, decision CH70 rule 1) and
  reads the bytes through `creche_wire::strict` as `FamilyEgress`
  (decision CH65 rule 2). An
  absent family dir or file is the empty list, so the family gets no
  egress. A symlink, a file that is not regular, a file past the cap and
  bytes that are not one `FamilyEgress` are each a `ReachFault`.
- `family::FamilyRoot::read_repos` reads the clone list `repos.json` of
  a family (decisions CH10-10, CH69 rules 4 and 5) as `read_egress`
  reads `egress.json`, with the cap `REPOS_CAP` (4 KiB, decision CH73
  rule 2), as
  `FamilyRepos`. An absent family dir or file is the empty list, so the
  family gets no clone. Each fault is a `ReachFault`.
- `mirror::MirrorRoot::git_dir` gives `<root>/<name>/.git` of a repo of
  the mirror (decisions CH46 rule 4, CH69 rules 16 and 17). It opens the
  root, then `<name>` and `.git`, each from the handle before it with
  `O_DIRECTORY` and `O_NOFOLLOW`. So a symlink or a file at either part
  refuses the repo (`MirrorFault::NotDir`), and an absent part is
  `MirrorFault::Absent`. The caller passes a `RepoName` of the clone
  list, which holds no `/` and no `..`, never the client bytes.
- `group::kill` sends SIGKILL to the process group of a leader pid
  (decision CH69 rule 14). The vmm spawns each `upload-pack` of the
  mirror as the leader of a new group, so the kill also ends its
  `pack-objects` child. The caller kills before it reaps the leader, so
  the group id names no other process.
- `nofile::raise` sets the soft `RLIMIT_NOFILE` of the vmm to the need
  that the vmm passes, and keeps the hard limit. A soft limit at or above
  the need stays. A hard limit below it is `NofileError::Short`, so the
  vmm never serves with fewer fds than its caps allow.
- `command::ChCommand` renders the CH argv from the checked store types
  only: `CheckedImage` (PR D6), `SessionDisk` (PR D7), `SerialLog` (PR
  D17) and `ClaimedSnapshot` (PR D11). No input names a network card, and no
  argument names `--seccomp` (decisions CH6-3, CH8-1, CH8-4). A boot
  gives the root disk `readonly=on` as the first `--disk` (decision
  CH8-8), the session disk the `DiskLimit::SESSION` rate limiter
  (decisions CH8-7, CH36 G15), and `--serial file=` the `SerialLog` of
  the session, `<state root>/sessions/<id>/serial.log`, with `--console
  off` (decisions CH36 G23, CH41 rule 2, CH59 rule 2).
  A restore gives only `--api-socket` and `--restore` with
  `memory_restore_mode=copyonwrite`, because CH v54 takes the sizes and
  devices from the snapshot (decisions CH2, CH29). The restore passes
  `resume=false`, so the VM stays paused (decision CH50 rule 10). A
  restore consumes the claim. A path with a comma, a bracket or a quote
  fails, because the CH option parser reads them as syntax.
- Every launch step forks from the one spawner thread (decision CH19 (4),
  fact 76). The vmm keeps its `Spawner` for its whole life. A drop ends
  the thread. The death signal then ends only a launch that has not yet
  execed CH: the launch step, `systemd-run` or the launch stage. A drop
  does not end CH. The `ExecStopPost=` step of the vmm unit ends CH
  (decision CH61 rule 1).
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
- `leftovers::kill_all` is the mode `vmm kill-scopes`, the `ExecStopPost=`
  step of the vmm unit (decisions CH61 rule 1, CH63). It lists the same
  units as `stop_all` and runs `systemctl --user kill --signal=SIGKILL`
  on each one by name. It queues no stop job, because each scope is
  ordered `Before=` the vmm unit (CH61 rule 2), so a wait for that job
  in the step would deadlock. When a kill fails, one fresh list decides:
  a unit that it no longer holds had ended (NoSuchUnit), and a unit that
  it still holds is an error. `tests/leftovers.rs` proves both against a
  fake `systemctl`.
- The Landlock ruleset of a VM confines the launch stage from its step 3
  on, then Cloud Hypervisor (decisions CH8-3, CH30, CH55). `systemd-run`
  runs before it, so the ruleset has no rule for it. The vmm process is
  never confined, so the `zstd` and `mke2fs`
  that the store runs need no rule. Every path of a rule exists before
  the launch. A missing path or a kernel below Landlock ABI 4 fails the
  launch. All Landlock calls sit behind `cfg(target_os = "linux")`
  (decision CH38).
- Only a boot reads the kernel. A boot and a restore both read the
  initramfs (decision CH61 rule 3): CH v54 opens the initramfs of the
  snapshot config on a restore, so `Start::Restore` carries the
  `store/<sha256>` initramfs of its snapshot (decision CH60 rule 2),
  and the snapshot key of `creche-vmm-store` holds its digest. A
  snapshot with the current key names the initramfs of the vmm config,
  so the vmm passes that checked store copy. The vmm writes one guest
  init initramfs at its start, and each VM start names it (decision
  CH62 rule 18). So `Start::Boot`, `Start::Restore` and
  `command::Boot` each require one, and a plan with no initramfs does
  not parse.
- `command::CMDLINE` is `console=ttyS0 rdinit=/init`, its final value
  (decisions CH36 G23, CH62 rule 19). The kernel runs `/init` of the
  initramfs, which is guest init. There is no `root=`, because guest
  init mounts `/dev/vda` itself, and no `panic=`, because a guest reboot
  would restart inside the same CH with no vmm in the loop.
- Decision CH59 rule 2 (PR D17, `landlock.rs`): the ruleset of a VM
  names its serial log, `<state root>/sessions/<id>/serial.log`, with
  write and truncate only. CH v54 opens it with `File::create`. The run
  dir rule has make socket, make file and write, and no truncate,
  because CH v54 makes `api.sock.lock` with no truncate
  (`vmm/src/locked_unix_listener.rs`). The ruleset opens the log, so
  lifecycle makes it with `SerialLog::fresh` (mode 0600, gap G23) before
  each launch, a boot and a restore alike. A missing log fails the
  launch with `RulesetError::Open`. The kvm job proves the rights: each
  boot of `tests/kvm_slot.rs` and `tests/kvm_vm.rs` writes its marker
  to the log under the ruleset.
- `deny::check` is the proof of deny-by-default (decision CH6-3). The
  first argument of the command line is the payload program, so a pid
  that still runs the launch step or `systemd-run`, or a CH zombie, fails
  with `NotCh`. The CH command line has no `--net` argument. A boot has exactly one
  `--vsock` and a restore none, because CH takes the devices of a restore
  from the snapshot. The check trusts that a snapshot comes only from a
  boot that passed the check (decision CH42 rule 2). The run dir holds
  exactly the `vsock.sock_<port>` listeners of the host ports that the
  vmm bound for the family: 1100, 1101 and 1102, plus 1104 for a family
  whose `egress.json` lists a host (decision CH49 rule 3, plan PR E2).
  The vmm takes that set from the listeners it bound at the start. Any
  failure is the fault `egress_assert_failed`.
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
  label only. CH reads a kernel, an initramfs or a root disk only as
  `store/*`. CH writes the serial log only as `sessions/*/serial.log`,
  with no read (decision CH59 rule 2, gap G23). The vmm writes the guest init initramfs into the state store
  as a content-named `store/<sha256>`, like the root disks, so that rule
  covers it (decision CH60 rule 2). The state root rules name the default
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
  the launch stage and CH. Each test runs its vmm stand-in as a transient
  user service (`tests/stand_in`, decisions CH60 rule 1, CH61), and each
  scope binds to that unit. The `ExecStopPost=` step of a stand-in kills
  every VM scope, as one vmm owns every VM, and systemd removes the run
  root at each stop of a stand-in (`RuntimeDirectory=vmm`). So one test
  at a time in a test binary writes in the run root (`OneVmm`). Each VM
  takes a slot through
  `RunRoot::link_vm`, under the real run root and the default state
  root. Each test has a control, and together they prove decision CH58
  rule 1:
  1. CH runs under `creche-ch-slot-<k>`, with no_new_privs set, the core
     limit 0 and the scope of its VM. CH with no chain runs unconfined.
  2. The ruleset lets CH read the root disk and write the session disk.
     It denies a write of the root disk and a read outside the VM files,
     at paths that the slot profile allows (decisions CH8-3, CH8-8). CH
     makes no TCP connect, and a slot label opens no inet socket, TCP or
     UDP (decision CH8-1, fact 77). Under the chain, AppArmor alone
     stops TCP: the slot profile refuses the socket before the Landlock
     connect rule runs. The unit test
     `landlock::tests::the_ruleset_denies_a_tcp_connect` in the job
     `rust` proves the Landlock TCP rule on its own.
  3. CH in one slot connects to no listener of another slot, to no
     `api.sock` or `vsock.sock` of a live CH in another slot, and not to
     `vmm.sock` or the user bus. One slot cannot signal the CH of another
     slot (decision CH51).
  4. The scope of CH has `BindsTo=` and `Before=` the vmm unit (CH61
     rule 2). Each stand-in copies the run root, restart, kill,
     `ExecStopPost=` and no_new_privs lines of
     `systemd/creche-vmm.service`, and a test
     in the job `rust` fails when they differ. The example in its role
     `hold` runs as the vmm unit and launches CH, with no_new_privs set
     before the launch. A stop of that unit ends CH and its scope after
     the stand-in has exited: CH still runs 1 s into the stop. A SIGKILL
     of the stand-in ends CH and its scope while the unit waits in
     `auto-restart`. A restart of the `hold` stand-in ends CH and its
     scope. In these three `hold` tests `BindsTo=` also stops the scope,
     and CH is a child of `hold`, so they prove the outcome of CH61 rule
     1, not the `ExecStopPost=` step. The step has its own proof, with
     the spawner in the test, which reads the signal 9 of CH. Without
     the step, the scope stop of `BindsTo=` sends SIGTERM, and CH exits
     with a code. A stop and a restart of a gated `/bin/sleep` stand-in
     each end CH by the SIGKILL of the step, after CH ran 1 s into the
     gated stop. After the restart the unit comes back. A crash of a
     `/bin/sleep` stand-in queues no stop job, so `Before=` orders
     nothing, and CH ends either by the SIGKILL of the step or by the
     SIGTERM of the `BindsTo=` scope stop, whichever comes first (CH61
     rule 1). The crash test accepts both. After each crash, while the
     unit waits in `auto-restart`, the test reads that the step ran in
     the unit and exited 0 (decision CH63), which proves the step there.
     The start part of a restart clears that run of the step, so no
     test reads it after a restart.
     After the restart and after the crash, the slot dir and the
     `vm/<id>` link of the last boot are gone with the run root, the
     serial log in the state root still holds the boot marker (decision
     CH59 rule 2), and a new launch of the same session boots. Each test
     has the control that CH runs while the stand-in
     runs. A drop of the spawner does not end CH, so no test claims it.
     `Launched::terminate` sends SIGTERM.
  5. The output of each CH reaches the sink tagged `ch <session id>`.
  `tests/kvm_boot.rs` runs the copy in `CRECHE_KVM_DIR` with no chain.
- `tests/kvm_vm.rs` runs one VM through the crate and the store (decision
  CH8-4, plan PR C16). The example `launch_chain` in its role `hold` plays
  the vmm with the real chain of `tests/kvm_slot.rs`: a cold boot in the
  scope with the C4 limits, under the slot label and the ruleset, a
  snapshot with dug holes, the key and the claim, a copy-on-write
  restore, a second snapshot, then a restore and a SIGKILL of the
  stand-in. Each restore passes `resume=false`, so the test waits for
  the paused VM, then resumes it, as the vmm does (decision CH50 rule
  10). Each start runs the stand-in as a transient user service,
  and the scope binds to it and is ordered before it (decision CH61
  rule 2). Each restore carries the `store/<sha256>` initramfs of the
  boot, the key holds its digest, and the ruleset reads it (decision
  CH61 rule 3). Step 5, the SIGKILL of the stand-in, proves that a crash
  ends CH and its scope while the unit waits in `auto-restart` (decision
  CH61 rule 1), with the control that CH runs while the stand-in runs.
  `BindsTo=` or the `ExecStopPost=` step ends CH there, and CH is a child
  of `hold`, so step 5 cannot tell them apart. On a crash the two race
  (no stop job, so `Before=` orders nothing). The step's exit status,
  read in `auto-restart`, proves that the step ran: step 5 reads that it
  ran in the unit and exited 0 (decision CH63). After the crash the
  `vm/<id>` link and the slot dir are gone with the run root, and the
  serial log in the state root still holds the ticks of the guest
  (decision CH59 rule 2). Each start takes a new slot
  with `RunRoot::link_vm` and runs `remove_stale`, as the start of
  `rust/crates/vmm/src/start.rs` does, so each restore moves the VM to
  another slot. The ruleset grants no right to remove a file, so CH
  cannot unlink its own sockets, and a stale `vsock.sock` would fail the
  next start (fact 75).

## Known gaps

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
- The kvm job builds CH and ch-remote on every run with the CH7-4 argv,
  and `bin/vm-smoke-setup.sh` fails unless CH prints `v54.0.0` (decision
  CH75, fact 78). `tests/kvm_vm.rs` makes the `CheckedCh` of the key from
  that CH, with no stand-in (decision CH36 G19).
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
- Closed by decision CH67 rule 5 (with CH23 rule 4, CH31 rule 3):
  `read_state` reads `creds.json` and `runtime.json` each with the cap
  `FAMILY_STATE_CAP`, and a larger file is `not_valid`, not
  `too_large`, because its excess may sit in fields that the reply
  drops.
- Closed by decision CH67 rule 6 (with CH23 rule 5, CH65 rule 2):
  `read_state` reads caregiver's files more strictly than the facade's
  file backend (`agent/src/family-state.ts`, `runtime-config.ts`,
  `creds.ts`). It refuses as `not_valid` an unknown key in `creds.json`
  or `runtime.json`, a `runtime.json` with no `shell` or
  `sandbox_tools`, a tool listed twice, and an entry of `skills/` that
  is not a dir. The file backend ignores or defaults each. caregiver
  writes none of these, so both backends give the same reply for every
  file caregiver writes.
- Closed by decision CH67 rule 4: the fd limit of the vmm, its headroom
  of 4096 and the refusal of a short hard limit (`nofile.rs`,
  `rust/crates/vmm/AGENTS.md` Known gap 21).
- Closed by decision CH70 rule 1: `EGRESS_CAP` is 64 KiB, room for
  about a thousand entries. A larger file is `ReachFault::TooLarge`, so
  the vmm allows nothing. Decision CH71 makes caregiver refuse a family
  whose file would pass the cap. `rust/crates/wire/vectors/family.json`
  holds the value, and `the_egress_cap_matches_the_wire_vectors` in
  `tests/family.rs` holds `EGRESS_CAP` equal to it.
- Closed by decision CH73 rule 2: `REPOS_CAP` is 4 KiB, the smallest
  cap that holds every list of decision CH69 rule 15 in the indented
  form that caregiver writes (32 names of 100 characters: 3476 bytes).
  So caregiver needs no cap check of its own, as decision CH71 needs
  for `egress.json`. A larger file is `ReachFault::TooLarge`, and the
  vmm serves no clone. `rust/crates/wire/vectors/family.json` holds the
  value, and `the_repos_cap_matches_the_wire_vectors` in
  `tests/family.rs` holds `REPOS_CAP` equal to it.
