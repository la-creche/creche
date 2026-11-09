# caregiver

The family manager. It reads `family.toml` through `agent_family` and
converges the host to it. The root `AGENTS.md` applies here too. For each
family it:

- mints the LiteLLM key and the chaperone token,
- writes the table of the family in `policy.toml` and the family config
  directory,
- writes `egress.json` and `repos.json`, which the vmm reads,
- generates the timers an autonomous family asks for,
- asks the vmm to drop the sessions of a deleted family.

It starts and stops no VM. The vmm starts one VM per session at its first
prompt and stops it itself (decisions CH1, CH21-1).

It is a reconciler, not a command the operator runs. `caregiver serve`
watches the registry and converges every family to its file. Families
converge side by side, four at a time by default. The other verbs exist for a
host where something has gone wrong.

## Layout

| Module | Owns |
|---|---|
| `paths.py`, `clock.py`, `atomic.py` | every host path, the one timestamp, the atomic write and the JSON read |
| `lan.py` | the LAN address from the site file |
| `litellm_keys.py`, `credentials.py`, `config_mount.py` | the key, the token, the config directory |
| `reach_files.py` | `egress.json` and `repos.json` beside the config directory (contract 01 §6.1.1) |
| `vmm.py` | the one call this service makes to the vmm: the drop of a family (decisions CH20, CH21-10, CH25) |
| `policy_file.py` | the types and the text of `policy.toml`, and `build_app` |
| `policy_fleet.py` | one fragment per family, and the checked assembly of the one `policy.toml` |
| `faults.py`, `chaperone_watch.py`, `status.py` | the fault files, the one chaperone probe, the status document |
| `webhook_tokens.py`, `timers.py` | one bearer per webhook, and the generated timer units |
| `applied.py`, `steps.py`, `reconcile.py` | the live snapshot, one function per step, one pass |
| `rotate.py`, `loop.py`, `apply.py`, `delete.py` | rotate, serve, the one-shot apply, the teardown |
| `live_manifest.py` | the ledger of root, read |
| `cli.py` | `serve`, `reconcile-once`, `rotate`, `apply-once`, `status`, `delete` |
| `verify.py` | `caregiver-verify` |

## Rules

- **Layers talk to the layer below, never sideways.** `apply.py`,
  `reconcile.py`, `rotate.py`, `loop.py` and `delete.py` call more than one
  sibling. `policy_fleet.py` calls
  `policy_file.py`. Every other module talks only to `paths.py`,
  `clock.py` and `atomic.py`.
- Every external dependency sits behind a Protocol. The fake of `VmmClient`
  lives in `tests/caregiver_helpers.py`, never in the package.
- `atomic.py` is the only place that writes a file another process reads.
- `atomic.read_json` reads a JSON file. It answers `None` for content that
  it cannot read. It does not raise on content.
- `live_manifest.py` and `verify.py` keep a read of their own. Each one
  refuses an integer past the digit limit and nesting past the limit of
  the parser. Each one refuses bytes that are not UTF-8.
- A client maps each failure of its transport and of its answer to its own
  error: `LiteLLMError`, `VmmError`. A step names that error in its
  handler, with one exception.
- `delete_family` has no handler. In `serve`, its error ends in a handler
  of the loop. In the `delete` verb it ends in `cli.main`: one line, and
  exit code 1.
- Credentials die before processes. `delete.py` removes the LiteLLM key,
  then the fragment of `policy.toml` and its table, then `creds.json`,
  `egress.json`, `repos.json`, `config/` and the webhook bearers, then
  asks the vmm to drop every session of the family, then removes the
  state directory (contract 05 §4.4, decisions CH21-10, CH33 rule 3, CH69
  rule 23). `test_delete.py` checks the order from inside the fake vmm.
- A drop that fails keeps the state directory, so the next look runs the
  delete again. The vmm answers `Dropped` for a family with no rows.
