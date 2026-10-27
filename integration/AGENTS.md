# integration

The cross-package suites. The doors, `attendance`, the chaperone, `caregiver`
and the playpen were each built against their own fakes. These suites hold
them together. The root `AGENTS.md` applies here too.

It owns no product code. A change here can change what is observed, never
what happens. It is not in the root `testpaths`, so the full suite does not
run it.

## Run it

```bash
cd playpen && AGENT_LAN_ADDRESS=192.0.2.10 pnpm run build && cd ..   # once, and after any playpen change
uv run pytest integration/tests_manager -m slow
```

The suite skips itself when `playpen/dist/playpen.js` is missing. A silent
pass would be worse than a skip. Every test is marked `slow` in
`conftest.py`. The suite needs `node` on `PATH`.

`tests/` held a second suite, which ran the doors, `attendance` and the
playpen in one process over the session API. That API left `attendance` in
the phase 2 plan, row 21, and the suite left with it. `tests/fake_sbx.py`
stays: the process-level suite and the tests of `attendance` start it in
place of `sbx`.

## CI

The `suites` job runs the suite for each code change. Two workflow files
have the job: `.github/workflows/gate.yml` and
`.github/workflows/release.yml`. No hook runs the suite. Run the command
before you push a change to this directory.

1. The job builds the playpen, as the `proc` job does.
2. It runs the suite in a pytest run of its own, with the command above.
3. Its last step reads the JUnit report of the run.

- A test that skips is a failure there. A suite that ran no test is a
  failure too.
- The variable `PYTEST_ADDOPTS` gives the run the path of its report. The
  command stays as it is.
- The last step prints one line for the suite: the tests that ran, the
  total, the tests that skipped and the seconds.
- The job has a time limit of 20 minutes.
- `gate.yml` runs the job for a pull request and for a merge group.
  `release.yml` runs the job again after the merge, before it mints a tag.
- When the job fails in `release.yml`, that run mints no tag. The next green
  run of `release.yml` mints a tag for each component that changed after its
  newest tag.
- `bin/tests/test_gate_workflow.py` pins the job, the command and the last
  step. It also holds the job of `release.yml` equal to the job of
  `gate.yml`.

## What runs

`tests_manager/` runs the real `caregiver` writers against the real
readers: the playpen mounts and the status document that `attendance`
reads. The Node drivers under `node/` start the real
playpen readers.

## The process-level suite

`proc/` holds a second suite. It starts each service as a process and talks
to it only through sockets, files and child programs. It can judge a service
in another language, and the suite above cannot. Read `proc/AGENTS.md`
before you edit it.

```bash
uv run pytest integration/proc -m slow
```

Run each suite in its own pytest command.

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
13. Retired with the in-process stack (phase 2 plan, row 21). The
    number stays, so that other documents keep their references.
14. Never put a playpen mount path in this process's environment. The three
    directories arrive only through `--env-file`.
15. The env file is written by `caregiver`'s own writer.
16. A multi-family scenario must not set `SESSIOND_SANDBOX_SESSIONS_MOUNT`.
17. Write a webhook's token file before the listener starts.
18. Retired with the in-process stack (phase 2 plan, row 21). The
    number stays, so that other documents keep their references.
19. Retired with the in-process stack (phase 2 plan, row 21). The
    number stays, so that other documents keep their references.
20. Retired with the in-process stack (phase 2 plan, row 21). The
    number stays, so that other documents keep their references.
21. Retired with the in-process stack (phase 2 plan, row 21). The
    number stays, so that other documents keep their references.

## Layout

| File | Holds |
|---|---|
| `tests/fake_sbx.py` | `sbx exec --env-file` for a machine with no sbx |
| `fixtures/*-registry/` | the families of the old stages. Read-only inputs. `vectors/` and `handover/` read them. |
| `tests_manager/` | the `caregiver` writers against the real readers, with the Node drivers under `node/` |
| `proc/` | the process-level suite: each service is a process. `proc/AGENTS.md` has its rules. |
