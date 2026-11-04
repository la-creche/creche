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
never reaches the hook, so it gets no boot. The hook returns the vsock
socket of the VM. Plan PR L16 adds the hook of the vmm. `tests/acp.rs`
fakes it, and a fake vsock socket plays guest port 1201.

`src/relay.rs` holds the relay registry with a stop signal (decision
CH26 rule 5). `Relays::claim` holds a session for one Upgrade until the
relay ends. `Relays::mark_drop` stops the open relay of a session and
marks the drop. An Upgrade under the mark gets `not_found` (decision
CH34 rule 6), and so does one whose start a drop overlapped. The drop
keeps its mark until the row is gone. The second row read comes after
the claim, so a drop that ends between the first read and the claim
leaves no row, and the Upgrade gets `not_found`.

After 101, `AcpRelay` dials guest port 1201 with `control::dial`, the
dial of the control link (decision CH13). It pauses 10 ms between two
tries and retries ECONNREFUSED, and `READY_WAIT` bounds it (decision CH43
rules 5 and 7). Then it copies bytes both ways (decision CH20). An EOF
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
sends SIGKILL and waits `KILL_WAIT` (5 s) at most. A process that
outlives it gives `StopError::Hung`, and its files stay. Both waits come from `creche-wire`. A kill is a power cut
for the guest, and no error: `Stopped::Killed`. After the exit it runs
`remove_stale` of `creche-vmm-store` in `spawn_blocking`, which removes
`api.sock.lock` (fact 75) and the `vsock.sock` that a kill leaves. A stop
takes no snapshot, so the next start is a cold boot (decision CH19 (5)).
`StopLink` and `VmProcess` let `tests/stop.rs` give fakes on paused time.

## Rules

- Each new dependency is its own pull request, with its lock delta
  (decision CH9-1).
- A library returns a typed error. Only `src/main.rs` turns an error into
  an exit status.
- A crate exposes `pub` items only for the crates that list it in their
  row (decision CH36, gap G24 of the phase 2 plan).

## Known gaps

1. Decision CH43 rule 8: no line on the control link reports a change
   of the facade state yet. The phase 3 guest init PR adds a line that
   reports each change. Until then the sleep gate treats a VM as up only
   after a ready said `running` or a turn event arrived. A VM after a
   cold boot reports `not_started` or `starting` (decision CH34 rule 7),
   so a gate that reads only the ready would never snapshot it. The sleep
   gate is plan PR L18, a later PR, so this crate does not hold the rule
   yet.
2. CONTRACT-QUESTION: no decision row bounds the whole start of an
   `acp` Upgrade. Decision CH36 G26 bounds the wait for `ControlReady`
   at 10 s, and `control::open` holds it (`READY_WAIT`). Admission and
   an eviction under pressure (plan PR L17) come before the dial and
   have no bound. The handler reads G26 narrowly: it adds no bound of
   its own around the `StartVm` hook (`src/handlers/acp.rs`).
3. CONTRACT-QUESTION: no decision row says what a failed dial of guest
   port 1201 after 101 does. The 101 is out, so no typed code fits. The
   relay reads the rows narrowly: it closes the client stream and writes
   no fault file (decision CH21-1 names start faults only). The client
   sees an EOF (`src/relay.rs`).
4. CONTRACT-QUESTION: no decision row says what a stop does when the
   `stop` line does not go out (no open control link, or a failed write),
   or when SIGTERM fails. Each wait of decision CH34 rule 5 gives its step
   time to work. The stop reads the rows narrowly: it skips the wait of a
   step that did not go out and goes to the next step at once
   (`src/stop.rs`).
5. CONTRACT-QUESTION: no decision row says how long a stop waits for
   the exit after SIGKILL, or what it answers when the process outlives
   that wait (a process in the D state can). Decision CH33 rule 11
   bounds a drop at about 35 s, and decision CH34 rule 5 gives the
   client 60 s. The stop reads the rows narrowly: it waits `KILL_WAIT`
   (5 s) at most, so it ends within 40 s. Past it, it gives
   `StopError::Hung` and removes no file (`src/stop.rs`).
