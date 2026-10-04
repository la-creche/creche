# attendance

The session service. Every kind of agent is one call: create a session in a
family, run a turn. `attendance` owns sessions, turns, the journal and the
event streams. Nothing else owns any of them. The root `AGENTS.md` applies
here too.

It is Python because the host side of the channel may not need Node. The
playpen runs inside the sandbox image, which carries Node.

## What it does

1. Serves the session API to four doors and the noticeboard, over a Unix
   socket, and over the LAN address when the config asks.
2. Keeps every session on disk. A session survives a restart, a sandbox
   replacement and a deploy.
3. Writes every event to the journal before any client sees it. A client
   that disconnects changes nothing.
4. Holds one channel per sandbox and treats every byte from it as untrusted.
5. Enforces one writer per session and many readers.
6. Reports faults in a fault file. It never calls `caregiver`. The one call
   between the two runs the other way: `caregiver` asks for a sandbox switch.
7. Runs one thin job per delegate call, then deletes the session.
8. Limits and queues turns for autonomous families only.
9. Serves the dispatch door: `enqueue` and `job_status`.
10. Copies an attended session into Open WebUI, and turns an edit into a pi
    branch.

## Ordering rules

1. Write the journal line, then fan it out. `_append` does both, in that
   order.
2. Journal the prompt before you send `start_turn`.
3. Subscribe before you replay.
4. Seed the sequence counter from the journal, not from `session.json`.
5. Never wait for a pre-start. `create_or_find` sends `open_session` and
   returns.
6. An idle close sends `shutdown` and waits for the exit, under the dial
   lock. `_stop_loops` cancels every loop before it awaits any.

## Trust rules

1. Every byte from the playpen is a claim. `wire.parse` returns a refusal,
   never raises.
2. Nothing from the channel is acted on. The host never opens a path, fetches
   a URL or runs a string that came from a sandbox.
3. `supervisor.lock` can delay a channel and can never grant one. Read the
   lock of the sandbox being dialled, never the family's. Time the counter,
   never a timestamp.
4. A fault clears only on evidence: a completed handshake or a service start.
5. Persona text carries no authority.
6. A door's request body is untrusted input. `requests.py` parses it.

## Security rules

1. Fail closed on tokens. A missing, empty, short or group-readable token
   file stops the service from starting.
2. Never log a token, a prefix of one, or its length.
3. No secret on argv. The family key and the chaperone token never enter this
   process.
4. Bind the LAN address. Never loopback, never `0.0.0.0`.

## Structure rules

1. `attendance` never calls `caregiver`.
2. This service runs the handshake, so it dials a `creating` sandbox.
   `startable_sandbox()` holds that rule.
3. One channel per sandbox, not per family. A draining family holds two.
4. Layers talk downward only: `api` to `service` to the stores and links to
   `channel` and `wire` to primitives. A route never touches the journal.
5. Nothing here needs Node.
6. Build paths with `paths.py`.

## Switch rules

1. The status document outranks the pin. The pin lives in memory.
2. Release the outgoing sandbox before you answer. `_retire_sandbox` stops
   every held-open process, sends `shutdown`, then closes the channel.
3. A switch belongs to the platform, not to the HTTP request. A repeat joins
   the run. A refused run is forgotten.
4. The switch runs the handshake before anything moves.
5. The pin is the family's choice of sandbox, not only a switch's.
6. Interrupt waits for nothing. Do not add a grace period.

## Lease rules

1. An idle lease passes to another door. `force` does not enter into it.
2. An active session refuses every door and every flag. `queued` counts as
   active.
3. A renew never acquires.
4. A release refuses while a turn is in flight.

## Autonomous and dispatch rules

1. Only an autonomous family has a limit and a queue.
2. `queue_full` is checked before the turn is minted.
3. `turn_queued` reaches disk before the FIFO holds the turn.
4. A restart ends the queue with `queue_lost`.
5. The outcome record is written before the session is deleted.
6. The target's own status document decides whether it may be dispatched to.
7. `door-dispatch` is its own principal.
8. A caller never names itself. `caller_family` comes from the token.
9. The ledger entry is written before the turn starts, and removed when the
   turn refuses.

## Mount rules

1. Never name a fixed in-VM path. A mount's path inside the VM is its host
   path. `SESSIOND_SANDBOX_SESSIONS_MOUNT` is the one override, and only the
   integration harness sets it.
2. Never dial without an env file. The path comes from the status document,
   per sandbox. A document without it raises `sandbox_start_failed`.

## Run it

```bash
uv run python -m attendance --check   # validate the environment and every token file
uv run python -m attendance           # serve
```

| Variable | Meaning |
|---|---|
| `SESSIOND_SESSIONS_ROOT` | the session store |
| `SESSIOND_STATE_ROOT` | tokens, leases, faults, family status |
| `SESSIOND_WORK_ROOT` | the `code-sandbox` work root |
| `SESSIOND_SOCKET` | the Unix socket |
| `SESSIOND_BIND_LAN`, `SESSIOND_LAN_ADDRESS`, `SESSIOND_LAN_PORT` | the optional TCP listener, port 8350 |
| `SESSIOND_CHANNEL_COMMAND` | how a channel is opened. `{env_file}` comes from the status document. |
| `SESSIOND_LOG_DIR` | where each family's playpen stderr lands |
| `SESSIOND_LOCK_STALE_S`, `SESSIOND_LOCK_POLL_S` | the lock beat window and its poll |

Seven token files sit under `<state root>/tokens/`. Five are mode 0600. The
delegate and dispatch tokens are mode 0640, group `agents`, because the
chaperone reads them as another user. `SIGHUP` reloads them.

## Layout

| Module | Holds |
|---|---|
| `api.py`, `__main__.py` | the routes, `--check`, the signal handlers |
| `service.py` | the operations, the turn lifecycle, the channel callbacks |
| `requests.py` | every request body, validated before use |
| `turns.py`, `streams.py`, `journal.py`, `store.py` | live turns, readers, the NDJSON journal, the on-disk layout |
| `leases.py`, `queueing.py`, `outcomes.py` | one writer per session, the autonomous queue, what survives a job |
| `approvals.py` | reading the chaperone's audit for a held gate |
| `owui_copy.py`, `terminal.py`, `branching.py`, `persona.py` | Open WebUI copy, terminal exchanges, pi branches, the folder prompt |
| `playpen_link.py`, `switching.py`, `channel.py`, `exec_channel.py`, `wire.py` | one sandbox's channel, a switch, the protocol |
| `jobs.py`, `dispatch.py`, `workspace.py` | thin jobs, the dispatch ledger, the per-chat work directory |
| `family_status.py`, `faults.py` | the status document, the fault file |
| `verify.py` | `attendance-verify` |

## Tests

```bash
uv run pytest attendance/tests
```

`tests/attendance_harness.py` is the far side of the channel. Add a new
misbehaviour there. A test that spawns a process is marked `slow`.

## Known gaps

- `approvals.py` tails the chaperone's audit file, which lags up to one
  second (`approvals.py`, `service.py`).
- An outcome record's `spend_usd` sums advisory costs. A sum of zero with
  tokens records `null` with `spend_reason` (`outcomes.py`).
- A `tui-` session with a null `leaf_id` can reach its chat twice
  (`service.py`, `terminal.py`).
- After an idle close, the status document still says `channel: open`
  (`playpen_link.py`).
- `wait=accepted` still waits out a `planned` sandbox for up to 60 seconds
  (`service.py`).
- `X-Session-Id` is caller-set, so a model can aim a `code-sandbox` job at
  another chat's directory of the same family. The grammar stops any escape
  from the work root (`service.py`).
