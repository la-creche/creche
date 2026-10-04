# caregiver

The family manager. It reads `family.yaml` through `agent_family` and
converges the host to it. The root `AGENTS.md` applies here too. For each
family it:

- mints the LiteLLM key and the chaperone token,
- writes the grant file and the family config mount,
- creates and replaces the sandbox,
- generates the timers an autonomous family asks for.

It is a reconciler, not a command the operator runs. `caregiver serve`
watches the registry and converges every family to its file. Families
converge side by side, four at a time by default. The other verbs exist for a
host where something has gone wrong.

## Layout

| Module | Owns |
|---|---|
| `paths.py`, `clock.py`, `atomic.py` | every host path, the one timestamp, the atomic write and the JSON read |
| `lan.py`, `images.py`, `released.py` | the LAN address from the site file, the image a flavor maps to, and the images a `playpen` release installed |
| `driver.py` | `SandboxDriver`: `SbxDriver` (real) and `FakeDriver` |
| `litellm_keys.py`, `credentials.py`, `grants.py`, `config_mount.py` | the key, the token, the grant file, the config mount |
| `playpen_env.py` | the env file `sbx exec --env-file` carries |
| `faults.py`, `chaperone_watch.py`, `status.py` | the fault files, the one chaperone probe, the status document |
| `webhook_tokens.py`, `timers.py` | one bearer per webhook, and the generated timer units |
| `switch.py` | the one call this service makes to `attendance` |
| `applied.py`, `steps.py`, `reconcile.py`, `sandboxes.py` | the live snapshot, one function per step, one pass, the one create path |
| `rotate.py`, `loop.py`, `apply.py`, `delete.py` | rotate, serve, the one-shot apply, the teardown |
| `mcp_wire.py`, `mcp_release.py`, `live_manifest.py` | one `mcp-servers` request per pass, and the ledger it reads |
| `cli.py` | `serve`, `reconcile-once`, `rotate`, `apply-once`, `status`, `delete` |
| `verify.py` | `caregiver-verify` |

## Rules

- **Layers talk to the layer below, never sideways.** `apply.py`,
  `reconcile.py`, `rotate.py`, `sandboxes.py`, `loop.py`, `mcp_wire.py` and
  `delete.py` call more than one sibling. Every other module talks only to
  `paths.py`, `clock.py` and `atomic.py`.
- Every external dependency sits behind a Protocol with a fake. Add a third
  the same way.
- `atomic.py` is the only place that writes a file another process reads.
- `atomic.read_json` reads a JSON file. It answers `None` for content that
  it cannot read. It does not raise on content.
- `grants.py`, `mcp_release.py`, `live_manifest.py` and `verify.py` keep a
  read of their own. Each one refuses an integer past the digit limit and
  nesting past the limit of the parser.
- The grant file reader takes each encoding that `json.loads` finds in
  bytes: UTF-8, UTF-16 and UTF-32. The other three refuse bytes that are
  not UTF-8.
- A client maps each failure of its transport and of its answer to its own
  error: `DriverError`, `LiteLLMError`, `SwitchError`. A step names that
  error in its handler, with two exceptions.
- The destroy step of a replacement and `delete_family` have no handler.
  In `serve`, their error ends in a handler of the loop. In each other
  verb it ends in `cli.main`: one line, and exit code 1.
- Credentials die before processes. `delete.py` removes the LiteLLM key, then
  the grant file, then `creds.json`, then the sandboxes. `test_delete.py`
  checks the order from inside the fake driver.
- No secret on argv, in a URL or in a log line. The master key comes from
  `LITELLM_MASTER_KEY` in the environment.
- A verb that acts refuses to start with no master key. It writes one
  line that names the variable, and its exit code is 1.
- The chaperone watch probes once per interval for the whole fleet. A probe
  that raises, hangs or answers 503 is a probe that did not answer. A moved
  verdict forces a pass.
- `chaperone.watch: off` is a value, not a silence. `status` prints it.
- Root's answer to a release request is said, not only logged. `mcp_release`
  holds an identical request while `done/<id>.json` stands, and the hold
  reaches every status document as `mcp_install_held`.
- The marker is written before the request. Every field in it is hostile.
- No status field claims what this service cannot observe. It cannot count
  running turns, so it does not.
