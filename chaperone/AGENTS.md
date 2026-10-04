# chaperone

The Policy Enforcement Point (PEP). Every action a sandboxed agent takes
passes through `POST /call`. The chaperone is a deterministic reverse proxy.
The same inputs give the same decision, every time.

It runs as its own user, as the one hardened system unit, on port 8300 of
the host's LAN address. It holds every upstream credential. A sandbox holds
none. The root `AGENTS.md` applies here too.

## Non-negotiables

1. **Fail closed.** Any unhandled error inside `/call` becomes a denial. A
   malformed grant file reads as no reach. An upstream that fails to start
   leaves its tools absent from every manifest.
2. **No LLM anywhere in this package.** No model call, no heuristic, no
   summarizer. `gates.approval_summary` is deterministic string formatting.
3. **`decide_family()` is pure.** No I/O, no clock, no globals. Gate state,
   execution and audit live in `family_app.py`.
4. **100% branch coverage on every decision input.** `family_decisions.py`,
   `verbs.py`, `argschema.py`, `fences.py`, `headers.py`,
   `family_grants.py`, `ha_data.py`, `faults.py`, `gates.py`,
   `gatekeeper.py` and `delegations.py`. A new branch needs a new test.
5. **Audit before you return.** An allowed call that cannot be recorded
   answers 500, even when the effect happened.
6. **Minimal dependencies.** fastapi, uvicorn, httpx, pyyaml, mcp, handover
   and the standard library. Do not add a framework or a retry library.
7. **`__init__.py` exports nothing.** `app` imports the whole `mcp` stack.
   The `bin` tests import `family_grants` and `family_decisions` directly.

## One identity kind

A bearer resolves to a family, or to nothing. There is no second kind. A
client that can pick its identity kind has picked its own permissions.

| Route | Bearer | Rule |
|---|---|---|
| `GET /healthz` | none | `chaperone-verify` calls it. Body: `ok`, `roster`, `upstreams`. |
| `GET /manifest` | family token | A bearer that resolves to nothing is 403 `unknown_token`. |
| `POST /call` | family token | The same refusal. |
| `POST /approval/{gate}` | `approval_callback_token` | The phone tap. Not a family token. It grants nothing else. |

## Module map

| Module | Owns |
|---|---|
| `app.py` | HTTP, the routes, the body cap, the rate window, the lifespan, the unidentified log |
| `__main__.py` | environment to `PepConfig`, then uvicorn |
| `site.py` | the site values: the LAN address, the TEI URL, the Home Assistant URL |
| `family_app.py` | the service layer: manifest, call, execution, audit wiring |
| `family_decisions.py` | the pure decision core |
| `family_grants.py` | token to grant file, stat-cached. The on-disk contract with `caregiver` |
| `family_audit.py` | audit record v2 |
| `headers.py` | the three advisory headers: validated, bounded, never trusted |
| `faults.py`, `fault_sweep.py` | the `grants_stale` fault, and the loop that clears it with no call |
| `delegate.py`, `delegations.py` | the `invoke_agent` client, and the delegation ids this process mints |
| `dispatch.py` | the `enqueue` and `job_status` client. Its own bearer. |
| `release_door.py` | the `release` verb: one request file, through `handover.requester` |
| `gates.py`, `gatekeeper.py` | the gate id and summary, and the gate that blocks in place |
| `verbs.py`, `argschema.py` | the verb catalog, transcribed, and its validator |
| `ha_data.py` | the `ha_call.data` fence |
| `audit.py` | append-only daily JSONL, shared by both logs |
| `fences.py` | upstream argument fences |
| `mcp_client.py` | the stdio upstream pool, one credential per server |
| `bounded_yaml.py` | the YAML read of the roster and of the secrets file, with two limits on merge keys and one limit on aliases |
| `run_as.py` | `chaperone-as`: one child switched to its `mcp-<name>` user before exec |
| `reload_pool.py`, `reload_wiring.py` | a pool whose roster moves at `SIGHUP` |
| `secrets.py` | both secret layouts: the monolith and one file per secret |
| `verify.py` | `chaperone-verify`, the component's verify hook |

A shared module may not grow a branch that only one caller reaches.

## Rules of the family path

- An advisory header never reaches a decision. A bad header reads as absent.
  Do not make a bad header deny a call.
- The schemas in `verbs.py` are the contract's own documents. Do not
  regenerate them. A schema change is a contract change.
