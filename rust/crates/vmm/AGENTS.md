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
`vmm launch-stage <plan>` takes exactly one plan argument and calls
`creche_vmm_confine::stage::run`, the one entry of the stage. It always
writes the real exec attribute (plan PR L16c, decisions CH55, CH58 rule
1).

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

`ControlLink::split` splits a link after `ControlReady`: a reader of the
turn events and a `StopSender` for the graceful stop (plan PR L18b).
`ColdBoot` splits the link when a start commits its VM. The table keeps
the sender, and one task per VM runs the reader. The reader passes each
event to `ControlState::turn` and writes each turn end with
`Rows::turn_end` in `spawn_blocking`, as for a ready (decision CH32 rule
4). A refused line ends the reader (decision CH43 rule 2). So does the
close of the link, or an event of a replaced link. The reader holds the
only strong handle of the stream, so its end closes the link. The sender
then sends nothing, and a stop skips the graceful step (decision CH50
rule 3). A VM whose link closed has no control link for the sleep gate,
so it never sleeps and is never evicted.

Decision CH54 rule 6: a refused line is a guest fault. The reader logs
the line with the session id (`ControlLink::next_event` gives its first
`REFUSED_LOG_CAP` bytes, 512). Then it stops the VM at once through the
stop path of the `StopVm` hook (`Stopper`). The link is gone, so the
stop sends SIGTERM, then SIGKILL. The VM leaves the table and frees its
RAM, so the next start of the session cold-boots (decision CH19 (5)).
A reader whose link is no longer current stops nothing. The stop names
the link too (`Vm::link_id`): it takes only the VM of that link, and a
later VM of the session runs on. `tests/start.rs` proves the events, the
row write, the gate, the stop after a refused line, a stale reader, and
a new VM of the session that the old reader leaves alone.

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

## The create and list routes

`src/handlers/create.rs` serves `create` (plan PR L7, decisions CH21-4,
CH25 rule 2). It first reads the family with `FamilyRoot::exists` of
`creche-vmm-confine`. A family with no `config/` dir gets
`unknown_family` 404 (decision CH33 rule 3). Then `Rows::create` of
`creche-vmm-store` makes the row. The same typed request gives the same
row, and a different one gives `conflict` 409 (decision CH33 rule 12).
The family read can sleep between its tries (decision CH50 rule 9), and
the row write syncs a file and a dir. So both run in `spawn_blocking`
(decision CH36 G21). The first family read holds no lock of the rows.
caregiver removes the family files, then drops the family (decision
CH33 rule 3). So create reads the family a second time under the lock
of the rows, before the write. A drop of the family then sees the new
row, or that read sees no family, and no row outlives its family. A
create makes no session disk and boots no VM: the first boot makes the
disk (decision CH21-4).

`src/handlers/list.rs` serves `list` (decisions CH21-5, CH21-7). It
answers `Rows::page` from memory, on the runtime worker, so a list boots
no VM. The request type requires `family` (decision CH34 rule 4). A page
ends early under the 1 MiB cap with a `next` (decision CH33 rule 2).
`tests/create_list.rs` proves a repeat create, a conflict, an unknown
family, a repeat after the family went, `internal` for a failed family
read or row write, and a page of 4096-character cwds that ends early.

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
answers `Dropped`. The drop runs in a task of its own, and the handler
waits for that task. hyper drops the handler future when the client
hangs up, because `src/server/accept.rs` leaves half-close off. The task
runs on with its marks, so a retry joins the live slot and waits for the
same start. The drop runs in this order:

1. `Relays::mark_drop` marks each session. The mark stops its open
   relay (decision CH26 rule 5), and an Upgrade under the mark gets
   `not_found` (decision CH34 rule 6).
2. `DropMark::claim_ended` waits for the Upgrade that the mark stopped.
   Its start may still boot a VM, so the drop waits for that start to
   end. Then no Upgrade starts a VM of the session under the mark.