- Read the fault files after the grant file lands, not only before.
- The grant file follows what it should hold, not the family file's diff.
- `apply_once` creates a sandbox once. `classify()` decides whether a change
  needs a new one.
- The applied snapshot moves at the end of a successful pass. An interrupted
  pass leaves the old one.
- Narrowing runs before widening, everywhere. Egress removals before allows.
  Timer removals before timer writes.
- A refused switch keeps both sandboxes.
- Every pass converges a timer's enabled state, not only its file.
- `Actors` has no defaults.
- A failed create burns an id and earns a backoff, 5 s doubling to 300 s. A
  registry edit clears the backoff.
- A slow step still publishes. A pass writes `reconciling` and the step
  in flight before each slow step: `sbx create`, the switch call and the
  destroy of a replacement. `loop._keep_fresh` restamps a document nothing
  is about to publish for.
- `restamp_status` rewrites only a document that this process published. A
  document from before a restart is the verdict of another process.
- A family that waits for a slot after a restart gets one look.
  `loop._look_at` runs the pass of that family with the stop already set.
  Each cheap step runs. The pass stops before the first slow step, and it
  publishes the document of this process.
- A pass that stops before a slow step publishes `reconciling`, never
  `in_sync`. The loop records no revision for it, so the family takes the
  next free slot.
- A stop is checked between steps, never inside one.
- A `planned` sandbox row is a create a kill cut short. `fail_planned`
  retires it at the top of every pass.
- A sandbox row that does not read stays in the ledger file. A rewrite
  removes it only when the JSON encoder cannot write it again.
- An empty registry deletes no family.
- A directory under `families/` with no `family.yaml` gets no pass and no
  delete. `reconcile.has_family_file` is the test for it. `apply.apply_once`
  holds a copy of that test, because `apply.py` does not call
  `reconcile.py`.
- `rotate` deletes the old key before it mints the new one. The token
  overlaps. The key does not.
- `rotate` publishes the new epoch. It writes the credentials block of
  the status document and no other field, so `written_at` stays. A write
  of the block that fails is one error line, and the rotation continues.
- `rotate` takes a `ValidFamily`. Only `rotate.valid_family` makes one. The
  report must have no error, and the applied snapshot must not refuse the
  edit.
- A family file that is invalid or refused does not stop a rotation.
  `rotate_serving` takes the key's router and budget from the applied
  snapshot. It writes the token digests into the grant file.
- `settle` writes the token digests alone. It reads no family file.
- An `invalid` status document carries the credentials block. `attendance`
  reads the epoch from that document alone.
- Never build a grant from the applied snapshot. `all` expands against
  the current server files, and no validation approved that result.
- A cron line with no `OnCalendar` spelling gets no unit.
- A fault's `blocks_turns` comes from `faults.BLOCKS_TURNS_BY_CODE`, never
  from the file.
- A flavor with no image creates nothing. The fault does not block turns.
- The images of a `playpen` release win over `--image` and `--image-python`.
  `released.py` reads `/opt/components/playpen/images.env` once per pass.
- A released file that is absent or does not read keeps the last good
  answer. Only a process that never read one uses the flags.
- A released reference is a digest. A file that holds a tag, an unknown
  flavor or no `base` line is refused whole.
- A run with a `--state-root` of its own reads no released file, unless
  `--released-images` names one.
- Never name a fixed in-VM path. `sbx create` mounts a host directory at the
  same path inside the VM. `playpen_env.py` writes the three host paths into
  the env file, per sandbox, beside `control/` and never inside it.
- `config_rev` and `applied_rev` are the same value: the registry revision.
- A webhook's bearer is never logged and never published. The status document
  carries `token_path` alone.

## The loop

| Interval | Value | Why |
|---|---|---|
| poll | 2 s | a content hash of the registry |
| heartbeat | 20 s | a document older than 90 s reads `unknown` |
| spend | 60 s | spend moves with turns, not ticks |
| create backoff | 5 s, doubling to 300 s | a failed create burns an id |

`SIGHUP` means "look now". The wait ends, and each family takes a pass at
the next look. A family in a create backoff keeps its wait. `SIGTERM` stops
the loop after the look in flight.

Each step of a look ends in a handler of the loop: a dispatch, a pass, a
delete, the MCP pass. The handler writes the first error with its
traceback. It counts each repeat. The loop continues, and each other
family gets its pass.

A delete that fails gets the backoff of a failed create. The loop starts
the delete again when the backoff ends or the registry changes.

