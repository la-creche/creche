# facade

The ACP facade: the ACP v1 agent of one family VM. guest init starts it,
and ACP comes only on the relay socket (decision CH80). The root
`AGENTS.md` applies here too.

The package has one bin, `creche-facade`: the one process of a VM that
owns Flue. It takes no lock, keeps no beat deadline and serves no ACP on
stdio. It serves ACP on `RELAY_SOCKET`, `/tmp/creche-facade/acp.sock`, and
guest init holds its stop (decisions CH5d, CH19).

ACP is the Agent Client Protocol. The SDK is `@agentclientprotocol/sdk`.
Flue is the agent runtime `@flue/runtime`.

## Build and test

```bash
pnpm install       # installs agent/ too: pnpm-workspace.yaml names it
pnpm test          # builds dist/ first. No VM, no model call
pnpm run typecheck
pnpm run build     # agent/ to agent/dist/, then src/ to dist/src/
```

`test/session-id.test.ts` runs the built bin
`dist/src/bin/creche-facade.js` as a child process, as guest init runs it.
`test/compose.test.ts` serves the real agent module in process, on a fake
vmm (`agent/test/fake-vmm.ts`), the agent's fake model and its fake `ch`.

The facade imports the agent module from `agent/dist/`. Flue's hooks
need one copy of `@flue/runtime`, but an install in `agent/` gives it a
Flue of its own. After such an install, delete `agent/node_modules` and
install here again.

## The wire

`vectors/creche-extensions.json` holds the ACP v1 values of the facade:
the protocol version, the line cap and the answer to `initialize`. The
facade and its tests read it. The facade answers no creche extension.

The facade reads no creche key of `_meta`. The host row carries the typed
origin of each session, which replaces the keys `creche/job`,
`creche/delegation`, `creche/trigger` and `creche/created_at` (decision
CH21-4). attendance refuses each creche key from a door client.

## Environment

`src/session-id.ts` reads `CRECHE_SESSION_ID` one time at process start,
before the family state (decision CH62 rule 6). guest init sets it. An
absent variable, or a value that is not the wire `SessionId`, refuses the
start with status 17 (decision CH81 rule 8). `src/guest.ts` holds the
variable name, and `test/guest.test.ts` holds it equal to
`rust/crates/wire/vectors/guest.json`.

The facade reads no `AGENT_*` variable (decisions CH5e, CH80). A set one
has no effect and gives no stderr line (decision CH81 rule 5).
`readGuestStart` of the agent module gives the start: the fixed guest
paths `FLUE_DB` and `WORKSPACE`, the planes at `127.0.0.1:4000` and
`127.0.0.1:8300`, and the state of one pull on the family-state socket. The
start pulls until the vmm gives a state, with one stderr line for each
fault, and never exits on a fault (decision CH23 rule 2). The pause
before the next pull is 250 ms, then doubles up to 5 s (decision CH81
rule 4). So the facade stays `starting` for guest init until the pull
gives a state. The image env gives only the tool env (`MPLBACKEND` and
`CRECHE_TOOL_HTTPS_PROXY`).

Before each `session/prompt`, the facade pulls the family state into the
agent (`src/turns.ts`, decision CH5a), from the vmm on the family-state
socket. A rotated key or token and an edited config reach that prompt. A
new `model_alias` reaches it too, with one stderr line that gives the new
alias's limits (decisions CH5c, CH5c-2). Each fault reply of the vmm fails
the prompt, and the facade serves on (decisions CH23 rule 2, CH53 rule 2).

The workspace of the granted sandbox tools is `WORKSPACE`: the one
writable place for the agent's files, with `flue.db` kept out of the
listing that Flue puts in the prompt.

## Exit status

| Status | Cause |
|---|---|
| 0 | the stop line of the local link |
| 8 | the relay socket cannot be made |
| 16 | Flue does not start |
| 17 | `CRECHE_SESSION_ID` is absent or is not a session id (`src/session-id.ts`) |
| 18 | the local link does not open, or closes or faults outside a stop (decision CH67 rule 8) |

