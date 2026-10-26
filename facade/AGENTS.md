# facade

The ACP facade: the ACP v1 agent of one family sandbox. attendance starts
one `creche-facade` per sandbox with `sbx exec` and speaks ACP over its
stdio. The root `AGENTS.md` applies here too.

The package has two bins:

- `creche-facade`: the one process of a sandbox that owns Flue. It holds
  the lock, the beat and the stop. It serves ACP on stdio and on a Unix
  socket in the VM, `$TMPDIR/creche-facade/acp.sock`.
- `acp-relay`: one per door connection. It pipes its stdio to the
  facade's socket, byte for byte. It parses nothing and adds nothing.

ACP is the Agent Client Protocol. The SDK is `@agentclientprotocol/sdk`.
Flue is the agent runtime `@flue/runtime`.

## Build and test

```bash
pnpm install
pnpm test          # builds dist/ first. No sbx, no VM, no model call
pnpm run typecheck
pnpm run build     # src/ to dist/src/, the vector file to dist/vectors/
```

The tests run the built bin `dist/src/bin/creche-facade.js` as a child
process, as attendance runs it.

## The wire

`vectors/creche-extensions.json` holds the creche extensions of ACP: the
answer to `initialize`, `_creche/beat` and `_creche/shutdown`, the line cap
and the beat cadence. The facade tests read it. The attendance tests read it
too. Change a value there, and both sides change.

## Environment

`src/env.ts` reads two variables at process start.

| Variable | Meaning | Absent |
|---|---|---|
| `AGENT_SANDBOX` | the sandbox id, for the lock record | exit 2 |
| `AGENT_CONTROL_DIR` | the control mount, for `supervisor.lock` | exit 7 |

## Exit status

| Status | Cause |
|---|---|
| 0 | `_creche/shutdown`, or stdin closed |
| 2 | no `AGENT_SANDBOX` |
| 3 | a live facade holds the lock |
| 5 | no `_creche/beat` for three beat windows (90 s) |
| 6 | the control mount cannot be written |
| 7 | no `AGENT_CONTROL_DIR` |
| 8 | the relay socket cannot be made |

`acp-relay` exits 0 when its link ends, and 1 when no facade listens.

## Sessions

A session is one Flue agent instance. Its ACP session id is the instance
id. Flue keeps no list of conversations, so the facade keeps one table,
`creche_sessions` (id, title, cwd, created_at, updated_at), in `flue.db`
beside Flue's own tables (`src/session-index.ts`). No other program of ours
writes the file. The tests use `sqlite()` with no path and pi's
`fauxProvider()`, as the agent tests do.

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
3. Write to stdout only through the writer of `src/stdio.ts`. Each line is
   strict JSON under the line cap (`src/line.ts`). Free text goes to
   stderr. `acp-relay` writes to stdout only the bytes of the socket.
4. A line that is not strict JSON, or that is over the cap, never reaches
   stdout. An answer becomes a JSON-RPC error with the same id. Another
   message is dropped, with a line on stderr.
5. Treat a Flue record as data. `src/mapper.ts` makes each forwarded
   string well formed and cuts it. Its switch over the chunk kinds has a
   `never` default, so a new kind breaks the build.
6. A failed turn answers a JSON-RPC error, never a stop reason
   (`settle` in `src/turns.ts`).
7. Only `src/flue-history.ts` imports `@flue/runtime/internal`.
   `test/flue-history.test.ts` pins each fact that it reads. When that
   test fails after a Flue change, fix the module, not the test.
8. Never serve without the lock. `lock.ts` never throws. Keep its pid and
   its beat: attendance reads the beat with its own clock.
9. Never make stdin EOF the only way the process ends. A client-side kill
   of `sbx exec` does not reach the VM. The beat deadline ends the process.

## Relay connections

Each socket connection gets an app of its own on the one session
service, so a session's live updates reach every connection on it. A
connection's beat deadline is armed by its first `_creche/beat`, and a
missed deadline closes that connection alone. `_creche/shutdown` on a
relay connection does nothing: only the supervisor link stops the
facade.

## Known gaps

- attendance's door adds no `_creche/beat` to a door connection, so a
  relay connection never arms its deadline. A relay whose host end died
  can hold its connection until the facade stops. `src/relay.ts` holds
  the `CONTRACT-QUESTION`.
- `src/env.ts` parses two variables that `agent/src/config.ts` parses
  too. Row 15 of the Phase 2 plan wires the agent module, and `env.ts`
  then goes.
- The bin starts no Flue runtime yet, so `_creche/shutdown` has no Flue
  to stop, and the bin serves no session method. Row 15 starts Flue with
  the agent module, wraps its adapter with `tapStores`, and passes
  `sessionService` to the supervisor.
