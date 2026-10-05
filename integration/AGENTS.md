# integration

The cross-package suite. The doors, `attendance`, the chaperone, `caregiver`
and the playpen were each built against their own fakes. This is the first
process that holds them together. The root `AGENTS.md` applies here too.

It owns no product code. A change here can change what is observed, never
what happens. It is not in the root `testpaths`, so the full suite does not
run it.

## Run it

```bash
cd playpen && AGENT_LAN_ADDRESS=192.0.2.10 pnpm run build && cd ..   # once, and after any playpen change
uv run pytest integration/tests -m slow
uv run pytest integration/tests_manager -m slow
```

Both suites skip themselves when `playpen/dist/playpen.js` is missing. A
silent pass would be worse than a skip. Every test is marked `slow` in
`conftest.py`. The suite needs `node` on `PATH`.

## CI

The `suites` job of `.github/workflows/gate.yml` runs both suites for each
code change. No hook runs them. Run the two commands before you push a
change to this directory.

1. The job builds the playpen, as the `proc` job does.
2. It runs each suite in a pytest run of its own, with the two commands
   above.
3. Its last step reads the JUnit report of each run.

- A test that skips is a failure there. A suite that ran no test is a
  failure too.
- The variable `PYTEST_ADDOPTS` gives each run the path of its report. The
  command stays as it is.
- The last step prints one line for each suite: the tests that ran, the
  total, the tests that skipped and the seconds.
- The job has a time limit of 20 minutes.
- `.github/workflows/release.yml` does not run the job yet. That file gets
  the job after the job passed 20 runs of the merge queue in a row.
- `bin/tests/test_gate_workflow.py` pins the job, both commands and the last
  step.

## What runs

```
httpx client (plays Open WebUI)
  | HTTP over a Unix socket
  v
door-owui  (real app, real listener)
  | HTTP over a Unix socket
  v
attendance (real app, real listener, real SessionService)
  | stdio, the channel protocol
  v
tests/fake_sbx.py exec --env-file <env file> ...
  | execs with the env file's variables and no others
  v
node playpen/dist/playpen.js   (the real bundle)
  | stdio, pi rpc
  v
playpen/test/fake-pi.mjs   (the playpen's own double)
```

Each later stage adds one real piece:

| Stage | Adds |
|---|---|
| `stage2.py` | the real `caregiver` reconcile pass |
| `stage3.py` | the real chaperone in front of the stack |
| `stage4.py` | the terminal door beside the Open WebUI door |
| `stage5.py` | the trigger door and the approval gate |
| `tests_manager/` | the real `caregiver` writers against the real readers |

## The process-level suite

`proc/` holds a third suite. It starts each service as a process and talks
to it only through sockets, files and child programs. It can judge a service
in another language, and the two suites above cannot. Read `proc/AGENTS.md`
before you edit it.

```bash
uv run pytest integration/proc -m slow
```

Run each suite in its own pytest command. One command for `proc/` and one of
the suites above fails at collection, because the tests in `tests/` import
`conftest` by name.

## Rules

1. Nothing under test may be faked. Four stand-ins exist, none under test:
   `fake-pi.mjs`, `fake_sbx.py`, `FakeDriver` and `FakeLiteLLMKeys`.
2. `fake-pi.mjs` belongs to `playpen/`. Reach it through `AGENT_PI_BIN`. Do
   not copy it here. Do not grow it.
3. When a scenario fails, decide which side departs from a contract, then fix
   that side, in its own commit, with its own unit test.
4. Never weaken a check to make a test pass.
5. Record every mismatch in the pull request, including the ones you fixed.
6. One scenario, one behaviour, one name. The thirteen stage 1 scenarios keep
   their numbering. A later scenario goes in its own `test_<packet>_*.py`.
7. Assert on what crossed a boundary: the SSE body, the journal, the pi spawn
   log, the control mount, the HTTP status.
8. Every temp path stays inside the fixture. Nothing needs `/run`, `/srv`,
   `sbx` or a network service.
9. Keep the socket paths short. macOS refuses a Unix socket path over 104
   bytes.
10. A second sandbox gets its own control directory and env file.
    `add_sandbox` never empties a directory that exists.
11. From stage 2 on, a scenario drives a family file, never a hand-written
    status document.
12. Never clear `supervisor.lock` by hand. The host proves the old playpen is
    gone by watching the beat counter.
13. A scenario that acts mid-turn slows the fake pi first, through
    `stack.set_pi_env`.
14. Never put a playpen mount path in this process's environment. The three
    directories arrive only through `--env-file`.
15. The env file is written by `caregiver`'s own writer.
16. A multi-family scenario must not set `SESSIOND_SANDBOX_SESSIONS_MOUNT`.
17. Write a webhook's token file before the listener starts.
18. A gate's limit must outlast `attendance`'s gate poll, twice over.
19. A gated call carries the playpen's own turn id, read from the turn file.
20. Fill `max_queued_turns` for real: 101 firings, not a patched constant.

## Reading a failure

Work down this list. The first line that does not hold names the hop.

1. Every test fails, or the suite skips: the bundle is missing or stale.
2. `channel_lost`: the playpen died before or during the handshake. Read its
   stderr at `<tmp_path>/log/playpen-chat.log`.
3. `sandbox_unavailable`: `attendance` found no ready sandbox in the status
   document.
4. The stream ends with an error chunk: read the journal.
   `Stack.journal_lines(session)` returns the lines in order.
5. The answer is empty but the turn settled: the playpen and the door disagree
   about an event shape.
6. The wrong number of pi processes: `Stack.pi_starts()` is one line per real
   start.
7. `sandbox_lost` and a `fatal` line: the playpen could not take its lock. A
   mount problem.
8. A turn that waits about `lock_stale_s`: a previous playpen left its lock.
9. A teardown error: a listener would not stop. This suite's own bug.

## Layout

| File | Holds |
|---|---|
| `tests/stack.py` | the wiring: temp mounts, the family fixture, both listeners, the pi shim |
| `tests/fake_sbx.py` | `sbx exec --env-file` for a machine with no sbx |
| `tests/conftest.py`, `tests/sse_read.py` | the `stack` fixture, the `slow` mark, reading SSE back |
| `tests/stage2.py` to `stage5.py`, `stage_eq.py`, `wb2.py` | the wiring of each later stage |
| `tests/test_stage1_gate.py` | the thirteen scenarios |
| `tests/test_i2_stage2.py` to `test_i5_stage5.py` | each stage's scenarios |
| `tests/test_*.py`, other | one packet each: the switch, the launcher, the terminal door, delegations, enqueue, grant refresh, the noticeboard's reads |
| `fixtures/*-registry/` | the families each stage publishes. Read-only inputs. |
| `tests_manager/` | the `caregiver` writers against the real readers, with the Node drivers under `node/` |
| `proc/` | the process-level suite: each service is a process. `proc/AGENTS.md` has its rules. |