- `vmm.py` sends the header `creche-host-api-protocol` on each request and
  reads each reply with `creche_wire.read` under `HOST_API_BODY_CAP`. A
  mismatch reply is sent again 2 s later, 3 requests in all, the numbers
  of attendance (decision CH33 rule 10). The last mismatch is `VmmError`
  with the code `vmm_protocol_mismatch` (decisions CH25 rule 1, CH82 rule
  5). ENOENT, ECONNREFUSED and EOF are `unreachable`, a transient fault
  (decision CH20).
- `serve` and `delete` read the socket of the vmm from
  `CAREGIVER_VMM_SOCKET`. The unit sets it to `%t/vmm/vmm.sock`, the path
  of decision CH20. With no value, the verb writes one line that names the
  key when it would act, and its exit code is 78.
- Each pass and `apply-once` write `egress.json` of the family (decision
  CH69 rule 6). It is `FamilyEgress` of
  `creche-wire`: hosts in lower case, an explicit port (a bare host is
  443), no entry twice, sorted by host and then by port (decision CH69
  rule 5, CH70 rule 4). A family with no `egress` gets `{"hosts": []}`.
  The planes never enter it (decision CH6-2). The file sits beside
  `config/`, mode 0640, and bytes equal to the file on disk write nothing.
  A write of either reach file is the step `set_egress` (contract 05
  §3.4).
- A revision whose `egress.json` would pass `EGRESS_CAP` of
  `reach_files.py`, the 64 KiB cap of the vmm (decision CH70 rule 1), is
  refused (decision CH71). The pass and `apply-once` render the file
  before the key step, so nothing lands. The family reads `invalid` with
  one error that names the cap, and the last good `egress.json` stays, as
  for a refused table of `policy.toml` (invariant 19).
  `rust/crates/wire/vectors/family.json` holds the cap, and
  `test_reach_files.py` holds `EGRESS_CAP` equal to it.
- Each pass and `apply-once` also write `repos.json` of the family, next
  to `egress.json` (decisions CH69 rules 4 and 6). It is `FamilyRepos` of
  `creche-wire`: the `repos` list, sorted. A family with no `repos` gets
  `{"repos": []}`. Mode and byte compare are those of `egress.json`.
  `check_family` refuses a name given twice (decision CH73 rule 3) and a
  list past 32 names (decision CH69 rule 15) before any write, so the
  last good file stays. The longest valid list renders to 3476 bytes,
  under the 4 KiB `REPOS_CAP` of the vmm (decision CH73 rule 2), so the
  file has no refusal of its own. `test_reach_files.py` holds that bound
  with `repos_cap` of `rust/crates/wire/vectors/family.json`.
- No secret on argv, in a URL or in a log line. The master key comes from
  `LITELLM_MASTER_KEY` in the environment.
- Each verb that applies the registry (`serve`, `reconcile-once`,
  `rotate`, `apply-once`) reads the fence of the host from its environment:
  `AGENT_PLATFORM_FAMILIES` (`agent_family.fences`). The unit reads it from
  the site file. With an entry of the wrong shape, the verb writes one line
  that names the key, and its exit code is 78. `LoopConfig` and
  `apply_once` take the fences, with no default. Contract 01 minor 18 drops
  `AGENT_MOUNT_ROOTS` (decision CH10-10), and the `limits` block carries
  no `caller_root`.
- A verb that acts refuses to start with no master key. It writes one
  line that names the variable, and its exit code is 1.
- The chaperone watch probes once per interval for the whole fleet. A probe
  answers only for a 200 whose body is a JSON object with `ok` true, the
  `{"ok":true}` of the Rust chaperone. A probe that raises, hangs, answers
  503 or answers another body is a probe that did not answer. A moved
  verdict forces a pass.
- `chaperone.watch: off` is a value, not a silence. `status` prints it.
- No status field claims what this service cannot observe. It cannot count
  running turns, so it does not.
- The table of the family follows what it should hold, not the family
  file's diff.
- `policy_fleet.py` holds the cap of `policy.toml` as
  `POLICY_FILE_MAX_BYTES`, 1 MiB. The Rust chaperone holds the same number
  as `POLICY_FILE_CAP`. `test_the_cap_is_the_rust_policy_file_cap` fails
  when the two numbers differ.
- The applied snapshot moves at the end of a successful pass. An interrupted
  pass leaves the old one.
