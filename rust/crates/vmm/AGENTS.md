# vmm

The crate `creche-vmm`: a library and the program `vmm` (decisions CH9,
CH7-5). One `vmm` process owns every Cloud Hypervisor VM of the playpen.
The root `AGENTS.md` applies here too.

## The three crates

| Crate | Directory | Calls |
|---|---|---|
| `creche-vmm` | `rust/crates/vmm` | `creche-vmm-confine`, `creche-vmm-store`, `creche-wire` |
| `creche-vmm-confine` | `rust/crates/vmm-confine` | `creche-vmm-store`, `creche-wire` |
| `creche-vmm-store` | `rust/crates/vmm-store` | `creche-wire` |

- `creche-vmm` composes two libraries: `creche-vmm-confine` and
  `creche-vmm-store`. `creche-vmm-confine` reads the types of
  `creche-vmm-store`.
- A crate calls only the crates in its row. For these three crates, this
  table replaces the layer rule of the root `AGENTS.md`.
- Each `Cargo.toml` declares every path edge of the table. A change adds
  no path edge.

## The program

`src/main.rs` documents the modes of `vmm` and the exit status 78.

## The server

`src/server/` holds the host API server (decisions CH20, CH26).
`server::run_with` accepts on a socket path that its caller passes in and
serves one HTTP/1.1 connection per accept, with upgrades on. The protocol
header is the first check of each request (decisions CH33 rule 1, CH34
rule 1). `server::run` serves no route: it answers each request that
passes with `no_route`. `Stop` stops the server through `tokio::sync`
(decision CH26 rule 2).

`server::run_routes` serves the one route table (decision CH25). A
handler set implements `Serve<R>` of `creche-wire` for each JSON route
and `ServeUpgrade<R>` for each Upgrade route. `creche_vmm::routes!` then
expands `route_table!` for it, so a set that misses a route does not
build. The router checks the route and the method, then the `Upgrade`
header of an Upgrade route, then the body, then the request type
(decisions CH26 rule 3, CH33 rule 13, CH34 rule 7). It answers 101 only
after the handler admits the request. A `server::Relay` then owns the
stream.

After hyper ends a connection that it did not upgrade, the server shuts
its write half, then reads and drops request bytes until the client
closes, `LINGER_BYTES` or `LINGER_TIME` (`src/server/accept.rs`). A
refusal can come before the server reads the body. Without this step,
the close with bytes unread resets the socket on Linux, and a client that
writes the whole request before it reads loses its typed reply (decision
CH26).

`server::BodyCheck` holds the body checks that follow the route, in the
order of decisions CH26 and CH33 rule 13: `Content-Type`, then the 1 MiB
cap, then strict JSON. Each refusal writes the reply of a shared code.
The cap binds the JSON body only (decision CH34 rule 3).

`examples/upgrade_echo/` is a server with one handler that echoes after
101. `tests/upgrade_body.rs` and `integration/proc/test_proc_vmm_upgrade.py`
use it to prove that the JSON body of an Upgrade request crosses
(`wire/AGENTS.md`, the `acp` route).

## The control link

`src/control.rs` opens the control link to guest init on guest port 1200
(decisions CH13, CH14). `control::open` dials through the vsock socket
of the VM (`VmRunDir::vsock_socket` of `creche-vmm-store`). It writes
`CONNECT 1200` and waits for `OK`. It retries on ENOENT and on an empty
reply, because both come before the guest listener exists (fact 74). It
also retries on ECONNREFUSED (decision CH43 rule 7). `DIAL_PAUSE` (10 ms,
decision CH43 rule 5) is the pause between two tries. It
then sends `ControlHello` with the wall time of the vmm (decision CH28)
and reads one `ControlReady` with the strict reader. `READY_WAIT` (10 s,
decision CH36 G26) bounds the whole open. A close before `ControlReady`
is a fault of the VM: `ControlError::needs_cold_boot` is true (decision
CH32 rule 3). Decision CH43 rule 6 allows a cold boot only when guest
init closes the link before `ControlReady`. The rule names four cases
that end the open with a reported fault and no cold boot: a bad line, a
timeout, a refused `CONNECT` and a failed dial. The rule does not name
`Io` (a failed read or write) or `Clock`. The code reads "only" to cover
every other error too, so each of them also ends the open with a
reported fault and no cold boot.

`control::ControlState` keeps the current link and the last facade state
of each VM. Each change names its `LinkId`, and only the current link
changes the state. So the late EOF of a replaced link changes nothing
(decision CH19 (2), fact 74). A link close keeps the facade state,
because it never stops the facade (decision CH19 (1)).

