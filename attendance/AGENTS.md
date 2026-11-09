# attendance

The host side of each family's sessions. `attendance` makes each session row
on the vmm, routes each ACP request by session id over the vmm ACP stream
of that session, runs the delegate and dispatch jobs, and serves the view.
The vmm keeps the host session list and one VM per session (decision CH1).
The conversation of a session lives in the facade of its VM. The root
`AGENTS.md` applies here too.

It is Python because the host side may not need Node. The facade runs
inside the VM, which carries Node.

## What it does

1. Serves `/delegate`, `/dispatch`, `/dispatch/jobs`, `/trigger`,
   `/trigger/jobs`, the view routes and `/busy` over a Unix socket, and
   over the LAN address when the config asks. Serves `WS /acp/{family}` on
   the Unix socket only.
2. Calls the vmm over `vmm.sock` (decision CH20, `vmm.py`): `create`,
   `list`, `drop` and the `acp` Upgrade. Each route constant of
   `creche_wire.routes` pairs the request with its reply and error types.
3. Reports faults in a fault file. It never calls `caregiver`.
4. Runs one thin job per delegate call (`acp_jobs.py`): it mints a session
   id, makes the row with the `delegate` origin, opens the session's vmm
   stream, runs the handshake and one `session/prompt`, and drops the
   session at the end (decisions CH21-2, CH21-4).
5. Serves the dispatch door: `enqueue` and `job_status`. Each job is one
   session with the `auto` origin, as a thin job is. Only an autonomous
   family has a limit and a queue.
6. Serves the ACP door (`acp_door.py`). One connection serves every session
   of its family. attendance answers `initialize`, `session/new` and
   `session/list` itself, from the host list, with no boot (decision
   CH21-7). It routes each other request and notification by its
   `sessionId`, over one vmm stream per session. A message whose
   `params._meta` holds a key that starts with `creche/` gets
   `invalidParams` (-32602) and reaches no stream (contract 02 §5.1 rule
   5, decision CH21-4). An id that is not a
   session id gets `resourceNotFound` (-32002) before attendance calls the
   vmm, and an id with no row gets it from the vmm with no boot (decisions
   CH10-9, CH21-9). Each other vmm refusal is `internalError` (-32603) with
   the vmm code as `data.code` (decision CH83 rule 1). The door closes the
   stream of a session when no request is in flight on it, in either
   direction, and the connection stays open (contract 02 §5.5 rule 5,
   decision CH21-8). The next request of the session opens a new stream
   after the old one has closed. A stream that ends with a request in
   flight closes the connection with code 1001 and the reason `the relay
   ended`.
7. Serves the view for `noticeboard` (`acp_view.py`). The session list is a
   page of the host list, with no boot. A transcript opens the session's
   vmm stream, sends `initialize` and `session/load`, and closes the
   stream. It never sends `session/prompt`.
8. Serves `GET /busy` for `noticeboard`: the release tool's quiet window reads
   it in place of the session API.
9. Serves the trigger door for `door-trigger`. Each firing is one job in
   an autonomous family's `JobQueue`, beside the dispatched jobs.
10. Drops each job row at start, before it binds a socket
    (`SessionService.drop_job_rows`). No job outlives a restart, so each
    row whose origin is `delegate` or `auto` drops, and each `door` row
    stays (contract 02 §6 rule 5, decision CH83 rule 5).

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
5. The `auto` origin of the row carries the kind and the name, with no
   time (decision CH21-4a). The outcome record names the same trigger,
   with the time the job was enqueued.
6. The trigger ledger is `<state root>/trigger-jobs/<family>/`, apart from
   the dispatch ledger.

## The view and `/busy`

Each route takes the `noticeboard` token and no other. A vmm refusal other
than `not_found` and `busy`, a stream that ends, a line that is not strict
JSON-RPC and a read past 30 s each answer 503 `sandbox_unavailable`.

