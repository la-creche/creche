# attendance

The host side of each family's facade. `attendance` holds the supervisor
link to each sandbox's facade, runs the delegate and dispatch jobs over
ACP, pipes the ACP door to the facade, and runs the sandbox switch. The
conversations live in the facade's store, not here. The root `AGENTS.md`
applies here too.

It is Python because the host side may not need Node. The facade runs
inside the sandbox image, which carries Node.

## What it does

1. Serves `/delegate`, `/dispatch`, `/dispatch/jobs`, `/trigger`,
   `/trigger/jobs`, `/internal/switch-sandbox`, the view routes and `/busy`
   over a Unix socket, and over the LAN address when the config asks.
   Serves `WS /acp/{family}` on the Unix socket only.
2. Holds one supervisor link per sandbox and treats every byte from it as
   untrusted.
3. Reports faults in a fault file. It never calls `caregiver`. The one call
   between the two runs the other way: `caregiver` asks for a sandbox switch.
4. Runs one thin job per delegate call over ACP: one `session/new` and one
   `session/prompt` on the family's facade. The conversation stays in the
   facade's store (phase 2 plan, Q5).
5. Serves the dispatch door: `enqueue` and `job_status`. Each job runs over
   ACP on the target's facade. Only an autonomous family has a limit and a
   queue.
6. Starts the family's facade before the `acp-relay` of each ACP door
   connection, on the sandbox that the relay goes to. A facade that cannot
   start closes the connection with 4503 (`sandbox_unavailable`), and no
   relay starts. Beats each ACP door connection. The door sends
   `_creche/beat` on the relay every 30 s, with the string ids
   `creche-beat-<n>`. The prefix is reserved. The door takes the facade's
   answers, so no answer reaches the client. Each other line passes as it
   is. A relay that leaves two beats unanswered closes with code 4408
   (`Close.NO_BEAT`). An error answer counts as no answer (`acp_door.py`).
   A relay that exits closes the connection at once with code 1001 and the
   reason `the relay ended`. The exit of the child ends its stream, also
   when a descendant of `sbx exec` still holds its stdout
   (`exec_channel.py`).
7. Serves the view for `view-ro`. Each view request opens one relay of its
   own, sends `initialize` and one read method, and closes it. It never
   sends `session/prompt` (`acp_view.py`).
8. Serves `GET /busy` for `view-ro`: the release tool's quiet window reads
   it in place of the session API.
9. Serves the trigger door for `door-trigger`. Each firing is one job in
   an autonomous family's `JobQueue`, beside the dispatched jobs.

## The trigger door

Each route takes the `door-trigger` token and no other. `/trigger` takes
an autonomous family only. The ACP door takes an attended family only.

| Route | Body | Answers |
|---|---|---|
| `POST /trigger` | `{"family", "kind", "name", "message", "idempotency_key"}` | `{"session_id", "status", "created"}`, as `/dispatch` |
| `POST /trigger/jobs` | `{"family", "session"?, "since"?, "limit"?}` | `{"jobs": [job]}`, as `/dispatch/jobs` |

1. `kind` is `timer` or `webhook`. A webhook firing names its trigger in
   `name`, in the grammar of a family name. A timer firing sends a null
   `name`.
2. `message` is the prompt, at most 256 KiB of UTF-8. It is data.
3. `idempotency_key` is required, at most 128 characters. A repeat in the
   same family within an hour answers the first job with `created` false,
   and enqueues nothing.
4. Each firing is a fresh `auto-<ulid>` job. The family's
   `max_running_turns` and the queue cap hold for a trigger as for a
   dispatch.
5. The prompt's `_meta` carries `creche/trigger`: the kind, the name and
   the time the job was enqueued. The outcome record names the same
   trigger.
6. The trigger ledger is `<state root>/trigger-jobs/<family>/`, apart from
   the dispatch ledger.

## The view and `/busy`

Each route takes the `view-ro` token and no other. A family with no sandbox
to relay to, a relay that ends, a line that is not strict JSON-RPC and a
read past 30 s each answer 503 `sandbox_unavailable`.