Only the stop line (0) and a lost local link (18) end a facade that
serves (decisions CH66, CH67 rule 8, CH80). Before it serves, 17, 16 and
8 are its exits, and guest init starts it again after the 1 s restart
pause (decisions CH62 rule 8, CH81 rule 8). The start pull has no exit:
it pulls until the vmm gives a state.

The refusal 17 writes one stderr line: the reason and the variable, never
a value.

## Sessions

A session is one Flue agent instance. Its ACP session id is the instance
id. A VM serves one session, and its id is the host's `CRECHE_SESSION_ID`
(decisions CH1, CH25 rule 2). The facade opens that session at start and
serves no other id (`src/sessions.ts`, decision CH21-8). Flue makes the
instance at the first send, so a session with no turn loads an empty
history.

The vmm keeps the host list: cwd, title, times and origin (decision CH21).
attendance answers `session/new` and `session/list` from that list
(decision CH21-7), so the facade serves neither, and the SDK answers each
with method not found. The facade keeps no table of its own in `flue.db`.

At each turn end the facade sends `session_info_update` to each peer on
the session. `updatedAt` is the time of the turn's last record. Only the
first turn, the one that makes the Flue instance, carries `title`, because
the vmm keeps the first title of the row (decision CH32 rule 4).

The tests use `sqlite()` with no path and pi's `fauxProvider()`, as the
agent tests do. Each test serves a fresh session id on the one Flue of its
file. `test/served-family.ts` serves the real agent module in process. The
start takes the fixed guest paths, which a test host lacks, so the fixture
puts a temporary store and workspace in their place.

One prompt runs per session at a time. `handle.read()` replays the whole
conversation from its start, so `SubmissionChunks` keeps only the chunks
of the turn's own submission.

`session/load` replays the history, then puts the link on the session's
live updates (`src/live.ts`). Flue gives no public read of a whole
conversation in process, so `src/flue-history.ts` reads Flue's updates
view through `@flue/runtime/internal` (Phase 2 plan, Q2). The replay
starts from the last `conversation-reset` (`src/replay.ts`). Live items
that come during the replay wait, and only those after the last replayed
position follow it.

## Rules

1. Pin each runtime dependency to one exact version. Do not use a range.
   ACP SDK 2.0 is new, and a patch of Flue can change a record that the
   facade reads.
2. Only `src/acp.ts` imports the ACP SDK. Other modules import the ACP
   types from `src/acp.ts`.
3. Write each ACP line only through `strictWriter` of `src/acp.ts`. Each
   line is strict JSON under the line cap (`src/line.ts`). Free text goes
   to stderr through `src/stdio.ts`. The facade writes nothing to stdout.
4. A line that is not strict JSON, or that is over the cap, never reaches
   a connection. An answer becomes a JSON-RPC error with the same id.
   Another message is dropped, with a line on stderr.
5. Treat a Flue record as data. `src/mapper.ts` makes each forwarded
   string well formed and cuts it. Its switch over the chunk kinds has a
   `never` default, so a new kind breaks the build.
6. A failed turn answers a JSON-RPC error, never a stop reason
   (`settle` in `src/turns.ts`).
7. Only `src/flue-history.ts` imports `@flue/runtime/internal`.
   `test/flue-history.test.ts` pins each fact that it reads. When that
   test fails after a Flue change, fix the module, not the test.
8. Change Flue only through `patches/@flue__runtime@2.3.0.patch`. Both
   `pnpm-workspace.yaml` files name it, so the agent's tests and the
   sandbox image run the same Flue. The patch does two things, each in
   `runModelTurnWithRecovery` and its helpers:
   - Between two tool calls, it ends the run when the next model call would
     pass the compaction threshold. It compacts, then resumes the run. At
     most 8 times per submission.
   - After a reply that stops at the output limit, it compacts when the
     context is full, then continues with one short user prompt. At most 2
     times per submission.
   A Flue upgrade drops the patch. Check that Flue does both, or port the
   patch, before you move the pin. `agent/test/compaction.test.ts` fails
   when either part is missing.