- The applied copy of a family file is `applied/family.toml`. The name
  comes from `agent_family.registry`.
- A pass copies the registry file that the report of the family names. A
  file with another name than `family.toml` gives no copy. The pass is then
  not done, and the old snapshot stays.
- A write of the snapshot first writes the copy, then `applied.json`.
- A copy that does not parse reads as nothing applied. A copy that
  caregiver cannot read also gives nothing applied. caregiver reads no
  `applied/family.yaml`.
- Narrowing runs before widening, everywhere. Egress removals before allows.
  Timer removals before timer writes.
- Every pass converges a timer's enabled state, not only its file.
- `Actors` has no defaults.
- A pass that raises earns a backoff, 5 s doubling to 300 s. A registry
  edit clears the backoff.
- `loop._keep_fresh` restamps a document nothing is about to publish for.
- `restamp_status` rewrites only a document that this process published. A
  document from before a restart is the verdict of another process.
- A family that waits for a slot after a restart gets one look.
  `loop._look_at` runs the pass of that family beyond the bound, and it
  publishes the document of this process.
- A pass that is `reconciling` and ran no step names the step `validate`.
- An empty registry deletes no family.
- A directory under `families/` with no `family.toml` gets no pass and no
  delete. `reconcile.has_family_file` is the test for it. `apply.apply_once`
  holds a copy of that test, because `apply.py` does not call
  `reconcile.py`.
- `rotate` deletes the old key before it mints the new one. The token
  overlaps. The key does not.
- A token is 32 random bytes as 64 lower-case hex characters. The Rust
  chaperone accepts no other form. `read_creds` reads a `creds.json` with a
  `pep_token` of another form as absent, so the next pass mints a new key
  and a new token. It reads a `previous_pep_token` of another form as no
  overlap. The current token stays.
- `rotate` publishes the new epoch. It writes the credentials block of
  the status document and no other field, so `written_at` stays. A write
  of the block that fails is one error line, and the rotation continues.
- `rotate` takes a `ValidFamily`. Only `rotate.valid_family` makes one. The
  report must have no error, and the applied snapshot must not refuse the
  edit.
- A family file that is invalid or refused does not stop a rotation.
  `rotate_serving` takes the key's router and budget from the applied
  snapshot. It writes the token digests into the fragment of the family.
- `settle` writes the token digests alone. It reads no family file.
- An `invalid` status document carries the credentials block. `attendance`
  reads the epoch from that document alone.
- Never build a grant from the applied snapshot. `all` expands against
  the current server files, and no validation approved that result.
- A cron line with no `OnCalendar` spelling gets no unit.
- A fault's `blocks_turns` comes from `faults.BLOCKS_TURNS_BY_CODE`, never
  from the file.
- Each pass folds three fault files into the document: `faults/attendance/`,
  `faults/pep/` and `faults/vmm/` (contract 05 §3.3.1, decision CH21-1).
  `faults.FAULT_CODES_BY_SOURCE` names the codes each writer may raise. An
  entry with a code of another writer is dropped. No fault of the vmm and
  no `vmm_protocol_mismatch` blocks turns (decision CH82 rules 2, 3 and 6).
- The status document holds no `sandboxes` list (contract 05 minor 18,
  decision CH21-1). The reconcile block holds no `needs_switch`, and its
  `step` is a `status.ReconcileStep`, one of the six names of contract 05
  §3.4 minor 20.
- `config_rev` and `applied_rev` are the same value: the registry revision.
- `runtime.json` holds `model_live_at` next to `model_alias`, its one home
  (decision CH23-1). A first write leaves the key out (decision CH45 rule
  1). A write with a new alias sets it `MODEL_CACHE_S` (10 s) ahead, in the
  same swap, as integer milliseconds since 1970 in UTC (contract 01 §6.1
  rule 5, decisions CH22-1, CH31). It is the one time of this package that
  is not RFC 3339, because the vmm reads it. A write with the same alias carries the old value. The sync
  check of a pass reads it from there, and `applied.json` holds `rev` and
  `applied_at` alone.
