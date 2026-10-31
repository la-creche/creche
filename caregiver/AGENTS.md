# caregiver

The family manager. It reads `family.toml` through `agent_family` and
converges the host to it. The root `AGENTS.md` applies here too. For each
family it:

- mints the LiteLLM key and the chaperone token,
- writes the table of the family in `policy.toml` and the family config
  mount,
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
| `litellm_keys.py`, `credentials.py`, `config_mount.py` | the key, the token, the config mount |
| `playpen_env.py` | the env file `sbx exec --env-file` carries |
| `policy_file.py` | the types and the text of `policy.toml`, and `build_app` |
| `policy_fleet.py` | one fragment per family, and the checked assembly of the one `policy.toml` |
| `faults.py`, `chaperone_watch.py`, `status.py` | the fault files, the one chaperone probe, the status document |
| `webhook_tokens.py`, `timers.py` | one bearer per webhook, and the generated timer units |
| `switch.py` | the one call this service makes to `attendance` |
| `applied.py`, `steps.py`, `reconcile.py`, `sandboxes.py` | the live snapshot, one function per step, one pass, the one create path |
| `rotate.py`, `loop.py`, `apply.py`, `delete.py` | rotate, serve, the one-shot apply, the teardown |
| `live_manifest.py` | the ledger of root, read |
| `cli.py` | `serve`, `reconcile-once`, `rotate`, `apply-once`, `status`, `delete` |
| `verify.py` | `caregiver-verify` |

## Rules

- **Layers talk to the layer below, never sideways.** `apply.py`,
  `reconcile.py`, `rotate.py`, `sandboxes.py`, `loop.py` and `delete.py`
  call more than one sibling. `policy_fleet.py` calls
  `policy_file.py`. Every other module talks only to `paths.py`,
  `clock.py` and `atomic.py`.
- Every external dependency sits behind a Protocol with a fake. Add a third
  the same way.
- `atomic.py` is the only place that writes a file another process reads.
- `atomic.read_json` reads a JSON file. It answers `None` for content that
  it cannot read. It does not raise on content.
- `live_manifest.py` and `verify.py` keep a read of their own. Each one
  refuses an integer past the digit limit and nesting past the limit of
  the parser. Each one refuses bytes that are not UTF-8.
- A client maps each failure of its transport and of its answer to its own
  error: `DriverError`, `LiteLLMError`, `SwitchError`. A step names that
  error in its handler, with two exceptions.
- The destroy step of a replacement and `delete_family` have no handler.
  In `serve`, their error ends in a handler of the loop. In each other
  verb it ends in `cli.main`: one line, and exit code 1.
- Credentials die before processes. `delete.py` removes the LiteLLM key, then
  the fragment of `policy.toml` and its table, then
  `creds.json`, then the sandboxes. `test_delete.py` checks the order from
  inside the fake driver.
- No secret on argv, in a URL or in a log line. The master key comes from
  `LITELLM_MASTER_KEY` in the environment.
- Each verb that applies the registry (`serve`, `reconcile-once`,
  `rotate`, `apply-once`) reads the fences of the host from its environment:
  `AGENT_MOUNT_ROOTS` and `AGENT_PLATFORM_FAMILIES`
  (`agent_family.fences`). The unit reads them from the site file. With no
  `AGENT_MOUNT_ROOTS`, or an entry of the wrong shape, the verb writes one
  line that names the key, and its exit code is 78. `LoopConfig` and
  `apply_once` take the fences, with no default.
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
- `apply_once` creates a sandbox once. `classify()` decides whether a change
  needs a new one.
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
- `paths.playpen_env_path` is the one source for the path of an env file.
  The ledger reader computes the path of a row from the state root, the
  family and the id of that row.
- The ledger writer keeps the field `supervisor_env` in each row. Another
  release of this package reads the path from that field.
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
- The env file of a sandbox has seven lines: the sessions directory, three
  host paths, the sandbox id and the URL of each plane. The two plane
  variables are `AGENT_PEP_URL` and `AGENT_LITELLM_BASE_URL`.
- The sessions directory is `paths.session_dir`, under the sessions root of
  the host. A family file that mounts `/srv/agents/state/index` read-only
  gets an eighth line, `AGENT_INDEX_ROOT`. `paths.index_root` decides it.
- Each caller of `write_playpen_env` gives the two URLs, from `lan.url`.
  `playpen_env.py` holds no address and no port.
- A sandbox keeps the env file of its create. `caregiver` writes the file
  again in two cases only: a pass finds the replacement that an earlier pass
  left, or the operator runs `apply-once`.
- `config_rev` and `applied_rev` are the same value: the registry revision.
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
| create backoff | 5 s, doubling to 300 s | a failed create burns an id |

`SIGHUP` means "look now". The wait ends, and each family takes a pass at
the next look. A family in a create backoff keeps its wait. `SIGTERM` stops
the loop after the look in flight.

Each step of a look ends in a handler of the loop: a dispatch, a pass, a
delete. The handler writes the first error with its
traceback. It counts each repeat. The loop continues, and each other
family gets its pass.

A delete that fails gets the backoff of a failed create. The loop starts
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
caregiver serve <registry> --image sha256:... --write
caregiver reconcile-once <registry> chat --image sha256:... --write
caregiver rotate <registry> chat --reason suspicion --write
caregiver status chat
caregiver delete chat --write
```

Every mutating verb prints its plan. `--write` turns the plan into action.
Exit codes: 0 ok, 1 degraded or refused, 2 a usage mistake, 78 the fences
of the host do not read.

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
- A pass lands the digests that `creds.json` gave at the start of the
  pass. A rotation of another process can write new digests between that
  read and the landing. The pass then writes the old digests, and the new
  token is refused until the next pass (`reconcile.py`).
- `CONTRACT-QUESTION` in `steps.py`, `WHOLE_FILE`. Contract 01 §7 gives
  `loc` as a field path. No one field owns a refusal of the app table, so
  that error has the place `<document>`.
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
  rule 3 says that `caregiver` ignores a directory with no `family.toml`.
  It has no rule for a family with state whose directory loses the file.
  The loop keeps the state of that family, and its status document goes
  stale. The loop writes one warning that names the family. A stale
  document fails the `heartbeat` check of `caregiver-verify`, so a release
  of `caregiver` fails while the directory stays as it is. The other
  reading deletes its key and its sandboxes.
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