9. The facade takes no lock (decision CH19 rule 6). The session disk OFD
   lock stops a second VM on one disk, and guest init runs at most one
   facade. stdin is `/dev/null`, and the local link ends the process.
10. The `initialize` answer keeps `sessionCapabilities.list`, and the
   facade serves no `session/list` (decision CH84 rule 11, `src/acp.ts`).
   attendance's handshake refuses a facade that does not list it
   (`read_agent` in `attendance/src/attendance/acp_link.py`), and
   attendance answers `session/list` itself (decision CH21-7).
11. The facade sends no delegation to the chaperone, and the chaperone
   applies no run-time one-hop bound from the delegation id (decision CH84
   rule 12, `src/sessions.ts`). The delegation rides in the origin of the
   host row (decision CH21-4). Flue's MCP adapter sends no `_meta` on
   `tools/call`, and a connection has no per-call header.

## The local link

The facade connects to guest init on the Unix socket `LOCAL_LINK_SOCKET`
(`src/local-link.ts`, decisions CH21, CH62 rules 9 to 11). `src/guest.ts`
holds the
socket path, the line cap, the stop wait and `RELAY_SOCKET`, and
`test/guest.test.ts` holds them equal to
`rust/crates/wire/vectors/guest.json` (decision CH62 rule 5).

1. The link opens after the start pull, the Flue start and the relay
   listen. guest init then moves the facade from `starting` to `running`
   (decision CH32 rule 6). So `running` means that the facade serves
   (decision CH81 rule 6).
2. Each turn sends `{"start":{}}` before Flue runs it, and
   `{"end":{"title":…,"updated_at":…}}` after its record. The end
   carries `titleOf` of the turn's prompt and the time of the turn's last
   record, in `UnixMillis` (decision CH21). Each start gets an end: the
   end goes up before the row write, so a write that throws still ends
   the turn for guest init.
3. Each line up is one wire `TurnEvent`, checked with the wire validator,
   then written as one strict JSON line under `CONTROL_LINE_CAP`. guest
   init kills the facade at a line that it refuses (decision CH62 rule
   11), so the facade drops an event that does not check, with one
   stderr line.
4. A `{"stop":{}}` line down stops the facade: it closes the relay,
   stops Flue for at most `FLUE_STOP_MS` (the 25 s stop wait less 1 s),
   and exits 0 (decision CH62 rule 9). Flue 2.3.0 waits
   up to 30 s for a running submission, so the facade bounds the wait.
5. The facade never closes the link. guest init kills a facade whose link
   closes outside a stop (decision CH32 rule 7), so the exit of the
   process is the only close. guest init accepts one link per facade
   start, so nothing dials again.
6. The facade fails closed on its link (decision CH67 rule 8). When the
   link does not open, or closes or faults outside a stop, the facade
   writes one stderr line, stops Flue for at most `FLUE_STOP_MS` and
   exits 18. guest init starts it again after the
   1 s restart pause (decisions CH62 rule 8, CH66 rule 1). A close during
   a stop keeps the stop path (decision CH66 rule 5).
7. The facade drops a down line that is not `stop` with one stderr line,
   and keeps the link (decision CH67 rule 7).

TypeBox 1.3.27 counts a `maxLength` in grapheme clusters, and JavaScript
counts a string in UTF-16 units. `agent/wire-read` compiles each length as
a pattern of code points, as Rust counts. So a title of 256 astral
characters (512 UTF-16 units) passes, and 257 is refused
(`test/local-link.test.ts`). `titleOf` cuts at 256 bytes, so it never
makes such a title.

## Relay connections

guest init joins each ACP link of guest port 1201 to the relay socket
(decisions CH5e, CH62 rule 5). Each socket connection gets an app of its
own on the one session service, so a session's live updates reach every
connection on it. No connection has a beat deadline, because the door
sends no beat (decisions CH5d, CH19). A connection ends when its peer
closes it. Only the local link stops the facade.

## Known gaps

- The workspace is one directory for every session. The playpen gave
  `coding-sandbox` one directory per chat (Phase 2 plan, risk 6).
