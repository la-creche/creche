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
| `paths.py`, `clock.py`, `atomic.py` | every host path, the one timestamp, the atomic write |
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
- Credentials die before processes. `delete.py` removes the LiteLLM key, then
  the grant file, then `creds.json`, then the sandboxes. `test_delete.py`
  checks the order from inside the fake driver.
- No secret on argv, in a URL or in a log line. The master key comes from
  `LITELLM_MASTER_KEY` in the environment.
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
- A slow step still publishes. A pass writes `reconciling` before
  `sbx create`. `loop._keep_fresh` restamps a document nothing is about to
  publish for.
- A stop is checked between steps, never inside one.
- A `planned` sandbox row is a create a kill cut short. `fail_planned`
  retires it at the top of every pass.
- An empty registry deletes no family.
- `rotate` deletes the old key before it mints the new one. The token
  overlaps. The key does not.
- `rotate` and `settle` take a `ValidFamily`. Only `rotate.valid_family`
  makes one, and only from a family whose report has no error.
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

`SIGHUP` means "look now". `SIGTERM` stops the loop after the look in flight.

## `mcp_release.py`

1. It installs nothing and reads no secret value.
2. A secret gap goes into a file root drains, never to the phone.
3. One request per gap, not one per tick.
4. The pass never raises. A host with no spool is silent.
5. A secret two servers name opens a gap for neither, unless both files
   declare the sharing under `shared_secrets`.
6. What is served is root's roster, not the install trees.

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
- `apply-once` publishes no chaperone fault (`apply.py`).
- An invalid family file holds a rotation overlap open. The chaperone
  accepts the previous token until the file is valid (`loop.py`).