After `ControlReady`, `ControlLink::event` reads each turn event that
guest init relays (decision CH21). `ControlState::turn` applies each
event in memory: a start marks a turn running, and an end clears the
mark. `ControlState::ready` keeps the facade state of each
`ControlReady`. Both return a `Change`. `Change::Applied` holds the turn
end, if any, and the caller writes it with `Rows::turn_end` of
`creche-vmm-store`. That write sets `updated_at`, and it sets the title
only while the row has none (decision CH32 rule 4). So an end that a
restore dropped still arrives through the next ready (fact 45). The
write syncs a file and a dir, so the caller runs it in `spawn_blocking`
(decision CH36 G21). A close of the link after `ControlReady` ends
`event` with `ControlError::Ended`, which asks for no cold boot.
`ControlState::turn_running` is the turn state for the sleep gate
(decision CH19 (6)). The vmm keeps the facade state of the last ready,
and turn events set `turn_running` (decision CH43 rule 8).
`ControlLink::stop` sends the graceful stop
`{"stop":{}}` (decision CH19 (3)).

## The acp route

`src/handlers/acp.rs` admits or refuses an `acp` Upgrade before 101, in
the order of decision CH34 rule 7. It reads the row first (`not_found`
404 for an unknown id or another family, decisions CH21-9, CH33 rule
5). Then it claims the session in the relay registry (`busy` 409,
decision CH33 rule 7) and reads the row again. So another family never
touches the claim of the owner. Then the `StartVm` hook starts the VM: admission, the
boot or restore, and the first `ControlReady`, whatever facade state it
reports. Each `StartFault` is a typed code before 101. An unknown id
never reaches the hook, so it gets no boot. After `ControlReady` the
hook dials guest port 1201 with `AcpLink::dial` and returns the link, so
a failed dial is `sandbox_start_failed` before 101 (decision CH50 rule
2). A drop during the start or the dial gives `not_found`, even when the
drop made the dial fail. `start::ColdBoot` is the hook of the vmm.
`tests/acp.rs` fakes it, and a fake vsock socket plays guest port 1201.

`src/relay.rs` holds the relay registry with a stop signal (decision
CH26 rule 5). `Relays::claim` holds a session for one Upgrade until the
relay ends. `Relays::mark_drop` stops the open relay of a session and
marks the drop. An Upgrade under the mark gets `not_found` (decision
CH34 rule 6), and so does one whose start a drop overlapped. The drop
keeps its mark until the row is gone. The second row read comes after
the claim, so a drop that ends between the first read and the claim
leaves no row, and the Upgrade gets `not_found`.

`AcpLink::dial` dials guest port 1201 with `control::dial`, the dial of
the control link (decision CH13). It pauses 10 ms between two tries and
retries ECONNREFUSED, and `READY_WAIT` bounds it (decision CH43 rules 5
and 7). After 101, `AcpRelay` copies bytes both ways over that link and
sends the bytes that came after `OK` first (decision CH20). An EOF
in one direction shuts down the write half of the other stream, and the
other direction runs on, as with `copy_bidirectional`. The relay ends
when both directions reach EOF, a read or write fails, or the stop
signal fires. Then it drops both streams. It
parses no ACP byte and caps none (decision CH34 rule 3). `Relays::door`
gives the door of a session for the sleep gate (decision CH19 (6)): open
from the claim to its end, then the time that the last admitted Upgrade
ended. A refused Upgrade records no close, so the map of close times
grows only with sessions that ran. A drop clears the time.

## The stop

`src/stop.rs` stops one VM (decisions CH19 (3), CH34 rule 5). It sends
`stop` on the control link and waits `DROP_STOP_WAIT` (30 s) for Cloud
Hypervisor to exit. Then it sends SIGTERM through the `Launched` handle
of `creche-vmm-confine` and waits `DROP_SIGTERM_GRACE` (5 s). Then it
sends SIGKILL and waits `KILL_WAIT` (1 s, decision CH50 rule 4) at
most, so a stop ends within 36 s. A process that outlives it gives
`StopError::Hung`, and its files stay. The first two waits come from
`creche-wire`. A kill is a power cut
for the guest, and no error: `Stopped::Killed`. After the exit it runs
`remove_stale` of `creche-vmm-store` in `spawn_blocking`, which removes
`api.sock.lock` (fact 75) and the `vsock.sock` that a kill leaves. A stop
takes no snapshot, so the next start is a cold boot (decision CH19 (5)).
A step that did not go out skips its wait: no open control link, a
failed or stalled write of `stop`, or a failed SIGTERM (decision CH50
rule 3). The write of `stop` has a timeout counted inside
`DROP_STOP_WAIT`, so SIGTERM follows 30 s after the start at most.
`StopLink` and `VmProcess` let `tests/stop.rs` give fakes on paused time.