| Route | Calls | Answers |
|---|---|---|
| `GET /families/{f}/sessions?cursor=` | the vmm `list` | `{"sessions": [row], "next_cursor": str \| null}` |
| `GET /families/{f}/sessions/{id}?start=&limit=` | the vmm `acp`, then `session/load` | `{"session_id", "entries": [entry], "next": int \| null, "truncated": bool}` |
| `GET /busy` | nothing | `{"busy": bool, "prompts_in_flight": int, "jobs_running": int, "jobs_queued": int}` |

1. A row is `{id, title, created, updated, job_id, delegation, trigger}`,
   made from the host row (contract 02 §7 rule 2). The cursor is the
   decimal text of the vmm cursor.
2. An entry has a `position` from 0 and a `kind`. `user`, `agent` and
   `thought` carry `text`: consecutive chunks of one kind join. A
   `tool_call` carries `tool_call_id`, `title`, `status` and `result`, the
   text of its last update.
3. The caps: 64 KiB of UTF-8 per text, 256 bytes per id or title, 20 000
   updates per read (`truncated` is then true), `limit` 1 to 500 (default
   100) and 1 MiB of entries per page.
4. An unknown session is 404 `not_found`, with no boot. Another holder of
   the session's stream gives 409 `busy` (decision CH83 rule 4). A page of
   `start` and `limit` that is not a whole number is 400 `bad_request`.
5. `/busy` names no family. `prompts_in_flight` counts the prompts of each
   job in flight and of each ACP door connection. `jobs_running` and
   `jobs_queued` are the dispatch door's `JobQueue`. `busy` is true when
   any count is above 0.

