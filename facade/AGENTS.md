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
pnpm install       # installs agent/ too: pnpm-workspace.yaml names it
pnpm test          # builds dist/ first. No sbx, no VM, no model call
pnpm run typecheck
pnpm run build     # agent/ to agent/dist/, then src/ to dist/src/
```

The tests run the built bin `dist/src/bin/creche-facade.js` as a child
process, as attendance runs it. `test/compose.test.ts` runs it with one
family's mounts (`agent/test/family.ts`), the agent's fake model and its
fake `ch`.

The facade imports the agent module from `agent/dist/`. Flue's hooks
need one copy of `@flue/runtime`, but an install in `agent/` gives it a
Flue of its own. After such an install, delete `agent/node_modules` and
install here again.

## The wire

`vectors/creche-extensions.json` holds the creche extensions of ACP: the
answer to `initialize`, `_creche/beat` and `_creche/shutdown`, the line cap
and the beat cadence. The facade tests read it. The attendance tests read it
too. Change a value there, and both sides change.

Its `session_meta` holds the three keys of `_meta` that attendance sends,
the method that carries each and the shape of each value, and the one key
that the facade adds:

| Key | Method | Column of `creche_sessions` |
|---|---|---|
| `creche/job` | `session/new` | `job_id` |
| `creche/delegation` | the first `session/prompt` | `delegation`: the ID only |
| `creche/trigger` | the first `session/prompt` | `trigger_json` |
| `creche/created_at` | sent: `session/list`, `session_info_update` | `created_at`, as RFC 3339 UTC |

`src/creche-meta.ts` checks each value. A bad value is refused with
invalid params. A session keeps its first delegation and its first
trigger, and a prompt that sends another one is refused. No key goes into
the model's prompt. Each `session/list` row, and the `session_info_update`
at the end of each turn, sends the keys of the session in its `_meta`.

## Environment

`src/family.ts` reads the family config one time at process start, with
the agent module's `readFamilyConfig`. `agent/AGENTS.md` names each
variable. The agent's store and the facade's `creche_sessions` share
`$AGENT_SESSIONS_DIR/flue.db`. Flue starts only after the facade holds the
lock.

The workspace of the granted sandbox tools is
`$AGENT_SESSIONS_DIR/workspace`: the one writable mount for the agent's
files, with `flue.db` kept out of the listing that Flue puts in the prompt.

## Exit status

| Status | Cause |
|---|---|
| 0 | `_creche/shutdown`, or stdin closed |
| 2 | no `AGENT_SANDBOX` |
| 3 | a live facade holds the lock |
| 5 | no `_creche/beat` for three beat windows (90 s) |
| 6 | the control mount cannot be written |
| 7 | a mount variable is unset. The facade reads the mounts first |
| 8 | the relay socket cannot be made |
| 9 to 15 | the agent module refuses the family config (`EXIT_BY_REFUSAL` in `src/family.ts`) |
| 16 | Flue does not start |

A refusal (2, 7, 9 to 15) writes one stderr line: the reason, and the
variable or the field, never a value.

`acp-relay` exits 0 when its link ends, and 1 when no facade listens.

## Sessions

A session is one Flue agent instance. Its ACP session id is the instance
id. Flue keeps no list of conversations, so the facade keeps one table,
`creche_sessions` (id, title, cwd, created_at, updated_at, job_id,
delegation, trigger_json), in `flue.db`
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
9. Never serve without the lock. `lock.ts` never throws. Keep its pid and
   its beat: attendance reads the beat with its own clock.
10. Never make stdin EOF the only way the process ends. A client-side kill
   of `sbx exec` does not reach the VM. The beat deadline ends the process.

## Relay connections

Each socket connection gets an app of its own on the one session
service, so a session's live updates reach every connection on it. A
connection's beat deadline is armed by its first `_creche/beat`, and a
missed deadline closes that connection alone. attendance's door sends
the beat on each relay connection, with the string ids
`creche-beat-<n>`, and takes the answers. `_creche/shutdown` on a
relay connection does nothing: only the supervisor link stops the
facade.

## Known gaps

- The facade does not send `creche/delegation` to the chaperone. Flue's MCP
  adapter sends no `_meta` on `tools/call`, and a connection has no
  per-call header. So the chaperone cannot use the delegation ID for a
  one-hop bound at run time. `src/sessions.ts` holds the
  `CONTRACT-QUESTION`.
- The workspace is one directory for every session. The playpen gave
  `coding-sandbox` one directory per chat (Phase 2 plan, risk 6).