- `config/shape.json` is `FamilyShape` of `creche-wire`: `cpus`, `flavor`
  and `memory_mib` from `sandbox.cpus`, `sandbox.image` and
  `sandbox.memory`, with `1g` as 1024 MiB (decisions CH36 G6, CH43 rule
  3). It changes in the same swap as the rest of `config/` (decision CH45
  rule 2). The vmm reads it, and the guest does not.
  `test_config_mount.py` reads it back as each accepted `FamilyShape`
  vector of `rust/crates/wire/vectors/wire.json`.
- `runtime.json` holds `sandbox_tools` as the list of the family file, in
  file order. The names are Flue's six: `read`, `write`, `edit`, `bash`,
  `grep`, `glob`. A family file with no `sandbox_tools` gets `[]`.
  `codemode` is not in the list. The agent mounts it for every family.
- A webhook's bearer is never logged and never published. The status document
  carries `token_path` alone.

## The loop

| Interval | Value | Why |
|---|---|---|
| poll | 2 s | a content hash of the registry |
| heartbeat | 20 s | a document older than 90 s reads `unknown` |
| spend | 60 s | spend moves with turns, not ticks |
| backoff | 5 s, doubling to 300 s | a pass that raised, or a delete that did not end |

`SIGHUP` means "look now". The wait ends, and each family takes a pass at
the next look. A family in a backoff keeps its wait. `SIGTERM` stops
the loop after the look in flight.

Each step of a look ends in a handler of the loop: a dispatch, a pass, a
delete. The handler writes the first error with its
traceback. It counts each repeat. The loop continues, and each other
family gets its pass.

A delete that fails gets the backoff of a pass that raised. The loop starts
the delete again when the backoff ends or the registry changes.

## `policy_file.py`

This module writes `policy.toml`, the policy file of the Rust chaperone.
`steps.land_policy` calls `build_app`, and `policy_fleet.py` calls
`render_app`.

1. Each type mirrors one Rust type of `crates/core/src/policy.rs` in the
   chaperone repository. Add no field that the Rust type does not have.
2. A type refuses a value that the Rust reader refuses for that field. It
   raises `PolicyShapeError`. The rules across apps stay with
   `chaperone check`.
3. `render_policy` and `render_app` write each map in key order. A tool
   name with a `.` gets a quoted key.
4. `build_app` maps one family file to one app. The app ID is the family
   name. `all` expands against the current server files.
5. The verbs are tools of plugin upstreams: `delegates` gives
   `attendance.invoke_agent`, `enqueue` and `job_status` give the tools of
   the same names, and `release` gives `release.request`. `embed` and
   `ha_call` give no grant.
6. `build_app` puts the argument fences of the server file on each tool
   that they cover. An allow keeps the values common to each allow entry,
   less the denied values. An allow with no value left is refused.
7. A pair `owner/repo` becomes two arguments. An allow gives `owner` and
   `repo`, and is refused when its pairs are not each owner with each
   repo. A deny gives `repo` alone: it also denies a repo of that name
   under another owner.
8. `build_app` refuses a tool whose exposed name `<server>__<tool>` is
   longer than `EXPOSED_NAME_MAX` (50). The Rust chaperone lists no such
   tool.
9. `tests/policy_golden/` holds the expected text for each family of the
   fixture registry of `family`. Change a golden file only with the change
   that moves it.

## `policy_fleet.py`

This module writes the one `policy.toml` of the fleet. Each verb that
moves a grant or a token calls it: a pass, `apply-once`, `rotate`, the
settle of the loop and `delete`.

1. Each family has one fragment, `grants/<family>.toml` in the state root:
   the text of `render_app`. `write_fragment` writes it. `remove_fragment`
   removes it.
2. `assemble_policy` holds an exclusive `flock` on `grants/policy.lock`.
   It joins the fragments in family order after one `version = 1`. The
   text is the text of `render_policy` for the same apps.
3. A fragment that does not read, that is over the cap or that holds a
   table other than its own `[apps.<family>]` is left out.
4. A `PolicyChecker` checks the joined text. `ChaperoneChecker` runs
   `chaperone check` in a new temp directory that holds a copy of
   `install.toml`. `FakeChecker` is for tests.
