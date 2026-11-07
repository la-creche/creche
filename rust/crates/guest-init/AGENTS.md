# guest-init

The crate `creche-guest-init`: a library and the program `guest-init`,
PID 1 of each Cloud Hypervisor VM (decisions CH9, CH7-5). It is the fourth
program of the vmm row. Only the root `AGENTS.md` applies here.

## Layer rule

`creche-guest-init` calls `creche-wire` only. It depends on no vmm crate,
and no vmm crate depends on it. It runs blocking std threads, not tokio
(CH62 rule 2).

## The program

- Off Linux, and on Linux in a process that is not PID 1, `run()` starts
  nothing and returns exit status 78 (CH38).
- As PID 1, `run()` runs the boot plan of `src/boot.rs` (CH62 rule 4):
  devtmpfs, devpts and `/dev/shm`, the root disk `/dev/vda` ext4 ro and
  nodev, the session disk `/dev/vdb` ext4 rw, nosuid and nodev at
  `creche_wire::guest::SESSION_DISK_MOUNT`, tmpfs on `/tmp`, `/run`,
  `/root`, `/var/cache` and `/var/tmp`, proc and sysfs. Then `/dev` moves
  into the new root, the new root moves onto `/` (MS_MOVE) and guest init
  chroots into it. It stays PID 1. Last, `src/lo.rs` brings `lo` up with
  one RTM_NEWLINK message.
- Then `src/env_file.rs` reads `/etc/creche/env` of the switched root
  (CH7-7, CH62 rule 7) before any listener. The grammar is what
  `playpen/bin/creche-env-write` writes: `NAME=value` lines ended by LF,
  NAME `[A-Za-z_][A-Za-z0-9_]*`, the value any bytes but LF and NUL (a CR
  is a value byte), names in strictly ascending byte order, at least one
  line. One rule goes beyond the script: a 64 KiB cap. A refused file
  writes `guest-init: env refused: <reason>` and powers off, so the vmm
  dial fails. The parsed env becomes the facade env (CH49 rule 1).
- Then `src/control.rs` listens on AF_VSOCK port 1200 of any CID through
  `nix` (CH13, CH14, CH62 rule 1), and `run()` writes `guest-init: up`.
  A listen fault powers off. One std thread accepts, one reads each
  link, and the main thread owns the state and every write (CH62 rule
  2).
- The control link (CH19 rule 2, CH28, CH32): each accepted link gets a
  new id, and a new link closes the old one. A line or an EOF of an old
  link changes nothing. Guest init sends nothing before `ControlHello`.
  Each hello sets `CLOCK_REALTIME` to `sent_at`, a backward step
  included, and then guest init answers one `ControlReady` with the
  facade state (CH62 rule 25, CH34 rule 7). A failed clock set closes the
  link before the ready (CH62 rule 25).
  So does a hello for a session other than the first hello of the boot
  (CH32 rule 3).
- A refused line closes the link with no error line, and the console
  line names why (CH43 rules 1 and 2). A line is refused when it is over
  `CONTROL_LINE_CAP`, is not one JSON value, or is not the type that the
  link expects at that point. A line of exactly the cap passes.
- The facade (`src/supervisor.rs`, CH5d, CH19 rule 1): the clock set of
  the first hello starts it, and the ready of that hello says
  `starting` (CH21-8, CH25 rule 2). The facade is
  `creche_wire::guest::FACADE_PROGRAM`, with the parsed env plus
  `CRECHE_SESSION_ID` (CH49 rule 1, CH62 rule 6), cwd
  `creche_wire::guest::WORKSPACE` (made when absent), stdin and stdout
  /dev/null and stderr on the console (CH62 rule 26). It runs as root
  (CH8-1). A link close never touches it.
- PID 1 reaps each child in a reaper thread. When the facade pid is
  reaped, the state becomes `exited`, and after `RESTART_PAUSE` (1 s, no
  cap) guest init starts the next one, so at most one runs (CH19 rule 1,
  CH62 rule 8). `starts` counts each start. Each other reaped pid is an
  orphan and changes no state. The reaper waits with `WNOWAIT` and reaps
  under the spawn lock, so it never takes a child whose exec failed:
  std reaps that one inside `Command::spawn`.
- After the ready of a link, each facade state change goes out on it as
  `{"facade": <state>}` (`GuestEvent::Facade`, CH43 rule 8). A change
  before the ready goes out in that ready.
- After the ready, `stop` sends SIGTERM to a live facade and waits up to
  `SIGTERM_GRACE` (2 s) for its exit. Then guest init powers off (CH19
  rule 3, CH62 rule 9). With no live facade, the power-off comes at once.
  A second stop changes nothing.