## The drop route

`src/handlers/drop.rs` serves `drop` (decisions CH21-2, CH21-10,
CH27). A session drop of an absent id, or of an id that another family
owns, gets `not_found` and stops nothing (decision CH33 rules 4 and 5).
A family drop takes the rows of the family, and a family with no rows
answers `Dropped`. Then the drop runs in this order:

1. `Relays::mark_drop` marks each session. The mark stops its open
   relay (decision CH26 rule 5), and an Upgrade under the mark gets
   `not_found` (decision CH34 rule 6).
2. `DropMark::claim_ended` waits for the Upgrade that the mark stopped.
   Its start may still boot a VM, so the drop waits for that start to
   end. Then no Upgrade starts a VM of the session under the mark.
3. The `StopVm` hook stops every VM of the target at once (decision
   CH33 rule 11). The hook of the vmm calls `stop::stop`. The tests fake
   it.
4. `remove_session` of `creche-vmm-store` removes the data of each
   session whose VM stopped, then its row (PR D14). It runs in
   `spawn_blocking`, one row at a time. The lock of the rows lasts one
   removal, so the other handlers get it between two removals (decision
   CH36 G21).
5. The marks drop after the removal, so they last until the row is gone.

A stop that fails gives `internal` 500 (decision CH50 rule 4). The row
and the files of that session stay, so a retried drop finishes the job.
`StopError::Hung` is one such stop. The drop logs the pid of each VM
that did not stop. A removal that fails gives `internal` too, and its
row stays. `tests/drop.rs` runs the handler on paused time with fake
VMs. It proves two drops that overlap a start, a family drop with one
hung VM, a failed removal, and marks that last until the row is gone.

## The cold boot

`src/start.rs` holds `ColdBoot`, the `StartVm` hook of the vmm for a
session with no usable snapshot (plan PR L16, decisions CH1, CH8-2). It
keeps one table of VMs, one per session. A VM that runs gets a new dial
of guest port 1201 and no boot. A VM whose process exited leaves the
table, and the start boots it again. A cold boot runs these steps:

1. The family VM shape (`FamilyRoot::read_shape`) and the free bytes of
   the state root, in `spawn_blocking` (decision CH50 rule 9).
   Admission needs the guest RAM of the shape, so this read comes before
   admission.
2. `admission::decide` (decisions CH8-6, CH8-7). The guest RAM of the
   start counts in the table from here on, so two starts never pass the
   cap together. `host_full` writes no fault. Then each eviction that
   admission names runs (see "The eviction").
3. In `spawn_blocking`: `remove_stale` (decision CH19 (4)), the store
   check of the kernel and the root disk of the flavor (decision CH44
   rule 2), `SessionDisks::for_boot` (decision CH21-4), the run dir
   `vm/<id>/` and `snapshots/` with mode 0700, and a new `serial.log`
   with mode 0600 (gap G23). Then the restore or cold boot choice (see
   "The restore"), and the launch with `ChCommand::boot` or
   `ChCommand::restore`, `VmLimits` and the Landlock files of the VM
   (decisions CH8-3, CH8-5, CH8-8, CH30).
4. `control::open` for the first `ControlReady`. It keeps the facade
   state in `ControlState`, writes a turn end to the row, and clears the
   faults of the family (contract 05 §3.3.1 rule 8).
5. `deny::check` with an empty bound set. Decision CH49 rule 3 binds
   1104 from phase 7 on.
6. `AcpLink::dial` of guest port 1201 (decision CH50 rule 2).

Each fault but `host_full` goes to the fault file of the family
(decisions CH6-3, CH21-1). A fault after the launch stops the VM with
`stop::stop`. After `ControlReady` the stop sends the graceful stop on
the control link first (decision CH19 (3)). Before it no link exists,
so the stop goes on to SIGTERM (decision CH50 rule 3). A VM whose stop
failed stays in the table, so admission counts its RAM (decision CH50
rule 4). A start that is dropped before its end sends SIGKILL to its VM
and keeps the VM in the table for the same reason. The launch step puts
the process in the start's reservation inside `spawn_blocking`, so a
start that hyper drops during the launch does the same. Admission first
removes each VM whose process exited, so an exited VM holds no RAM.
The `Launcher` trait
lets `tests/start.rs` fake the launch, the command line file and the
guest. Every other step there is the real one on a scratch dir.