- `argschema.py` implements only what the catalog uses. `assert_supported`
  runs at import, so an unknown keyword stops the process at start.
- `ha_data.py` refuses a target smuggled through `data`. The tests in
  `test_ha_data.py` pin every verdict.
- Every identifier pattern ends in `\Z`.
- A fault clears without traffic. `fault_sweep.py` re-reads the grant file of
  each faulted family on a timer. It visits no healthy family. It never takes
  the process down.
- `PEP_REWORK_DIR` is required. Without it the chaperone resolves no bearer.
- An execution this chaperone is not configured for stays a seam. The
  decision denies with `not_implemented`, and the manifest leaves the action
  out. Never default a seam on.
- `requested_by` is the family the bearer resolved to. Never an argument,
  never a header.
- Only the chaperone mints a delegation id. `X-Delegation-Id` is only an
  index into that table.
- A gate is single use and stores no decision. A tap with no caller waiting
  closes the gate and authorizes nothing.
- `release_door.py` imports `handover.requester` and never
  `handover.executor`. The `release` verb approves nothing. It files an
  intent.
- A call has no effect when no audit line can hold its arguments in full.
  An allow becomes `internal_error`. A denial keeps its reason.
- Only the line of a denial can hold the marker for such arguments. For
  each other decision, the writer writes the line and raises `AuditError`.
- A result that is not strict JSON in UTF-8 is an upstream failure. The
  audit line says so before the answer goes out.
- A route never answers 500 with no `reason`. For a failure that no layer
  handles, the route answers `internal_error`, row 12 of contract 04 §5.
  A call of a family still gets its audit line.

## Identity and revocation

`FamilyStore.lookup` re-stats the grant file on every call and compares
`sha256(token)`. Revocation is `caregiver` removing the digest or the file.
The next call is 403 with zero IPC. Do not add a cache that survives a
deleted file. Do not add an in-memory session for a family.

## Credentials

The sops file decrypts under the chaperone user only. Values live in process
memory and reach an MCP upstream through `secret:<key>` env references. A
sandbox never sees an upstream credential. The two approval bearers ride in
the sops file, not in the unit, because the unit is in git.

A secret that fails to load is a per-upstream failure. The chaperone still
serves verbs. One exception: a file that decrypts and does not parse stops
the chaperone at start, with exit 78. An error about a decrypted file names
its position, never its content.

## `run_as.py`

Every upstream starts as `chaperone-as mcp-<name> <command> <args...>`. The
launcher switches the child to `mcp-<name>` and execs the command.

1. The user comes from `ServerFile.user`. Never spell the `mcp-` prefix twice.
2. The row's variables cross the launcher as data, in `PEP_CHILD_ENV`. They
   become an environment only at the server's exec.
3. Every capability set is emptied before the exec, and the kernel is asked.
   The launcher reads `/proc/self/status` back and refuses on anything but
   four empty sets and `no_new_privs`.
4. Refuse, never fall back. A launcher that cannot switch exits 125. Never
   start a server as the chaperone user.
5. The chaperone cannot signal its children. Closing stdin is the only stop.

A test cannot `setuid`. `tests/fake_run_as.py` is the real launcher with the
user lookup and the switch faked.

## `reload_pool.py` and `reload_wiring.py`

The trigger is `SIGHUP` and must stay one. A listener would be a second
authenticated path into the process that holds every credential.

1. Route by generation, not by name. A replace needs two generations live.
2. Retirement never cancels an owner task. It sets the closing event and
   awaits the task.
3. `call()` reads its generation and increments the in-flight count with no
   `await` between them.
4. The drain bound stays between `UPSTREAM_CALL_TIMEOUT_S` and `IDLE_TTL_S`.
   The module asserts it at import.
5. The family path reads `serving`, never a copy.
6. `reload_once` never raises. Every failure is a log line, and the old set
   keeps serving.
7. A roster the chaperone cannot read is not an empty roster. The chaperone
   starts with no MCP server, `/healthz` says `unreadable`, and the next
   `SIGHUP` reads again. Never read the roster in `__main__`.
8. A `sops` failure keeps the credentials already in memory.
9. One reload task at a time, with a repeat flag.
10. The read of the roster and of the credentials runs on a worker thread,
    not on the thread of the event loop. `sops -d` can take seconds, and
    the loop holds every call.

Both secret layouts are read, and that is permanent. The monolith holds every
credential that predates the intake. The per-secret store is the only layout
the host can author. A name in both takes the per-secret value.