| Route | Sends | Answers |
|---|---|---|
| `GET /families/{f}/sessions?cursor=` | `session/list` | `{"sessions": [row], "next_cursor": str \| null}` |
| `GET /families/{f}/sessions/{id}?start=&limit=` | `session/load` | `{"session_id", "entries": [entry], "next": int \| null, "truncated": bool}` |
| `GET /busy` | nothing | `{"busy": bool, "prompts_in_flight": int, "jobs_running": int, "jobs_queued": int}` |

1. A row is `{id, title, created, updated, job_id, delegation, trigger}`.
   The cursor is the facade's, passed as it is: printable ASCII, at most
   256 bytes.
2. An entry has a `position` from 0 and a `kind`. `user`, `agent` and
   `thought` carry `text`: consecutive chunks of one kind join. A
   `tool_call` carries `tool_call_id`, `title`, `status` and `result`, the
   text of its last update.
3. The caps: 64 KiB of UTF-8 per text, 256 bytes per id or title, 20 000
   updates per read (`truncated` is then true), `limit` 1 to 500 (default
   100) and 1 MiB of entries per page.
4. An unknown session is 404 `not_found`. A page of `start` and `limit`
   that is not a whole number is 400 `bad_request`.
5. `/busy` names no family. `prompts_in_flight` counts the prompts of each
   supervisor link and of each ACP door connection. `jobs_running` and
   `jobs_queued` are the dispatch door's `JobQueue`. `busy` is true when
   any count is above 0.