3. The `StopVm` hook stops every VM of the target at once (decision
   CH33 rule 11). `ColdBoot` is the hook of the vmm (see "The stop
   hook"). `tests/drop.rs` fakes it.
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
VMs. It proves two drops that overlap a start, a retry after a drop
whose client hung up, a family drop with one hung VM, a failed removal,
and marks that last until the row is gone.

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
2. `admission::decide` (decisions CH8-6, CH8-7, CH58 rule 2). The guest
   RAM of the start counts in the table from here on, so two starts never
   pass the cap together. The start also takes the lowest free slot, or
   with none free the slot of its first eviction (see "The slots"). `host_full` writes no fault. Then each eviction that
   admission names runs (see "The eviction").
3. In `spawn_blocking`: `RunRoot::link_vm` links `vm/<id>` to the slot
   (PR D16), then `remove_stale` (decision CH19 (4)), the store check of
   the kernel and the root disk of the flavor (decision CH44 rule 2),
   `SessionDisks::for_boot` (decision CH21-4), `snapshots/` with mode
   0700, and a new `serial.log` with mode 0600 (gap G23). Then the
   restore or cold boot choice (see "The restore"), and the launch with
   `ChCommand::boot` or `ChCommand::restore`, `VmLimits`, the Landlock
   files of the VM and `Launch::in_slot` (decisions CH8-3, CH8-5, CH8-8,
   CH30, CH55).
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

## The slots

Plan PR L16b gives each VM one slot of the pool of
`creche_vmm_store::layout::SLOTS` (64, decisions CH51, CH55). Each entry
of the table holds its slot: a start, a VM that runs, a VM under
eviction or under a stop, and a VM whose stop failed. Admission takes
the lowest slot that no entry holds, under the table lock, so two starts
never take one slot. A full pool counts like a full RAM cap (decision
CH58 rule 2): with no free slot, admission evicts the least recently used
VM that `may_evict` passes (decision CH8-6), and the start takes the slot
of its first eviction. With no such VM, the start gets `host_full`
(decision CH21-3) and launches nothing. An evicted VM holds its slot as
`Evicting` until its CH ends. The start links that slot only after each
eviction ended, and a failed eviction fails the start, so no two live CH
share a slot. With a free slot, a start that evicts for RAM takes the
free slot.

The start runs `RunRoot::link_vm` before `remove_stale`, and passes the
slot to the launch with `Launch::in_slot` (PR C18). So the launch stage
enters the slot profile `creche-ch-slot-<k>`. A restore links the
`vm/<id>` of its snapshot to its new slot, and each path in the snapshot
holds. `link_vm` empties the slot (decision CH56 rule 1), so the start
takes only a slot that no live CH holds (caller duty (c) of PR D16).

An entry leaves the table only after its CH ended: a stop, a drop, the
stop after a guest fault, an eviction, an idle sleep, a shutdown, a start
that failed, or a VM that exited. Each of these runs `release`. It runs
`RunRoot::unlink_vm` under the table lock before the entry goes, so no
start of the session links before the unlink. A failed unlink is logged,
and the slot still frees. `tests/start.rs` proves two slots, a reused
slot, a full pool of busy VMs, a full pool that evicts its idle VM, each
stop path and a restore into a new slot.

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
3. The key check and the claim run under the lock of the claims, which
   the sweep holds too.
   The generation stays in the set from the key check until CH ends:
   the VM leaves the table, or its start ends with no process
   (decision CH50 rule 8). `ColdBoot::in_use` gives the set to the
   sweep.
4. `ChCommand::restore` runs with `resume=false`. Then
   `restore::ChApi::info` is read until CH answers, inside
   `READY_WAIT`. A reply that is not a `VmInfo` ends the wait at once.
   `restore::check_devices` reads the `VmInfo` of `creche-vmm-store`
   (PR D15). It requires `state` `Paused` and refuses `net`,
   `generic_vhost_user`, `fs`, `user_devices`, `vdpa`, `devices`, `pmem`
   and `tpm`, a disk with `vhost_user` or a `vhost_socket` (CH v54 opens
   the socket and ignores the path), any disk but the root disk and then
   the session disk, and a vsock socket other than the one of the VM. Then
   `ChApi::resume`, then the wait for `ControlReady` (decisions CH42
   rule 2, CH50 rule 10). `INFO_TIMEOUT` and `RESUME_TIMEOUT` (5 s each)
   bound each call (decision CH44 rule 3).
5. The deny check runs as for a boot. A restore has no `--vsock`
   argument.

`ChApi::READS_DEVICES` is true for `restore::ChTools`, the API of the
vmm, since PR D15 (plan PR L18b). So the vmm restores a snapshot that
it finds. An API with no device read restores no VM: the claim consumes
the snapshot, and the start cold-boots. `ChApi::GUEST_SYNCS` is false
for `ChTools`: the control link has no `sync` command until phase 3
(item 5.7, the item 42 note of the design). So an eviction or a sleep
takes no snapshot yet (caller duty 7). So the vmm restores no VM that
it put to sleep until the phase 3 PR. The restore path and the device
check run only in the tests. `tests/start.rs` gives a fake API with
each value of each constant, and its `info` replies have the shape of
CH v54.

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
   (decision CH44 rule 4). An API with no device read
   (`ChApi::READS_DEVICES` false) takes no snapshot: no snapshot would
   restore, and a take ends CH with no guest power-off. An API with no
   guest sync (`ChApi::GUEST_SYNCS` false) takes no snapshot either:
   item 5.7 of the design asks for a guest `sync` before each take. The
   stop of step 4 is then the graceful one (decision CH19 (3)).
4. `stop::stop` with the control link of the VM. A VM that does not
   stop goes back with no link, so admission counts its RAM (decision
   CH50 rule 4), and the start gets `host_full`. The VMs left in the
   list go back too.

Decision CH54 rule 6 binds a retire too, for an eviction and for an idle
sleep. A VM whose control link closed after its gate takes no snapshot.
A VM whose link closed on a refused line during the take or the stop
(`ControlState::refused`) gets its take consumed with `claim`, so the
next start cold-boots (decision CH19 (5)). Either VM stops and frees its
RAM, but an idle sleep does not count it as slept. `tests/start.rs`
proves a refused line during a take.

The list runs in a task of its own (`tokio::spawn`), and the start
waits for it. hyper drops the start future when the client hangs up
before 101. The task runs on, so no take or stop ends half way, and no
drop of an `Eviction` kills a VM during its snapshot. The blocking step
of a take owns its read lock of the takes, so the lock lasts as long as
the take.

The gate of each VM is `Activity::may_evict` of `creche-vmm-store`, not
`may_sleep` (PR D12). Only a VM with a control link and a snapshot key
may be evicted. Its last use is the later of its start and its last
door close, and an open door counts as used now (decision CH50 rules 5
and 6). An Upgrade of a session under eviction gets `host_full`. So does
an Upgrade whose door an eviction closes after its start: `Relays::close`
stops the claim with `relay::Stopped::Evict`, and the acp handler answers
it with `host_full`. A drop stops it with `Stopped::Drop`, which gets
`not_found`. `tests/start.rs` proves the order, the door close,
`host_full`, a failed take, a failed stop, a start under eviction, an
eviction with no device read, and an eviction and a sleep with no guest
sync. `tests/acp.rs` proves the eviction during
the dial.

## The sleep and the sweep

`sleep::run` times two passes until its stop future ends (plan PR L18).
Both run at once, then the idle check every `IDLE_CHECK` (30 s) and the
sweep every `SWEEP_EVERY` (1 h). A pass that runs ends before the stop
counts. A long pass delays the next tick, and missed ticks do not run
back to back. The sweep goes first when both are due, so long idle
passes never starve it. `sleep::Sleeper` lets `tests/sleep.rs` count
the passes on paused time.

1. `ColdBoot::sleep_idle` reads `Activity::may_sleep` of each VM with a
   control link and a key: the facade is up, no turn runs, no door link
   is open, and the VM is idle past `IDLE_TIMEOUT` (decisions CH2, CH19
   (6), CH36 G12). The gate and the take out run under the table lock.
   It takes out one VM, retires it in its own task as in step 2 to 4 of
   "The eviction", then reads the table again for the next. So a VM
   waits as `Evicting` only for its own retire, and a VM whose door
   opens meanwhile stays up. Each VM gets one try per pass. A sleep
   closes no door.
2. `ColdBoot::sweep` runs `retention::sweep` of `creche-vmm-store` with
   `BUDGET` (PR D13, decisions CH2, CH8-7, CH50 rule 8). It holds the
   lock of the claims, so no key check or claim runs, and it reads a
   copy of the `InUse` set. A VM that ends meanwhile only shrinks the
   set, so the copy names each generation in use, and no generation of
   a restored VM goes. The lock of `InUse` lasts one insert or remove,
   so no runtime worker waits behind the sweep (decision CH36 G21). The
   write lock of the takes keeps each take out of the sweep, as
   `retention::sweep` asks. The blocking step owns both locks.

The last use of a VM is the latest of its start, its last door close
and its last turn event (decision CH50 rule 5). `ControlState::turn`
keeps the time of each turn event and `turn_seen`: a turn event arrived
after the last `ControlReady`. A ready clears `turn_seen`. So a VM is up
only after a ready said `running` or a turn event arrived (decision CH43
rule 8). Admission reads the same gates. `tests/start.rs` proves the
sleep, a turn or an open door that keeps a VM awake, `turn_seen` and its
clear at a new ready, a door close through the acp handler as a use, a
sweep that skips a generation in use, an eviction that outlives its
dropped start, and an idle check that takes out one VM at a time.

## The stop hook

`ColdBoot` implements `StopVm` (plan PR L18b). A stop takes the VM out
of the table under a `Stopping` slot, so its RAM still counts (decision
CH50 rule 4). Then it calls `stop::stop` with the control link of the VM,
so the graceful stop goes first (decision CH19 (3)). A stop that worked
removes the VM from the table and its generation from `InUse`. A stop
that failed puts the VM back with no link, and `VmStopFailed` carries the
pid (`VmProcess::pid`). A second stop of the session waits for the first
and returns its outcome: the same `VmStopFailed`, whose error is shared
in an `Arc` (caller duty 6).

A slot that a start or a retire holds is read again every `SLOT_POLL`
(10 ms) until it ends. A launch step can outlive a dropped start future,
and its output then kills the VM and puts it in the table. A drop of the
stop future kills the VM and puts it back (`Eviction`), and a second stop
that waits then reads the slot again. A start of a session under a stop
gets `host_full` (decision CH54 rule 4). `tests/start.rs` proves a drop
through the hook, a failed stop that keeps the row and the RAM, two stops
that overlap, a stop during a launch, and the RAM of a VM under a stop. It
also proves a start of the session under a stop (`host_full`, one
launch) and a stop that waits for an idle sleep whose retire fails.

## The shutdown

`src/shutdown.rs` stops the vmm cleanly on SIGTERM (plan PR L19, gap
G16 option A). `shutdown::sigterm` registers the handler and gives the
future of the signal. `shutdown::shutdown` then runs these steps:

1. `Stop::stop`: the server accepts no further connection (decision
   CH26 rule 2). Then `VmSet::refuse_starts`: each start on an open
   connection gets `host_full`, so no VM joins the set (decision CH56
   rule 4). This covers a session with no VM too.
2. It waits for its `sleep` future, the end of `sleep::run`, so no idle
   retire runs beside a shutdown take (caller duty 5).
3. Each VM of the `VmSet`, all at once: `Relays::close` stops its door
   link and waits for its claim (decision CH19 (6)). Then
   `VmSet::take` retires the VM as an eviction does, when the gate of
   admission passes and the take gate passes (caller duty 7). Each other
   VM gets `StopVm::stop`, the stop of plan PR L14 through the `Stopper`
   (decision CH19 (3)). `GUEST_SYNCS` is false for `ChTools`, so each VM
   gets the graceful stop today.
4. A start that passed admission before the refusal holds a slot, so
   the first read finds it. A session that a later read adds gets the
   next pass. Each session gets one pass, and no seen session gets a new
   VM.

`SHUTDOWN_WAIT` (85 s) bounds the whole shutdown from SIGTERM, 5 s
inside the `TimeoutStopSec` of 90 s (decision CH56 rule 3, gap G16). A
stop (36 s) and a take (75 s) each fit inside it. One deadline bounds
every step, so a full take and then a full stop of one VM need not both
fit (decision CH56 rule 2). A VM still in the set at the deadline ends
with the vmm through PDEATHSIG (decision CH19 (4)). `ColdBoot` is the `VmSet` of the vmm.
`tests/shutdown.rs` runs the steps on paused time with a fake set.
`tests/start.rs` proves the take of an idle VM, the stop of a busy one
and the door close over the hook, and the graceful stop of each VM with
no guest sync.

## Rules

- Each new dependency is its own pull request, with its lock delta
  (decision CH9-1).
- A library returns a typed error. Only `src/main.rs` turns an error into
  an exit status.
- A crate exposes `pub` items only for the crates that list it in their
  row (decision CH36, gap G24 of the phase 2 plan).

## Known gaps

1. Closed by decision CH54 rule 1: a drop waits for a start that its
   mark overlapped, with no bound (`src/handlers/drop.rs`). So a drop
   may outlast the 60 s wait of the client (decision CH34 rule 5). The
   drop runs in its own task, so a client that hangs up cannot cancel
   it, and the retry waits for the same start.
2. Decision CH43 rule 8: no line on the control link reports a change
   of the facade state yet. The phase 3 guest init PR adds a line that
   reports each change. Until then the sleep gate treats a VM as up only
   after a ready said `running` or a turn event arrived. A VM after a
   cold boot reports `not_started` or `starting` (decision CH34 rule 7),
   so a gate that reads only the ready would never snapshot it.
   `ControlState::turn_seen` holds the rule (plan PR L18).
3. Closed by decision CH54 rule 2: the deny check runs after
   `ControlReady` and before the dial of 1201 and the 101
   (`src/start.rs`). This departs from the order of plan PR L16, which
   put the check before the wait for `ControlReady`.
4. Closed by decision CH54 rule 4: an Upgrade of a session whose VM is
   being evicted, put to sleep or stopped gets `host_full`
   (`src/start.rs`). A door that an eviction closes between the end of
   the start and the 101 gets `host_full` the same way
   (`relay::Stopped::Evict`). Under the mark of a drop no such Upgrade
   reaches the hook (decision CH34 rule 6).
5. Closed by plan PR L18b: the two production pieces that no earlier
   plan PR named. `impl StopVm for ColdBoot` is "The stop hook", and the
   turn reader is in "The control link".
6. Closed by plan PR L18b: `ChTools::info` returns the `VmInfo` of PR
   D15, so `READS_DEVICES` is true and the vmm restores. A device kind
   that `VmConfig` does not name passes the read unseen
   (`creche-vmm-store` AGENTS.md). A new kind needs a field there and a
   line in `check_devices`.
7. Closed by decision CH54 rule 3: a restore whose VM is not `Paused`,
   or has a refused device, gives `egress_assert_failed`. A CH that does
   not answer `info` or `resume` gives `sandbox_start_failed`
   (`src/start.rs`). A reply that is not a `VmInfo` is a device set that
   the check cannot read, so it gives `egress_assert_failed` too.
8. Decision CH43 rule 6: a guest that closes the link before
   `ControlReady` asks for a cold boot. After a restore the start does
   not cold-boot then. It faults, stops the VM, and the next start
   cold-boots, because the claim consumed the snapshot.
9. Closed by plan PR L18b: no take runs with no guest `sync`.
   `ChApi::GUEST_SYNCS` gates each take, and it is false for `ChTools`.
   So `Retirer::retire` sends the graceful stop, as before L18b. The
   rest is caller duty 7.
10. Closed by plan PR L18b: the turn reader calls `ControlState::turn`
    for each event, so `turn_seen` and the last turn time follow the
    guest (decisions CH43 rule 8, CH50 rule 5).
11. Closed by decision CH54 rule 5: the idle check runs every 30 s
    (`sleep::IDLE_CHECK`). So a VM sleeps between 5 min and 5 min 30 s
    after its last use.
12. Closed by decision CH54 rule 5: the sweep runs at the start of
    `sleep::run`, then each hour (`sleep::SWEEP_EVERY`).
13. Closed by decision CH57 rules 3 and 4 (plan PR L7): a family dir or
    `config` that is a symlink or a file gets `unknown_family`, and a
    repeat create whose family `config/` is gone gets `unknown_family`.
14. CONTRACT-QUESTION (decision CH36 G21): decision CH36 G21 puts
    blocking work in `spawn_blocking`.
    `release` runs `RunRoot::unlink_vm` under the table lock
    (`src/start.rs`), so that no start of the session links first. It is
    one `unlink` on the tmpfs of the run root, as cheap as the status
    check and the SIGKILL that run under the same lock.

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
5. Plan PR L20 runs `sleep::run` with the `ColdBoot` of the vmm on the
   runtime, and plan PR L19 ends its stop future before the shutdown
   snapshots. L20 fires that stop at SIGTERM and passes the end of
   `sleep::run` to `shutdown::shutdown` as its `sleep` future.
   `sleep::run` is the only caller of `ColdBoot::sweep` and
   `ColdBoot::sleep_idle` in the vmm.
6. Two drops of one session can overlap. The `StopVm` hook of the vmm
   makes a stop that finds a stop of the same VM running wait for it
   and return its outcome. So no drop removes the files of a VM that
   still runs.
7. Item 5.7 of the design and its item 42 note: lifecycle syncs the
   guest through the control link before each snapshot. The store gives
   that duty to lifecycle, before `Snapshotter::take` (`creche-vmm-store`
   Known gaps, caller duty of PR D9). The phase 3 guest init PR that
   adds the `sync` command sets `ChTools::GUEST_SYNCS` true. In the same
   PR, `Retirer::retire` sends `sync` on the control link before each
   take. Each other take, such as the shutdown snapshots of plan PR L19,
   reads the same gate. A gate set true before the sync would let a take
   end CH with no guest sync, and a cold boot after it (a key mismatch,
   a failed restore, an `Aged` or a `Budget` sweep) would find the
   session disk as a power cut leaves it.
8. Plan PR L20 calls `shutdown::sigterm` at vmm start, inside the
   runtime, and runs `shutdown::shutdown` when the signal fires. A take
   runs in `spawn_blocking`, and the drop of a runtime waits for each
   blocking task. So L20 ends the runtime with `shutdown_timeout`, and
   the vmm exits within the 5 s that `SHUTDOWN_WAIT` leaves.