## `mcp_release.py`

1. It installs nothing and reads no secret value.
2. A secret gap goes into a file root drains, never to the phone.
3. One request per gap, not one per tick.
4. The pass never raises. A host with no spool is silent.
5. A secret two servers name opens a gap for neither, unless both files
   declare the sharing under `shared_secrets`.
6. What is served is root's roster, not the install trees.
7. The roster reader has two limits for merge keys: a chain of 128 keys,
   and 65,536 copied pairs for one file. A merged value with no pair counts
   as one pair. A roster past a limit reads as a roster with no row.

## Use

```bash
caregiver serve <registry> --image sha256:... --write
caregiver reconcile-once <registry> chat --image sha256:... --write
caregiver rotate <registry> chat --reason suspicion --write
caregiver status chat
caregiver delete chat --write
```

Every mutating verb prints its plan. `--write` turns the plan into action.
Exit codes: 0 ok, 1 degraded or refused, 2 a usage mistake.

## Tests

```bash
uv run pytest caregiver/tests
```

`caregiver_helpers.py` is not a `conftest.py`. It holds `write_registry`.
Nothing here touches a real sandbox or LiteLLM.

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
- A look runs `fail_planned`, which destroys the virtual machine of a
  `planned` row. That is one `sbx` call outside the bound (`loop.py`).
- A restart during a fleet replacement still ends each create in flight.
  The next process retires the `planned` row and burns one id
  (`sandboxes.py`).
- A pass publishes the step name `write_timers`. Contract 05 §3.4 lists
  eight step names, and that name is not one of them (`reconcile.py`).
- A pass that raises publishes no fault. Contract 05 §3.3 has no code for
  it. The log holds the error, and the status document keeps its last
  content (`loop.py`).
- `read_creds` converts a field with `int` and `str`. It reads `true` as
  epoch 1. Contract 03 §12 gives no rule for a field of another type
  (`credentials.py`).
- The ledger reader converts a field with `int` and `str`. No contract
  defines the ledger file (`sandboxes.py`).
- No contract gives the encoding of the grant file. The reader takes each
  encoding that `json.loads` finds in bytes (`grants.py`).
- No pass manages a sandbox whose ledger row does not read. The row stays
  in the file, and the log names the family at each rewrite
  (`sandboxes.py`).
- The ledger reader takes a file that is present and gives no list of rows
  as an absent file. The next rewrite replaces the file and writes one error
  line. No pass then destroys a sandbox that the old file named
  (`sandboxes.py`).
- A destroy that fails after a failed create, or for a `planned` row,
  leaves the virtual machine. The row reads `failed`, and the log holds one
  error line. No later pass destroys that virtual machine. Contract 05 §4.2
  rule 5 has no rule for a destroy that fails (`sandboxes.py`).
- `CONTRACT-QUESTION` in `reconcile.py`, `_text`. Contract 01 §6.1 has no
  rule for an instructions file or a skill file that is not UTF-8. A pass
  reads it as an empty file and writes an empty file into the config
  mount. The family validator does not refuse such a file, so no report
  names it.
- `CONTRACT-QUESTION` in `loop.py`, `_forget_deleted`. Contract 01 §5.6
  rule 3 says that `caregiver` ignores a directory with no `family.yaml`.
  It has no rule for a family with state whose directory loses the file.
  The loop keeps the state of that family, and its status document goes
  stale. The loop writes one warning that names the family. A stale
  document fails the `heartbeat` check of `caregiver-verify`, so a release
  of `caregiver` fails while the directory stays as it is. The other
  reading deletes its key and its sandboxes.
- `CONTRACT-QUESTION` in `mcp_release.py`. `stage7-releases.md` §4.4 gives
  no limit for a merge key in the roster. The roster reader uses the two
  limits of the Rust reader of a component manifest.
- The two merge limits bound the memory of a roster read. They do not
  bound its time. `MAX_ROSTER_BYTES` is the one bound on the time. A
  roster at that cap took 2 to 18 seconds in the measured reads, and 221 MB
  at most. The MCP pass runs on the loop thread (`mcp_release.py`).
- A pass takes a `creds.json` that is present and does not read as an
  absent file. It mints a new key and a new token, writes epoch 1 and
  writes one error line. Contract 03 §12 rule 3 says that the epoch
  increases on every write (`steps.py`).