## The restore

`src/start.rs` restores a session from its newest snapshot when it can
(plan PR L17, decision CH2). `StartConfig::ch_version` is the
`CheckedCh` of the version check at vmm start (gap G10). With the
checked kernel and root disk it gives the current `SnapshotKey`.

1. `check_newest` of `creche-vmm-store` compares the key of the newest
   generation. A mismatch or an unreadable key drops every generation,
   and the VM cold-boots (decisions CH7-6, CH44 rule 5).
2. `claim` marks a keyed snapshot consumed before CH starts (decision
   CH19 (5)). A consumed mark, or a retired claim, gives a cold boot. A
   claim that cannot retire fails the start (decision CH44 rule 6).
3. The key check and the claim run under the lock of the `InUse` set.
   The generation stays in the set from the key check until CH ends:
   the VM leaves the table, or its start ends with no process
   (decision CH50 rule 8). `ColdBoot::in_use` gives the set to the
   sweep.
4. `ChCommand::restore` runs with `resume=false`. Then
   `restore::ChApi::info` is read until CH answers, inside
   `READY_WAIT`. `restore::check_devices` requires `state` `Paused` and
   refuses `net`, `generic_vhost_user`, `fs`, `user_devices`, `vdpa`,
   `devices`, `pmem` and `tpm`, any disk but the root disk and then the
   session disk, and a vsock socket other than the one of the VM. Then
   `ChApi::resume`, then the wait for `ControlReady` (decisions CH42
   rule 2, CH50 rule 10). `INFO_TIMEOUT` and `RESUME_TIMEOUT` (5 s each)
   bound each call (decision CH44 rule 3).
5. The deny check runs as for a boot. A restore has no `--vsock`
   argument.

`ChApi::READS_DEVICES` is false for `restore::ChTools`, the API of the
vmm, so the vmm restores no VM yet: the claim consumes the snapshot, and the start cold-boots
(Known gaps). `tests/start.rs` gives a fake API that reads the device
set.

## The eviction

When admission answers `EvictFirst`, `ColdBoot` takes each named VM out
of the table and marks its slot `Evicting`, so its RAM still counts
(plan PR L17, decision CH8-6). Then, one VM at a time in the order of
admission:

1. `Relays::close` stops the open door link of the VM and waits for its
   claim to end. The client reconnects with `session/load` (decision
   CH19 (6)).
2. A VM with a turn running goes back, and the start gets `host_full`.
3. `ChApi::snapshot` takes the snapshot and writes its key
   (`sleep::take_keyed`, PRs D9 and D10). `sleep::EVICT_SNAPSHOT`
   bounds each `ch-remote` call of the take: pause 5 s, snapshot 60 s
   and `shutdown-vmm` 10 s (decisions CH44 rule 3, CH50 rule 1). The
   take ends CH. A failed take is logged, and the stop follows anyway
   (decision CH44 rule 4).
4. `stop::stop` with the control link of the VM. A VM that does not
   stop goes back with no link, so admission counts its RAM (decision
   CH50 rule 4), and the start gets `host_full`. The VMs left in the
   list go back too.

The gate of each VM is `Activity::may_evict` of `creche-vmm-store`, not
`may_sleep` (PR D12). Only a VM with a control link and a snapshot key
may be evicted. Its last use is the later of its start and its last
door close, and an open door counts as used now (decision CH50 rules 5
and 6). An Upgrade of a session under eviction gets `host_full`.
`tests/start.rs` proves the order, the door close and `host_full`.

## Rules

- Each new dependency is its own pull request, with its lock delta
  (decision CH9-1).
- A library returns a typed error. Only `src/main.rs` turns an error into
  an exit status.
- A crate exposes `pub` items only for the crates that list it in their
  row (decision CH36, gap G24 of the phase 2 plan).

## Known gaps

1. CONTRACT-QUESTION (plan PR L15, `src/handlers/drop.rs`): a drop
   waits for a start that its mark overlapped, and the start has no
   outer bound (decision CH50 rule 1). So a drop that overlaps a long
   start can pass the "about 35 s" of decision CH33 rule 11 and the 60 s
   wait of the client (decision CH34 rule 5). The client retries, and
   the retry waits for the same start. No row bounds this wait. The
   narrowest reading waits, because a drop must not remove the files of
   a VM that a start then boots.