## Configuration

| Variable | Meaning |
|---|---|
| `AGENT_LAN_ADDRESS` | From the site file. The bind address and the TEI host. Required. |
| `PEP_REWORK_DIR` | Required. `grants/`, `audit/` and `faults/pep/` hang off it. |
| `PEP_AUDIT_DIR` | Required. The log for a request that resolved to no family. |
| `PEP_UPSTREAMS`, `PEP_UPSTREAMS_GENERATED` | The base roster and the roster root writes after an MCP release. Set both, or a released server is never served. |
| `PEP_SECRETS`, `PEP_SECRETS_DIR` | The monolith and the per-secret store. |
| `PEP_SESSIOND_SOCKET`, `PEP_DELEGATE_TOKEN_FILE`, `PEP_DISPATCH_TOKEN_FILE` | The `attendance` socket and the two door bearers. Each file is read on each call, not at start. |
| `PEP_RELEASE_REQUESTS_DIR` | Root's release spool. Set, the `release` verb files a request. Unset, it answers 501. |
| `PEP_APPROVAL_URL` | The protected hook a gate is pushed to. |
| `PEP_FAULT_SWEEP_INTERVAL_S` | How often a faulted family's grant file is re-read. |
| `PEP_BIND`, `HA_URL` | Explicit overrides of the site values. |

The bind is `host:port`. The chaperone refuses each bind in this list. It
writes one line and exits with 78 (`site.py`).

1. A text that is not `host:port`.
2. A port that is not a number from 0 to 65535.
3. A bind with no host. The chaperone has no default address.
4. A host that Python cannot give to the resolver: a host name with an
   empty label, or with a label of more than 63 characters.
5. A host that stands for each interface of the host, in each spelling:
   `0.0.0.0`, `0`, `::` and `*` are four of them. The check applies to
   `PEP_BIND` and to `AGENT_LAN_ADDRESS`.

## Tests

```bash
uv run pytest chaperone/tests
```

`tests/stub_mcp_server.py` is a real stdio MCP server, started through
`tests/fake_run_as.py`. Nothing in the suite needs sops, LiteLLM, a sandbox
or a network.

## Known gaps

Each line is an open contract question and the module it lives in.

- The family manifest marks no write tool (`family_app.py`).
- The delegate door's answer is unbounded by contract, so the chaperone
  truncates each answer string at 64 KiB (`delegate.py`).
- One tap for two identical calls on one gate: the first waiter runs, the
  other is denied as spent (`gatekeeper.py`).
- An abandoned gate keeps its remaining time and its push. The phone rail has
  no withdraw leg (`gatekeeper.py`).
- A cut summary ends with the number of fields it hid (`gates.py`).
- A deleted family keeps its fault and one `stat` per sweep until restart
  (`family_grants.py`, `fault_sweep.py`).
- A request that resolves to no family goes to `PEP_AUDIT_DIR` in a shape of
  its own (`app.py`).
- The chaperone holds no `CAP_KILL`, so an MCP server that ignores stdin EOF
  outlives its retirement until the unit stops (`mcp_client.py`, `run_as.py`).
- A call held at a gate across a reload runs under the fences its decision
  read (`family_app.py`).
- An `embed` reply can hold an empty vector. Contract 04 §4.1 gives no
  minimum length (`family_app.py`).
- An `embed` reply can hold `true` or `false` in the vector. Contract 04
  §4.1 does not say if a boolean is a number (`family_app.py`).
- A `model_id` of `/info` that is not a string reads as its `str()`.
  Contract 04 §4.1 gives the id as a string (`family_app.py`).
- No audit line holds a string that is not Unicode text, or arguments that
  nest deeper than the interpreter recurses. The record holds a marker in
  their place (`family_audit.py`, `family_app.py`).
- A refused body whose echo is not JSON text answers 422 with the type and
  the message of each error only (`app.py`).
- The log of a request of no family counts a string that is not Unicode
  text as its `surrogatepass` bytes (`app.py`).
- No contract gives a limit for the merge keys of a roster or of the
  secrets file. The two readers use the limits of the Rust reader of a
  manifest: a chain of 128 merge keys, and 65,536 copied pairs
  (`bounded_yaml.py`).
- No contract gives a limit for the aliases of a roster or of the secrets
  file, and no Rust reader has one. The two readers refuse a file whose
  aliases stand for more than 262,144 nodes (`bounded_yaml.py`).
