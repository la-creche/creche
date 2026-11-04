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
CH32 rule 3). Each other error ends the open with a reported fault and
no cold boot (decision CH43 rule 6).

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