2. Decision CH43 rule 8: no line on the control link reports a change
   of the facade state yet. The phase 3 guest init PR adds a line that
   reports each change. Until then the sleep gate treats a VM as up only
   after a ready said `running` or a turn event arrived. A VM after a
   cold boot reports `not_started` or `starting` (decision CH34 rule 7),
   so a gate that reads only the ready would never snapshot it. The sleep
   gate is plan PR L18, a later PR, so this crate does not hold the rule
   yet.
3. CONTRACT-QUESTION (plan PR L16, `src/start.rs`): decision CH6-3 runs
   the deny check "before the VM is ready". The plan puts it before the
   wait for `ControlReady`. Until CH runs, the command line is the one
   of the launch step, and the check fails closed. No row bounds a wait
   for the exec of CH. The code reads "ready" as the 101: the check runs
   after `ControlReady` and before the dial of 1201. A poll of the
   command line inside `READY_WAIT` before the open costs about 15
   lines.
4. CONTRACT-QUESTION (decisions CH19 (6), CH21-3, `src/start.rs`): no
   row names the reply to an Upgrade of a session under eviction. The VM
   still holds its RAM, so the code answers `host_full`, and attendance
   retries. A door that an eviction closes between the end of the start
   and the 101 gives that Upgrade `not_found`, because the handler reads
   a stopped claim as a drop.
5. `ColdBoot` keeps the control link of each VM, and no code reads its
   turn events yet. The `StopVm` hook of the vmm and its use of the
   table of `ColdBoot` are not built yet either.
6. CONTRACT-QUESTION (decision CH50 rule 10, plan PR L17,
   `src/restore.rs`): the plan reads the device set "through ch-remote
   info (D8)". `ChRemote::info` returns `VmInfo`, which holds only
   `state`, and this crate may not edit `creche-vmm-store`. So
   `READS_DEVICES` is false for `ChRemote`, and no VM restores. A store
   PR that gives the whole `info` reply costs about 10 lines there and 3
   lines here.
7. CONTRACT-QUESTION (decision CH50 rule 10, `src/start.rs`): no row
   names the fault of each restore step. The code reads a VM that is not
   `Paused`, or that has a refused device, as `egress_assert_failed`
   (decisions CH6-3, CH42 rule 2). An `info` that never answers, or a
   failed resume, is `sandbox_start_failed`.
8. Decision CH43 rule 6: a guest that closes the link before
   `ControlReady` asks for a cold boot. After a restore the start does
   not cold-boot then. It faults, stops the VM, and the next start
   cold-boots, because the claim consumed the snapshot.
9. Item 5.7 of the design asks for a guest `sync` before each snapshot
   (`creche-vmm-store` Known gaps). The control link has no `sync`
   line yet, so an eviction takes its snapshot with no guest sync.
10. No code reads the turn events of a VM yet (gap 5), so the eviction
    gate passes `turn_seen` false (decision CH43 rule 8). A VM whose last
    ready did not say `running` is never evicted until plan PR L18 adds
    the flag.

## Caller duties

1. Decision CH50 rule 1: the start of an `acp` Upgrade has no outer
   bound, and the handler adds none around the `StartVm` hook
   (`src/handlers/acp.rs`). Each step keeps its own bound.
   `sleep::EVICT_SNAPSHOT` bounds the snapshot of one eviction. The
   bound of a start grows with the number of VMs that its admission
   evicts.
2. Decision CH50 rule 2: the `StartVm` hook of plan PR L16 dials guest
   port 1201 with `AcpLink::dial` before it returns. It answers a failed
   dial with `StartFault::SandboxStartFailed` and writes that fault to
   the fault file (decision CH21-1). The handler writes no fault. The
   rule holds only if guest init accepts 1201 in every facade state.
   Phase 3 confirms that. If guest init refuses 1201 until the facade
   runs, 101 waits for the facade, against decision CH34 rule 7, and
   David decides again.
3. Decision CH50 rule 4: the drop route maps `StopError::Hung` to
   `internal` 500, keeps the row and logs the pid
   (`src/handlers/drop.rs`). The `StopVm` hook of the vmm keeps a VM
   whose stop failed in its table, so admission keeps counting the RAM
   of that VM.
4. Plan PR L20 runs `check_cloud_hypervisor` at vmm start and passes
   its `CheckedCh` as `StartConfig::ch_version`. A failed check refuses
   to start the vmm (gap G10 option A).
5. Plan PR L18 runs its sweep under the lock of the `InUse` set and
   names each generation of `ColdBoot::in_use` in it (decision CH50 rule
   8).
6. Two drops of one session can overlap. The `StopVm` hook of the vmm
   makes a stop that finds a stop of the same VM running wait for it
   and return its outcome. So no drop removes the files of a VM that
   still runs.
