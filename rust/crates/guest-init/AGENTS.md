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
  included, and then guest init answers one `ControlReady` (CH62 rule
  25, CH34 rule 7). The facade state is `not_started` until plan PR G4.
  A failed clock set closes the link before the ready (CH62 rule 25).
  So does a hello for a session other than the first hello of the boot
  (CH32 rule 3).
- A refused line closes the link with no error line, and the console
  line names why (CH43 rules 1 and 2). A line is refused when it is over
  `CONTROL_LINE_CAP`, is not one strict JSON text, or is not the type
  that the link expects at that point. A line of exactly the cap passes.
- Guest init checks each control line with `creche_wire::strict` before
  it reads the line as its type, as the vmm does (CH65 rule 2). So the two
  Rust ends of the link cannot drift.
- After the ready, `stop` powers the VM off (CH19 rule 3).
- After the ready, `sync` runs `syncfs` of the session disk, then
  `sync`, and guest init answers `{"synced":{}}` (`GuestEvent::Synced`)
  on the same link (CH62 rule 23). The link stays ready. A sync of a
  replaced link changes nothing. A failed `syncfs` writes `guest-init:
  control link <n> sync failed: <error>`, sends no `synced` and keeps
  the link (CH65 rule 3).
- One thread, the main one, reads the event channel and holds no
  sender. The accept thread and each reader thread hold one, so the
  channel ends only when all of them are gone. Then guest init writes
  `guest-init: control events ended` and powers off.
- Each step writes one `guest-init: <step>` line to the console (CH62
  rule 26). A fault writes `guest-init: <step> failed: <error>`.
- A stop, a boot fault, a refused env file, a listen fault and a panic
  in any thread all end in `power::off`. A panic hook, set first by
  `as_pid_1`, runs the default hook, writes `guest-init: panicked` and
  powers off. The power-off is: sync, a read-only remount of the session disk,
  then `reboot(RB_POWER_OFF)` (CH19 rule 3). The remount flushes the ext4
  journal and marks the disk clean. Before the switch root it fails with
  its own console line. The guest kernel has
  `PANIC_TIMEOUT=0`, so an exit of PID 1 would hang the VM (fact 53). No
  fsck runs (CH62 rule 4).
- `examples/standin_facade.rs` is a stub that calls `run()`.

## Tests

- The unit tests check the boot plan, the options of every mount, the
  netlink bytes, the env grammar, the control state machine and the line
  framing on any OS. On Linux, one more test checks the kernel flags
  that each set of options gives. One runs the real
  `playpen/bin/creche-env-write`, so the reader and the script cannot
  drift.
- `tests/kvm_guest_init.rs` boots CH with `$CRECHE_KVM_DIR/guest-init.cpio`
  of `bin/vm-smoke-setup.sh`, the cmdline `console=ttyS0 rdinit=/init`
  (CH62 rule 19), ext4 disks that `tests/common/mod.rs` builds with
  `mke2fs -d` (CH62 rule 12) and a vsock device. The tests speak control
  link 1200 themselves: `CONNECT 1200` on the hybrid vsock socket of CH,
  then the creche-wire types. A test reads a console line that follows a
  link write with `wait_for_line`, because the guest writes that line
  after the link write and the tty can buffer it. After a stop, `dumpe2fs -h` and
  `e2fsck -fn` read the session disk. Only the `kvm` job runs them.