5. When the check refuses the joined text, the assembly checks a file
   with no app. A refusal there is not the fault of a family: the
   assembly writes nothing and leaves no family out.
6. Otherwise each fragment is checked alone. A fragment that fails alone
   is left out, with the first error line of the check for its family. The
   rest is checked again. When that check refuses the text and names apps
   (`the app <id> is refused: ...`, from `chaperone check` 0.4.0), those
   apps are left out with their lines, and the rest is checked again. A
   check that names no app writes nothing, as before.
7. Never rename an unchecked file into place. A check that does not run
   raises `CheckerError`, and the assembly writes nothing.
8. The cap of `policy.toml` is `POLICY_FILE_MAX_BYTES`, 1 MiB, equal to
   `POLICY_FILE_CAP` of the Rust chaperone. A larger text is refused
   before the check.
9. The checked text replaces `policy.toml` with `atomic_write`. The old
   file becomes `policy.toml.prev` first. Text equal to the current file
   writes nothing.
10. In the configuration directory of chaperone, this module writes the
    two names of `TargetFile` and nothing else. `install.toml` belongs to
    root.
11. `land_app` writes the fragment when its table moved, the note aside.
    It assembles when the fragment moved or when `policy.toml` does not
    hold the fragment. A pass that moves nothing runs no check.
12. Before it writes a moved fragment, `land_app` checks the new table
    alone. A refusal, or a check that does not run, writes nothing. The
    last good fragment stays, and `policy.toml` keeps the last good table
    of the family. A family that never passed has no table.
13. `land_app` and `land_digests` answer `Held` or `LeftOut`, from what
    `policy.toml` holds after the assembly. `LeftOut` carries one line:
    the error of the family, or the error of the assembly.
14. `land_digests` moves the `token_sha256` line of a fragment and no
    other key. `rotate_serving` and `settle` call it, because they take no
    family file.
15. `withdraw_app` removes the fragment, then assembles. It answers why
    `policy.toml` still holds the table, or `None`.

### The wire

1. Each pass writes the table of `policy.toml`. caregiver writes no
   other policy file.
2. A pass checks the shape of the table before the key step, with
   `steps.check_policy`. A `PolicyShapeError` is one error of the
   validation report, and the pass runs no step.
3. A family that `land_policy` leaves out reads `invalid`. Its first error
   is the one line of `LeftOut`. The pass stops after the policy step, and
   the applied snapshot does not move. The last good table of the family
   stays in `policy.toml`. Each later pass checks again, so the new table
   lands once the check passes.
4. A rotation writes the digests into the fragment: two during the
   overlap, one after. A rotation or a settle that `policy.toml` does not take is
   one error line. It does not undo `creds.json`.
5. `apply-once` refuses as a pass does, and publishes `invalid`.
6. `--chaperone-config-dir` names the directory, `/etc/chaperone` by
   default. A run with a `--state-root` of its own writes
   `<state-root>/chaperone` instead. `--chaperone-bin` names the binary of
   the check, `/opt/components/chaperone/bin/chaperone` by default.
   `creche-caregiver.service` gives both.

## MCP servers

`caregiver` files no `mcp-servers` request and opens no secret gap. A
release request for `mcp-servers` comes from the follower of `handover` or
from the operator (`handover request`). The operator adds a server to the
chaperone by hand (`handover/AGENTS.md`, "An `mcp-servers` release").

## Use

```bash
caregiver serve <registry> --write
caregiver reconcile-once <registry> chat --write
caregiver rotate <registry> chat --reason suspicion --write
caregiver status chat
caregiver delete chat --write
```

Every mutating verb prints its plan. `--write` turns the plan into action.
Exit codes: 0 ok, 1 degraded or refused, 2 a usage mistake, 78 the fences
of the host do not read, or `serve` or `delete` acts with no
`CAREGIVER_VMM_SOCKET`.

## Tests

```bash
uv run pytest caregiver/tests
```

`caregiver_helpers.py` is not a `conftest.py`. It holds `write_registry`.
Nothing here touches a real vmm or LiteLLM.

