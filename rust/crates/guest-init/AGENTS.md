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
  `nix` (CH13, CH14, CH62 rule 1), and `src/local_link.rs` listens on
  the Unix socket `creche_wire::guest::LOCAL_LINK_SOCKET`, mode 0600
  (CH62 rule 10), and `src/bridge.rs` listens on AF_VSOCK port 1201
  (CH13). Then `run()` writes `guest-init: up`. A listen fault
  powers off. One std thread accepts on each listener, one reads each
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
  `CONTROL_LINE_CAP`, is not one strict JSON text, or is not the type
  that the link expects at that point. A line of exactly the cap passes.
- Guest init checks each control line with `creche_wire::strict` before
  it reads the line as its type, as the vmm does (CH65 rule 2). So the two
  Rust ends of the link cannot drift.
- The facade (`src/supervisor.rs`, CH5d, CH19 rule 1): the clock set of
  the first hello starts it, and the ready of that hello says
  `starting` (CH21-8, CH25 rule 2). The facade is
  `creche_wire::guest::FACADE_PROGRAM`, with the parsed env plus
  `CRECHE_SESSION_ID` (CH49 rule 1, CH62 rule 6), cwd
  `creche_wire::guest::WORKSPACE` (made when absent), stdin and stdout
  /dev/null and stderr on the console (CH62 rule 26). It runs as root
  (CH8-1). A control link close never touches it. It runs in a process
  group of its own, so each SIGTERM and SIGKILL of guest init goes to its
  group and reaches its children too.
- PID 1 reaps each child in a reaper thread. When the facade pid is
  reaped, the state becomes `exited`, guest init sends SIGKILL to its
  group (`guest-init: facade pid <n> sent SIGKILL` when a child was
  left), and after `RESTART_PAUSE` (1 s, no cap) it starts the next one,
  so at most one runs, its children included (CH19 rule 1, CH62 rule
  8). `starts` counts each start. Each other reaped pid is an
  orphan and changes no state. The reaper waits with `WNOWAIT` and reaps
  under the spawn lock, so it never takes a child whose exec failed:
  std reaps that one inside `Command::spawn`.
- The local link (`src/local_link.rs`, CH21, CH32 rules 6 and 7, CH62
  rules 10 and 11): guest init keeps a peer only when its `SO_PEERCRED`
  pid is the live facade and that facade opened no link before. It
  closes each other peer at once, and the facade stays. The open moves
  the facade from `starting` to `running`, and it stays `running` until
  the reap. The framing is the one of control link 1200: each facade
  line is one `TurnEvent`, at most `CONTROL_LINE_CAP` bytes, and
  `creche_wire::strict` checks it before the typed read (CH65 rule 2). A
  refused line closes the local link. A close of the local link outside
  a stop sends SIGKILL to the facade group, and the restart follows the
  reap. The
  facade exit closes its local link too.
- Each turn event sets `turn_running`, and each end becomes the
  `last_turn_end` of every later state, across a restart too (CH32 rules
  4 and 5). Guest init relays each event on the control link after its
  ready as `{"turn": <event>}` (`GuestEvent::Turn`), then the facade
  line of the change. With no ready link, the event is dropped, and the
  next ready carries the last end (CH62 rule 11). The reader checks
  each line, not the order of starts and ends.
- The ACP row (`src/bridge.rs`, CH13, CH50 rule 2, CH62 rules 14 and
  16): an accept thread of its own takes each link of 1201 from `up`
  on, before the first hello and in every facade state. Each link dials
  `creche_wire::guest::RELAY_SOCKET` fresh. While nothing listens there
  (no socket file, or ECONNREFUSED), the dial tries again every 100 ms
  for up to 10 s after the accept (the vmm `READY_WAIT`), then guest
  init closes the link. Any other dial error closes it at once. The row
  holds at most 64 open links, from the accept to the close, and closes
  each link past that at once. No idle timeout closes a link. Each link
  writes `acp link <n> open`, `relay connected` or `relay dial failed:
  <error>`, then `closed`. A failed accept writes `acp accept failed:
  <error>` and powers off, as a failed accept of 1200 does.
- `src/splice.rs` copies the bytes of one link both ways on two std
  threads and reads none of them. An EOF, a failed read or a failed
  write in either direction shuts both sockets down in both directions,
  so a stale 1201 link after a restore closes only its own relay
  connection, and the facade keeps running (CH19 rule 1, fact 74).
- After the ready of a link, each facade state change goes out on it as
  `{"facade": <state>}` (`GuestEvent::Facade`, CH43 rule 8). A change
  before the ready goes out in that ready.