The session API (`/v1/sessions*`), the journal, the event streams, the
writer lease, the Open WebUI copy and the playpen channel left in the phase
2 plan, rows 21 to 25. Session files, journals and streams of an earlier
release are not read. Nothing migrates them. The sessions of sbx are not
imported: an old id is not found (decision CH10-9). The facade owns each
conversation and the one-writer rule (Flue's queue).

## Trust rules

1. Every byte from a VM is a claim. A reader returns a refusal, never
   raises.
2. Nothing from a stream is acted on. The host never opens a path, fetches
   a URL or runs a string that came from a VM.
3. A reply of the vmm is read with `creche_wire.read` under
   `HOST_API_BODY_CAP`. A reply of another shape is the code `unreadable`.
4. A fault clears only on evidence: a vmm reply with no mismatch or a
   service start (contract 05 §3.3.1 rule 8).
5. A door's request body is untrusted input. `requests.py` parses it.
6. The status document is a claim. A document with no known `kind` opens no
   door and starts no queued job. Read the kind with `served_kind`.
7. A cap of a text from a VM is a count of bytes of UTF-8. Cut such a
   text with `lines.cut_bytes`. The cut is between two characters.
8. Frame every byte stream from a VM with `lines.LineSplitter`: LF only, a
   cap per line, a refusal for a line over the cap or not UTF-8.
9. The ACP door translates nothing that it routes. It checks the envelope
   and the size of a frame, and pipes the bytes. A client frame may end in one LF or one
   CR LF. The door strips that ending before the check. Each other line
   break closes the connection. It keys a prompt id as the JavaScript
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
2. Each new vmm stream starts with the handshake: ACP `initialize`, with
   the answer kept by attendance (`acp_link.handshake`).
3. One vmm stream per session and per holder: one door connection, one job
   or one view read (decisions CH33 rule 7, CH83 rule 4). attendance never
   sends an internal `session/new` or `session/load` (decision CH21-8).
   A stream closes when no request is in flight on it, so no holder keeps
   an idle VM awake (decision CH21-8). Its close waits for the vmm to end
   the stream (`AcpStream.close`).
4. Layers talk downward only: `api` to `service` to the door, the jobs and
   the view, to `vmm` and `acp_link`, to `channel` and primitives.
5. Nothing here needs Node.
6. Build paths with `paths.py`.
7. Give each task a name and the done-callback `tasks.report_failure`. A
   task that ends with an error must reach the log.
8. Give each step of a loop that lives as long as the service a handler.
   The handler logs the failure, and the loop goes on. `_upkeep_loop` is the
   pattern. A link loop is the exception: a dead reader ends in a lost link.
9. A vmm that does not answer is transient (decision CH20): ENOENT,
   ECONNREFUSED and EOF give the code `unreachable`, and the next request
   tries again. The unit orders `After=creche-vmm.service` and has no
   `Requires=`.

## Autonomous and dispatch rules

1. Only an autonomous family has a limit and a queue.
2. `queue_full` is checked before anything is written.
3. The target's own status document decides whether it may be dispatched to.
4. `chaperone-dispatch` is its own principal.
5. A caller never names itself. `caller_family` comes from the token.
6. A dispatched job runs over ACP (`JobQueue`, `acp_jobs.py`). Its ledger
   entry is written only after every refusal, so a refusal leaves none. A
   dispatch with no chain is refused at `POST /dispatch`, because the
   `dispatch` trigger of the origin needs 1 to 8 families (decision
   CH21-4a). The ledger reader drops a dispatch entry with no chain.
7. The job keeps its `auto-<ulid>` id. attendance mints the session id of
   its row, and the entry's `acp_session` maps one to the other.
8. The outcome record is written, then noted in the ledger, then the slot
   is freed and the next queued job starts.

## Mount rules

Each session's `cwd` is the sessions mount of its family. Contract 01
minor 18 drops `job.caller_root` (decision CH10-10). A job runs in a VM of
its own, dropped at job end (decisions CH1, CH21-2), so no chat gets a
directory. No family name is in this code.

1. Never name a fixed in-VM path. The `cwd` of a row is a display value,
   never a guest path (decision CH21-4). `ATTENDANCE_SANDBOX_SESSIONS_MOUNT`
   is the one override, and only the integration harness sets it.

## Run it

```bash
uv run python -m attendance --check   # validate the environment and every token file
uv run python -m attendance           # serve
```

| Variable | Meaning |
|---|---|
| `ATTENDANCE_SESSIONS_ROOT` | the root of each family's sessions mount, the cwd of a job |
| `ATTENDANCE_STATE_ROOT` | tokens, faults, family status, outcome records, the dispatch ledger |
| `ATTENDANCE_SOCKET` | the Unix socket |
| `ATTENDANCE_BIND_LAN`, `ATTENDANCE_LAN_ADDRESS`, `ATTENDANCE_LAN_PORT` | the optional TCP listener, port 8350 |
| `ATTENDANCE_VMM_SOCKET` | `vmm.sock`. The unit sets it to `%t/vmm/vmm.sock` (decision CH20). No default. |

Five token files sit under `<state root>/tokens/`. Three are mode 0600. The
delegate and dispatch tokens are mode 0640, group `agents`, because the
chaperone reads them as another user. `SIGHUP` reloads them.

## Layout

| Module | Holds |
|---|---|
| `api.py`, `__main__.py` | the routes, `--check`, the signal handlers |
| `service.py` | the operations: the jobs, the ACP door checks, the view, `/busy` |
| `requests.py` | every request body, validated before use |
| `queueing.py`, `outcomes.py` | the dispatch queues, what survives a job |
| `vmm.py` | the client of `vmm.sock`: `create`, `list`, `drop`, the `acp` Upgrade and its stream |
| `channel.py`, `lines.py` | a record stream to a facade, the LF framing and the byte cut |
| `acp_link.py`, `acp/schema-v1.json` | the handshake and the ACP link of one session, and the pinned ACP schema |
| `acp_jobs.py` | one job: the row, the stream, one prompt, the deadline and its cancel, the drop |
| `acp_door.py` | `WS /acp/{family}` on the Unix socket: the answers from the host list, one stream per session, the envelope check, the count of its prompts |
| `acp_view.py` | the view: a page of the host list, or one stream and `session/load`, the transcript and its caps |
| `jobs.py`, `dispatch.py` | thin jobs, the dispatch and trigger ledgers |
| `family_status.py`, `faults.py` | the status document, the fault file |
| `auth.py`, `config.py`, `paths.py` | the token files and the grants, the environment, every path |
| `ids.py`, `clock.py`, `atomic.py`, `errors.py`, `models.py`, `states.py` | the id grammars, the time, the file writes, the error body, the trigger record, the family kind |
| `tasks.py` | what a task says when it ends with an error |
| `verify.py` | `attendance-verify` |

## Tests

```bash
uv run pytest attendance/tests
```

`tests/fake_vmm.py` is the vmm: the rows and one `FakeFacade` per stream.
`tests/fake_facade.py` is the far side of a stream, over a
`tests/fake_channel.py` `FakeChannel`. Add a new misbehaviour of a facade
there. `tests/attendance_harness.py` holds the
fixtures: tokens, a status document, a config. A test that spawns a process
is marked `slow`.

## Known gaps

- The phase 2 plan names no family kind for the ACP door. `door-acp`
  reaches attended families only (`auth.py`).
- ACP's prompt answer carries no usage. A delegate answer's `usage` is null.
- Approvals are chaperone gates, and a gate holds no turn (phase 2 plan,
  correction 9). An outcome record's `approvals` is null, and no turn
  reaches `waiting-approval`.
- A restart forgets each queued or running dispatched job. Its entry then
  reads `ended` with a null outcome.
- An outcome record of a job over ACP has a null `spend_usd`: ACP reports
  no usage (`outcomes.py`).
- No contract names `/trigger`. The phase 3 plan, row 14, names the route.
  Its body and its answer are this package's (`requests.py`).
- `ErrorCode` still holds the codes of the session API, such as
  `session_busy` and `lease_taken_over`. No route answers them now. The
  vectors of `session.error_body` list every code (`errors.py`).
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
- Contract 02 §3 rule 3 says that a body is JSON. It does not say what a
  reader does with the escape of one half of a surrogate pair. Such a text
  has no UTF-8 form. A parser refuses the body with `bad_request` for each
  text that an answer or a line of the link can hold. One text reaches a
  file only: the key of a dispatch. A parser takes the escape in it. A
  member that no parser reads can hold the escape too (`requests.py`).
- Contract 02 §3 rule 2 says never `0.0.0.0` and gives the LAN address no
  grammar. `from_env` refuses each spelling of the address of each interface.
  It takes each other text as the address (`config.py`).
- CONTRACT-QUESTION: decision CH20 fixes `/run/user/<uid>/vmm/vmm.sock` and
  names no env key. The env catalog cannot spell a uid, so the unit sets
  `ATTENDANCE_VMM_SOCKET` from `%t`, outside the catalog (`vmm.py`). The
  reading of caregiver is the same.
- CONTRACT-QUESTION: decision CH83 rule 1 names the JSON-RPC error of a
  vmm refusal on the ACP door, and no row names the error of a facade whose
  handshake answer is not usable after 101. The door answers
  `internalError` with `data.code` `protocol_violation` (`acp_door.py`).
- The ACP door opens the stream of a session in the read loop of its
  connection. A boot of one session therefore delays the next frame of
  each session of that connection.
- A vmm stream that ends with a request in flight closes the door
  connection, as a relay that ended did. The client opens the session again
  with `session/load` (decision CH19 (6)).
- CONTRACT-QUESTION: decision CH83 rule 4 gives a session one holder, and
  no row says how a holder ends its stream so that its next Upgrade meets
  no claim of its own. `AcpStream.close` ends the write half, reads to the
  vmm's end and waits at most `CLOSE_WAIT_S` (5 s). Then it releases the
  socket (`vmm.py`).
- The outcome record's `sandbox` is the empty text: a job runs in a VM of
  its own session, which no sandbox id names (decision CH83 rule 3).
- A job whose vmm drop fails leaves its row. The log names it. The next
  start of attendance drops it (contract 02 §6 rule 5).
- CONTRACT-QUESTION: decision CH83 rule 5 drops each job row at start and
  names no step for a vmm that does not answer then. A list or a drop that
  the vmm refuses leaves the rows, and the log names the family. The next
  start tries again (`service.py`).
- `caregiver` reads only the codes of minor 17 from the fault file of this
  service, so it drops `vmm_protocol_mismatch` until plan PR CR10 adds the
  code to its table.