- After the ready, `sync` runs `syncfs` of the session disk, then
  `sync`, and guest init answers `{"synced":{}}` (`GuestEvent::Synced`)
  on the same link (CH62 rule 23). The link stays ready. A sync of a
  replaced link changes nothing. A failed `syncfs` writes `guest-init:
  control link <n> sync failed: <error>`, sends no `synced` and keeps
  the link (CH65 rule 3).
- One thread, the main one, reads the event channel, runs the one timer
  of the supervisor and holds no sender. The accept thread, the reaper
  thread and each reader thread hold one, so the channel ends only when
  all of them are gone. Then guest init writes
  `guest-init: control events ended` and powers off.
- Each pass of that loop checks the timer before it reads the channel,
  so a steady stream of events cannot hold off the stop waits (CH62
  rule 9) or the restart pause (CH62 rule 8). The channel holds `INBOX`
  (64) events. A full channel blocks the sender: a reader thread then
  reads no more of its socket, so a fast peer fills only its own socket
  buffer. The reaper sends its reports after it drops the spawn lock,
  because `Facade::start` takes that lock on the control loop.
- Each step writes one `guest-init: <step>` line to the console (CH62
  rule 26). A fault writes `guest-init: <step> failed: <error>`.
- A stop, a boot fault, a refused env file, a listen fault and a panic
  in any thread all end in `power::off`. A panic hook, set first by
  `as_pid_1`, runs the default hook, writes `guest-init: panicked` and
  powers off. The power-off is: SIGKILL to every process left
  (`kill(-1)`), sync, a read-only remount of the session disk, then
  `reboot(RB_POWER_OFF)` (CH19 rule 3, CH62 rule 9). The remount flushes the ext4
  journal and marks the disk clean. Before the switch root it fails with
  its own console line. The guest kernel has
  `PANIC_TIMEOUT=0`, so an exit of PID 1 would hang the VM (fact 53). No
  fsck runs (CH62 rule 4).
- `examples/standin_facade.rs` is the stand-in facade of the kvm tests,
  a test program in no release row (CH62 rule 12). It writes one line on
  stdout and its session id, env names and cwd on stderr. With the mode
  `exit-once` in `/etc/creche-standin/mode`, its first start writes a
  marker on the session disk and exits with status 3. Each other start
  stays up until a signal ends it.

## Tests

- The unit tests check the boot plan, the options of every mount, the
  netlink bytes, the env grammar, the control state machine, the
  supervisor state machine and the line framing on any OS. On Linux, one more test checks the kernel flags
  that each set of options gives. One runs the real
  `playpen/bin/creche-env-write`, so the reader and the script cannot
  drift.
- `tests/kvm_guest_init.rs` boots CH with `$CRECHE_KVM_DIR/guest-init.cpio`
  of `bin/vm-smoke-setup.sh`, the cmdline `console=ttyS0 rdinit=/init`
  (CH62 rule 19), ext4 disks that `tests/common/mod.rs` builds with
  `mke2fs -d` (CH62 rule 12) and a vsock device. Each root disk holds
  `$CRECHE_KVM_DIR/standin_facade` at `FACADE_PROGRAM`. The tests speak control
  link 1200 themselves: `CONNECT 1200` on the hybrid vsock socket of CH,
  then the creche-wire types. A test reads a console line that follows a
  link write with `wait_for_line`, because the guest writes that line
  after the link write and the tty can buffer it. After a stop, `dumpe2fs -h` and
  `e2fsck -fn` read the session disk. Only the `kvm` job runs them.

## Known gaps

1. CONTRACT-QUESTION: CH65 rule 2 decides the strict reader. One wire
   lane PR moves the check `strict` of the vmm crate into creche-wire,
   and the vmm and guest init both read each vsock JSON line through it
   (rule 3 of "The vsock JSON links" in `wire/AGENTS.md`). Until that PR
   merges, the typed serde reader is the strict reader of the control
   link, because each of its types denies unknown fields and holds no
   free JSON value. A unit test holds that it refuses a key given twice,
   a second value, a float for an integer and an array for an object. A
   later commit on this branch calls the shared check in
   `src/control.rs` once the wire PR merges.
2. CONTRACT-QUESTION: no row decides the answer to a failed facade
   start, for example a root disk with no facade program. The reading
   taken, in `src/supervisor.rs`: guest init writes
   `guest-init: facade start failed: <error>`, counts no start, keeps
   the state and tries again after `RESTART_PAUSE` (CH62 rule 8), with
   no cap. The vmm sees no `starting`, so it never snapshots the VM. A
   change costs one arm of `Supervisor::start_failed` and its test.