- After the ready, `stop` runs the stop of CH62 rule 9. With an open
  local link, guest init writes `{"stop":{}}` on it and waits up to
  `FACADE_STOP_WAIT` (25 s). Then it sends SIGTERM to the facade group
  and waits up to `SIGTERM_GRACE` (2 s). Then it powers off. A facade
  exit in either wait powers off at once. A facade with no open local
  link gets SIGTERM at once. With no live facade, the power-off comes at
  once. A second stop changes nothing.
- After the ready, `sync` runs `syncfs` of the session disk, then
  `sync`, and guest init answers `{"synced":{}}` (`GuestEvent::Synced`)
  on the same link (CH62 rule 23). The link stays ready. A sync of a
  replaced link changes nothing. A failed `syncfs` writes `guest-init:
  control link <n> sync failed: <error>`, sends no `synced` and keeps
  the link (CH65 rule 3).
- One thread, the main one, reads the event channel, runs the one timer
  of the supervisor and holds no sender. The two accept threads, the
  reaper thread and each reader thread hold one, so the channel ends
  only when all of them are gone. Then guest init writes
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
  (`kill(-1)`), a wait of up to `KILL_WAIT` (1 s) until PID 1 has no
  child left, sync, a read-only remount of the session disk, then
  `reboot(RB_POWER_OFF)` (CH19 rule 3, CH62 rule 9). The wait matters
  because `kill` only queues the signal: a killed process can still
  finish a write and holds its files until it exits. A wait that runs
  out writes `guest-init: processes left after 1s`. The remount flushes the ext4
  journal and marks the disk clean. Before the switch root it fails with
  its own console line. The guest kernel has
  `PANIC_TIMEOUT=0`, so an exit of PID 1 would hang the VM (fact 53). No
  fsck runs (CH62 rule 4).
- `examples/standin_facade.rs` is the stand-in facade of the kvm tests,
  a test program in no release row (CH62 rule 12). It writes one line on
  stdout and its session id, env names and cwd on stderr. With the mode
  `exit-once` in `/etc/creche-standin/mode`, its first start leaves a
  child in its process group, writes the child pid in a marker on the
  session disk and exits with status 3. The next start writes whether
  that child is gone. With the mode `holder`, it starts a child that
  leaves its group with `setsid`, so the SIGTERM of the stop misses it,
  and that child keeps a file of the session disk open for writing.
  With the mode `turn`, it opens the local link, sends one turn start
  and one turn end, starts a child of itself that opens the link too,
  and exits with status 0 on the stop line. With the mode `stream`, it
  opens the local link, writes turns on it with no pause and never
  reads the stop line. With the mode `close-once`, its first start opens
  the local link, closes it and stays up. With the mode `relay`, it
  listens on `RELAY_SOCKET` 2 s after its start, answers each line
  `ping` with `pong <pid>` and writes `standin: relay link ended` at
  the end of each link. Each other start stays up until a signal ends
  it.

## Tests

- The unit tests check the boot plan, the options of every mount, the
  netlink bytes, the env grammar, the control state machine, the
  supervisor state machine, the local link rules, the line framing, the
  splice, the relay dial bound, the row cap and the accept loop at the cap on any OS. On Linux, one more test checks the kernel flags
  that each set of options gives. One runs the real
  `playpen/bin/creche-env-write`, so the reader and the script cannot
  drift.
- `tests/kvm_guest_init.rs` boots CH with `$CRECHE_KVM_DIR/guest-init.cpio`
  of `bin/vm-smoke-setup.sh`, the cmdline `console=ttyS0 rdinit=/init`
  (CH62 rule 19), ext4 disks that `tests/common/mod.rs` builds with
  `mke2fs -d` (CH62 rule 12) and a vsock device. Each root disk holds
  `$CRECHE_KVM_DIR/standin_facade` at `FACADE_PROGRAM`. The tests dial
  the guest ports themselves: `CONNECT <port>` on the hybrid vsock
  socket of CH. On 1200 they speak the creche-wire types, and on 1201
  the `ping` lines of the stand-in. A test reads a console line that follows a
  link write with `wait_for_line`, because the guest writes that line
  after the link write and the tty can buffer it. After a stop, `dumpe2fs -h` and
  `e2fsck -fn` read the session disk. Only the `kvm` job runs them.

## Known gaps

- CONTRACT-QUESTION (splice EOF): CH62 rule 14 closes both halves of a
  reset link. No row decides an EOF. The splice follows plan entry G6
  and closes both halves on an EOF too, while the vmm `AcpRelay` shuts
  down only the write half of the other stream on an EOF.