## Known gaps

- A graceful rotation gives the key no overlap (`rotate.py`).
- An edit that moves `name` or `kind` reads `invalid`, and the last good
  definition keeps serving (`reconcile.py`).
- `degraded` wins over `reconciling` while a fault is open (`reconcile.py`).
- A cron line with no `OnCalendar` spelling gets no timer and no fault
  (`timers.py`).
- `CONTRACT-QUESTION` in `timers.py`, `_atom`. Contract 01 §3.13 gives no
  grammar for a cron field. The timer step reads `1-` as `1` and `*/` as
  `*`. It does not check the size of a step.
- `apply-once` publishes no chaperone fault (`apply.py`).
- `settle` and `rotate_serving` write token digests while a family file is
  invalid. Contract 05 §3.1 and §6.3 do not agree on this (`loop.py`).
- A pass that raises publishes no fault. Contract 05 §3.3 has no code for
  it. The log holds the error, and the status document keeps its last
  content (`loop.py`).
- `read_creds` converts a field with `int` and `str`. It reads `true` as
  epoch 1. Contract 03 §12 gives no rule for a field of another type
  (`credentials.py`).
- A pass lands the digests that `creds.json` gave at the start of the
  pass. A rotation of another process can write new digests between that
  read and the landing. The pass then writes the old digests, and the new
  token is refused until the next pass (`reconcile.py`).
- `CONTRACT-QUESTION` in `steps.py`, `WHOLE_FILE`. Contract 01 §7 gives
  `loc` as a field path. No one field owns a refusal of the app table, so
  that error has the place `<document>`.
- `CONTRACT-QUESTION` in `reconcile.py`, `_text`. Contract 01 §6.1 has no
  rule for an instructions file or a skill file that is not UTF-8. A pass
  reads it as an empty file and writes an empty file into the config
  mount. The family validator does not refuse such a file, so no report
  names it.
- `CONTRACT-QUESTION` in `loop.py`, `_forget_deleted`. Contract 01 §5.6
  rule 3 says that `caregiver` ignores a directory with no `family.toml`.
  It has no rule for a family with state whose directory loses the file.
  The loop keeps the state of that family, and its status document goes
  stale. The loop writes one warning that names the family. A stale
  document fails the `heartbeat` check of `caregiver-verify`, so a release
  of `caregiver` fails while the directory stays as it is. The other
  reading deletes its key and drops its sessions.
- Two fragments that each pass the check alone and fail together, such as
  two apps with one token digest, are left out together when the check
  names them. Their last good tables are not kept, as the chaperone
  itself refuses both apps of such a file. A chaperone before 0.4.0 names
  no app, and then no write of `policy.toml` happens (`policy_fleet.py`).
- Each pass and the `rotate` verb publish `rotation_state: settled`, also
  during the overlap of a token. Contract 05 §6.3 gives `rotating` until
  the overlap ends (`steps.py`).
- A pass takes a `creds.json` that is present and does not read as an
  absent file. It mints a new key and a new token, writes epoch 1 and
  writes one error line. Contract 03 §12 rule 3 says that the epoch
  increases on every write (`steps.py`).
- `CONTRACT-QUESTION` in `vmm.py`, `VmmError`. Contract 05 §3.3.1 says
  that caregiver raises `vmm_protocol_mismatch` for its own vmm call. The
  one call is the drop of a deleted family, whose status document goes
  with the family. The reading here: the code is the `code` of the
  `VmmError`, the log line of the delete names it, and no status document
  carries it. To publish it, contract 05 must name a document for a fault
  of a family that is being deleted.
- `CONTRACT-QUESTION` in `vmm.py`, `VMM_SOCKET_ENV`. Decision CH20 fixes
  the path `/run/user/<uid>/vmm/vmm.sock` and names no env key. The env
  catalog cannot spell a uid, so `default.env` holds no key for it. The
  reading here: the unit sets `CAREGIVER_VMM_SOCKET` from `%t`, outside the
  catalog. To make it a catalog key, the catalog grammar needs a form for
  the runtime dir of an account.