The session API (`/v1/sessions*`), the journal, the event streams, the
writer lease, the Open WebUI copy and the playpen channel left in the phase
2 plan, rows 21 to 25. Session files, journals and streams of an earlier
release are not read. Nothing migrates them. The facade owns each
conversation and the one-writer rule (Flue's queue).

## Trust rules

1. Every byte from a sandbox is a claim. A reader returns a refusal, never
   raises.
2. Nothing from a link is acted on. The host never opens a path, fetches a
   URL or runs a string that came from a sandbox.
3. `supervisor.lock` can delay a dial and can never grant one. Read the
   lock of the sandbox being dialled, never the family's. Time the counter,
   never a timestamp.
4. A fault clears only on evidence: a completed handshake or a service start.
5. A door's request body is untrusted input. `requests.py` parses it.
6. The status document is a claim. A document with no known `kind` opens no
   door and starts no queued job. Read the kind with `served_kind`.
7. A cap of a text from a sandbox is a count of bytes of UTF-8. Cut such a
   text with `lines.cut_bytes`. The cut is between two characters.
8. Frame every byte stream from a sandbox with `lines.LineSplitter`: LF
   only, a cap per line, a refusal for a line over the cap or not UTF-8.
9. The ACP door translates nothing. It checks the envelope and the size of
   a frame, and pipes the bytes. It keys a prompt id as the JavaScript
   facade reads it: a number by its double, a string apart from a number.
   It closes on a prompt whose number id is past the double range.

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
   `startable_sandbox()` holds that rule. The handshake is ACP `initialize`
   on the sandbox's facade (`acp_link.py`).
3. One supervisor link per sandbox, not per family. A draining family holds
   two.
4. Layers talk downward only: `api` to `service` to the links to `channel`
   to primitives.
5. Nothing here needs Node.
6. Build paths with `paths.py`.
7. Give each task a name and the done-callback `tasks.report_failure`. A
   task that ends with an error must reach the log.
8. Give each step of a loop that lives as long as the service a handler.
   The handler logs the failure, and the loop goes on. `_upkeep_loop` is the
   pattern. A link loop is the exception: a dead reader ends in a lost link.

## Switch rules

1. The status document outranks the pin. The pin lives in memory.
2. Release the outgoing sandbox before you answer. `_retire_sandbox` sends
   `_creche/shutdown` to the outgoing facade and stops its link.
3. A switch belongs to the platform, not to the HTTP request. A repeat joins
   the run. A refused run is forgotten.
4. The switch runs the handshake before anything moves: `initialize` on
   the incoming facade.
5. The pin is the family's choice of sandbox, not only a switch's.
6. Interrupt waits for nothing. Do not add a grace period.
7. A drain waits for the prompts in flight on the outgoing facade, up to
   the deadline. An interrupt stops the outgoing facade, and each of its
   jobs answers `failed` with `channel_lost`.

## Autonomous and dispatch rules

1. Only an autonomous family has a limit and a queue.
2. `queue_full` is checked before anything is written.
3. The target's own status document decides whether it may be dispatched to.
4. `door-dispatch` is its own principal.
5. A caller never names itself. `caller_family` comes from the token.
6. A dispatched job runs over ACP (`JobQueue`, `acp_jobs.py`). Its ledger
   entry is written only after every refusal, so a refusal leaves none.
7. The job keeps its `auto-<ulid>` id. The facade mints the conversation
   id, and the entry's `acp_session` maps one to the other.
8. The outcome record is written, then noted in the ledger, then the slot
   is freed and the next queued job starts.

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
| `SESSIOND_SESSIONS_ROOT` | the root of each family's sessions mount, the cwd of a job |
| `SESSIOND_STATE_ROOT` | tokens, faults, family status, outcome records, the dispatch ledger |
| `SESSIOND_WORK_ROOT` | the `code-sandbox` work root |
| `SESSIOND_SOCKET` | the Unix socket |
| `SESSIOND_BIND_LAN`, `SESSIOND_LAN_ADDRESS`, `SESSIOND_LAN_PORT` | the optional TCP listener, port 8350 |
| `SESSIOND_LOG_DIR` | where the stderr of each `sbx exec` of a family lands |
| `SESSIOND_LOCK_STALE_S`, `SESSIOND_LOCK_POLL_S` | the lock beat window and its poll |
| `SESSIOND_ACP_RELAY_COMMAND` | how one ACP door connection reaches the facade. `{env_file}` comes from the status document. |
| `SESSIOND_FACADE_COMMAND` | how the supervisor link starts each sandbox's facade. `{env_file}` comes from the status document. |

Six token files sit under `<state root>/tokens/`. Four are mode 0600. The
delegate and dispatch tokens are mode 0640, group `agents`, because the
chaperone reads them as another user. `SIGHUP` reloads them.

## Layout

| Module | Holds |
|---|---|
| `api.py`, `__main__.py` | the routes, `--check`, the signal handlers |
| `service.py` | the operations: the jobs, the ACP door, the switch |
| `requests.py` | every request body, validated before use |
| `queueing.py`, `outcomes.py` | the dispatch queues, what survives a job |
| `switching.py` | the bookkeeping of a switch: the pin and the runs |
| `channel.py`, `exec_channel.py`, `lines.py` | a byte channel and its fake, an `sbx exec` process, the LF framing and the byte cut |
| `supervisor_lock.py` | the proof that no older supervisor holds a sandbox |
| `acp_link.py`, `acp/schema-v1.json` | the ACP link to one sandbox's facade, and the pinned ACP schema |
| `acp_jobs.py` | one job over the link: `session/new`, one prompt, the deadline and its cancel |
| `acp_door.py` | `WS /acp/{family}` on the Unix socket: one `acp-relay` per connection, its beat, the envelope check, the count of its prompts, nothing translated |
| `acp_view.py` | the view: one relay per request, `session/list` or `session/load`, the transcript and its caps |
| `jobs.py`, `dispatch.py`, `workspace.py` | thin jobs, the dispatch and trigger ledgers, the per-chat work directory |
| `family_status.py`, `faults.py` | the status document, the fault file |
| `auth.py`, `config.py`, `paths.py` | the token files and the grants, the environment, every path |
| `ids.py`, `clock.py`, `atomic.py`, `errors.py`, `models.py`, `states.py` | the id grammars, the time, the file writes, the error body, the trigger record, the family kind |
| `tasks.py` | what a task says when it ends with an error |
| `verify.py` | `attendance-verify` |

## Tests

```bash
uv run pytest attendance/tests
```

`tests/fake_facade.py` is the far side of a link: in process over a
`FakeChannel`, or as a program that `fake_sbx.py` starts. Add a new
misbehaviour of a facade there. `tests/attendance_harness.py` holds the
fixtures: tokens, a status document, a config. A test that spawns a process
is marked `slow`.

## Known gaps

- The phase 2 plan names no family kind for the ACP door. `door-acp`
  reaches attended families only (`auth.py`).
- The phase 2 plan puts `_creche/beat`, `_creche/shutdown` and the
  `initialize` `_meta` keys in a vector file under `facade/vectors/`. That
  file does not exist yet. `acp_link.py` sends the beat as a request, sends
  the shutdown as a notification, and sends no `_meta`.
- ACP's prompt answer carries no usage. A delegate answer's `usage` is null.
- The phase 2 plan names no `_meta` keys for a job. `acp_jobs.py` sends
  `creche/job` on `session/new` and `creche/delegation` on
  `session/prompt`. The facade may ignore both, so contract 04 §6.3's chain
  reaches the PEP only if the facade forwards it.
- Approvals are chaperone gates, and a gate holds no turn (phase 2 plan,
  correction 9). An outcome record's `approvals` is null, and no turn
  reaches `waiting-approval`.
- A dispatched job sends its trigger in the prompt's `_meta` as
  `creche/trigger`. The facade may ignore it: the outcome record takes the
  trigger from the ledger entry.
- A restart forgets each queued or running dispatched job. Its entry then
  reads `ended` with a null outcome.
- An outcome record of a job over ACP has a null `spend_usd`: ACP reports
  no usage (`outcomes.py`).
- `caregiver` writes the status document's `channel` field from its own
  ledger. This service does not publish its `LinkStatus` there: it never
  calls `caregiver`, and contract 05 names `caregiver` the one writer.
- `X-Session-Id` is caller-set, so a model can aim a `code-sandbox` job at
  another chat's directory of the same family. The grammar stops any escape
  from the work root (`service.py`).
- No chat session lives here since row 21, so nothing deletes a chat's
  `code-sandbox` directory (`workspace.py`).
- No contract names `/trigger`. The phase 3 plan, row 14, names the route.
  Its body and its answer are this package's (`requests.py`).
- The facade's `session/list` row carries `sessionId`, `cwd`, `title` and
  `updatedAt` only. The view reads `created`, `job_id`, `delegation` and
  `trigger` from the row's `_meta` (`creche/created_at`, `creche/job`,
  `creche/delegation`, `creche/trigger`), so each reads null until the
  facade sends them. No vector names `creche/created_at` yet
  (`acp_view.py`).
- A view request does not start a facade. A family whose facade is not
  running answers 503 until a job or a switch starts it.
- The switch answer's `sessions` is 0: no session lives here
  (`service.py`).
- `ErrorCode` still holds the codes of the session API, such as
  `session_busy` and `lease_taken_over`. No route answers them now. The
  vectors of `session.error_body` list every code (`errors.py`).
- `config.playpen_log` keeps the file name `playpen-<family>.log` for the
  stderr of the facade and the relays (`config.py`).
- Contract 03 §8 gives each cap of a text in bytes. It gives no count of
  bytes for one half of a surrogate pair. Such a half has no UTF-8 form.
  `cut_bytes` counts it as three bytes and keeps it in the text (`lines.py`).
- Contract 05 §2.1 does not say what a reader does with a `kind` that is
  not one of its three words. `served_kind` refuses each door with
  `forbidden`. A family that never validated has an empty `kind` and gets
  `family_invalid` (`family_status.py`).
- Contract 05 §3 does not say what a reader does with a `state` that is not
  one of its four words. The reader keeps no state for it. The detail
  `family_state` of a refusal is then null (`family_status.py`).
- Contract 02 §14 gives no rule for the fields of `internal`. The handler
  answers null for `family`, `session` and `turn`, an empty `detail` and a
  fixed message (`api.py`).
- Contract 05 §5.3 rule 8 names no refusal for a switch on a status document
  with no known `kind`. `_check_switch` refuses that switch with
  `bad_request` (`service.py`).
- Contract 02 §3 rule 3 says that a body is JSON. It does not say what a
  reader does with the escape of one half of a surrogate pair. Such a text
  has no UTF-8 form. A parser refuses the body with `bad_request` for each
  text that an answer or a line of the link can hold. Two texts reach a
  file only: the reason of a switch and the key of a dispatch. A parser
  takes the escape in each of the two. A member that no parser reads can
  hold the escape too (`requests.py`).
- Contract 02 §3 rule 2 says never `0.0.0.0` and gives the LAN address no
  grammar. `from_env` refuses each spelling of the address of each interface.
  It takes each other text as the address (`config.py`).
