# integration/proc

The process-level suite. It starts each service as an operating system
process, from the command that the service's systemd unit runs. A test talks
to a service only through sockets, files and child programs. The root
`AGENTS.md` and `integration/AGENTS.md` apply here too.

Rules 11 and 15 of `integration/AGENTS.md` apply only to a topology with a
`caregiver` process. A topology with none writes the status document and the
env file from the contracts.

The suite is the judge of a port. A service in another language passes or
fails the same tests, and no test changes. The suites in `integration/tests`
and `integration/tests_manager` host every service in the test process. They
cannot judge a service that Python cannot import.

The suite owns no product code. It is not in the root `testpaths`, so the
full suite does not run it. CI runs it in the `proc` job of `gate.yml` and of
`release.yml`.

## Run it

```bash
cd playpen && pnpm install && AGENT_LAN_ADDRESS=192.0.2.10 pnpm run build && cd ..   # once, and after any playpen change
uv run pytest integration/proc -m slow
```

Run this suite in its own pytest command. One command for this suite and an
old suite fails at collection, because the old tests import `conftest` by
name.

Add `-n 4` to run the tests on four workers. Each run prints the command of
each service before its result line. Each line ends with the origin of the
command: `default` or `override`.

A test that needs `playpen/dist/playpen.js` skips itself when the file is
missing. A silent pass would be worse than a skip. The same applies to
`playpen/dist/agent-pi-launch.js`, which the same build writes. The suite
needs `node` and `git` on `PATH`. Every test is marked `slow` in
`conftest.py`.

Set `CRECHE_PROC_KEEP=1` to keep the root of each test on disk after the run.

Set `CRECHE_PROC_NO_SKIP=1` to make each skip a failure. A run in which
every test skips is green, and it judged nothing. The `proc` job of CI sets
the variable.

## Words

| Word | Meaning |
|---|---|
| service | A program of this repository that the suite starts: one row of the service table. |
| root | The temporary directory of one test. Every file and every Unix socket of the test is in it. |
| stand-in | A program that takes the place of a program the suite cannot run: `sbx`, `pi`, `systemctl` and the LiteLLM key API. |
| topology | The services that one fixture starts together. |
| terminal | A pseudo-terminal. A test holds the master side. A program holds the other side as its controlling terminal. |

## The service table

`proc_services.py` holds one table. Each row gives one service its default
command and the name of one environment variable.

| Service | Default command | Variable |
|---|---|---|
| `attendance` | `python -m attendance` | `CRECHE_PROC_ATTENDANCE` |
| `door-owui` | `python -m agent_door_owui` | `CRECHE_PROC_DOOR_OWUI` |
| `door-trigger` | `agent-trigger` | `CRECHE_PROC_DOOR_TRIGGER` |
| `door-tui` | `agent-tui` | `CRECHE_PROC_DOOR_TUI` |
| `caregiver` | `caregiver` | `CRECHE_PROC_CAREGIVER` |
| `chaperone` | `chaperone` | `CRECHE_PROC_CHAPERONE` |
| `noticeboard` | `noticeboard` | `CRECHE_PROC_NOTICEBOARD` |

The default command is the program and the first words of the unit's
`ExecStart`. The program comes from the `bin` directory of the workspace
venv, which is in the place of the component tree. `test_proc_table.py`
compares each row with the unit file.

### Replace a service with another binary

1. Build the binary.
2. Set the variable of the service to the command that starts the binary.
3. Run the suite. Change no test.

```bash
CRECHE_PROC_ATTENDANCE=/path/to/the/binary uv run pytest integration/proc -m slow
```

The value replaces every word of the default command. The suite splits the
value as a shell does, so the value can hold arguments. The test adds the
arguments that the unit adds, for example `--check`. The suite makes a
relative path absolute, because a service starts in the root of its test.

The binary must read the same environment variables as the service it
replaces, and it must accept the same arguments. The suite gives it nothing
else.

A variable that is set and empty is an error. A value with an open quote is
an error. A variable that names no program is an error. The suite never
returns to the default command. A run that judged the default would look
like a run that judged the binary.

Every variable of the suite starts with `CRECHE_PROC_`. A variable with that
start that the suite does not read stops the run before the first test. A
misspelled name would start the default command.

## What runs

Six topologies exist. `attendance`, the `sbx` stand-in and the `pi`
stand-in are in the first two.

```
a test (plays Open WebUI)                a test (plays the bridge in a sandbox)
  | HTTP, loopback port                    | HTTP, loopback port
  v                                        v
door-owui                                chaperone
  | HTTP over a Unix socket                | reads the grant file at each call
  v                                        | HTTP over a Unix socket
attendance  <------------------------------+
  | stdio, the channel protocol
  v
sbx exec --env-file <env file> <sandbox> -- node playpen.js --sandbox <id>
  |          the sbx stand-in, found through PATH
  v
node playpen/dist/playpen.js             the real bundle
  | stdio, pi rpc
  v
the pi stand-in                          found through AGENT_PI_BIN
```

The third topology starts `caregiver`. It has two forms: `caregiver` alone,
and the house. A scenario can add the chaperone to the first form.

```
a test (edits one file of the registry)
  v
caregiver ---- sbx create, policy, rm ---> the sbx stand-in, with state
  |       ---- systemctl --user ---------> the systemctl stand-in
  |       ---- HTTP, loopback port ------> the LiteLLM stand-in
  | writes the status document, the grant file, the credential file,
  | the config mount and the env file of each sandbox
  | POST /internal/switch-sandbox, over the Unix socket
  v
attendance, door-owui and the chaperone      only in the house
```

The table gives the last three topologies. Each one starts `attendance`
with the two stand-ins of the first picture. None starts `caregiver`.

| Topology | A test plays | The service |
|---|---|---|
| The fourth, `proc_trigger.py`: the trigger door and `attendance` | an automation on the LAN, over HTTP on a loopback port, and a systemd timer, which runs `agent-trigger fire` to its end | reads the registry, the status documents, the webhook bearer files and the outcome records. Dials `attendance` as `door-trigger`. |
| The fifth, `proc_board.py`: the noticeboard and `attendance` | the reverse proxy and a browser, over HTTP on a loopback port | reads the status documents, the report, the outcome records and the audit files. Dials `attendance` as `view-ro`. Writes one git commit in the registry of the root. |
| The sixth, `proc_tui.py`: the terminal door, the Open WebUI door and `attendance` | the operator at a keyboard, on a terminal | dials `attendance` as `door-tui`. Reads the status document. Runs `sbx exec -it` with the real launcher bundle, which starts the pi stand-in on the terminal. |

| File | Topology | What the scenarios check |
|---|---|---|
| `test_proc_owui_turns.py` | door and `attendance` | the thirteen stage 1 scenarios, with the numbers of the old suite |
| `test_proc_owui_start.py` | door and `attendance` | a start, a refused start, the socket mode, a token reload |
| `test_proc_switch.py` | door and `attendance` | the switch call of contract 05 §5, with a test in the place of `caregiver` |
| `test_proc_status.py` | door and `attendance` | what the readers do with the status document and the config mount |
| `test_proc_delegate.py` | chaperone and `attendance` | the delegate path of contract 04 §7, the manifest and the audit |
| `test_proc_caregiver_stage2.py` | the house | the stage 2 scenarios, with the names of the old suite |
| `test_proc_caregiver_start.py` | `caregiver` alone, and the house | a start, a refused start, a signal, a kill, and the verbs that run to an end |
| `test_proc_caregiver_files.py` | `caregiver` alone, `caregiver` with the chaperone, and the house | each file that `caregiver` publishes, read as the next program reads it, and the seams of `integration/tests_manager` that assert on a file |
| `test_proc_override.py` | door and `attendance`, and the last three topologies | a service starts through its variable: one that serves, one that runs to its end, one on a terminal |
| `test_proc_trigger_fire.py` | trigger door and `attendance` | the timer command: one job and its outcome record, a refused family, the queue, a restart of `attendance`, a refused start |
| `test_proc_trigger_webhooks.py` | trigger door and `attendance` | the listener: a webhook starts a job, the one 404, the payload, the bearer files, a start, a refused start, `SIGHUP`, `SIGTERM` |
| `test_proc_trigger_quiet.py` | trigger door and `attendance` | the quiet check of contract 01 §3.15, through the timer command |
| `test_proc_board_pages.py` | noticeboard and `attendance` | each page of `docs/rework/spec.md` §8.1, a bad route parameter, the access key |
| `test_proc_board_edit.py` | noticeboard | the edit form: the CSRF token, the preview, the one commit, a refused save |
| `test_proc_board_start.py` | noticeboard | a start, a refused start, `SIGTERM` |
| `test_proc_tui_terminal.py` | terminal door, door and `attendance` | attach, the command of contract 03 §7.6, the lease, a refused takeover, the release at exit and at a signal, a terminal exchange |
| `test_proc_tui_start.py` | terminal door, door and `attendance` | `--check`, and each refusal before pi has the terminal |
| `test_proc_harness.py`, `test_proc_table.py` | none | the harness and the table, checked against their own rules |
| `test_proc_standins.py`, `test_proc_sse.py` | none | the record of a stand-in, and the SSE reader |
| `test_proc_standin_programs.py` | none | each rule of a stand-in program that a scenario relies on |
| `test_proc_terminal.py` | none | the pseudo-terminal of the harness: the keys, the signals, what it showed |
| `test_proc_html.py`, `test_proc_ids.py` | none | the HTML reader, and the ids a test mints |
| `test_proc_registry.py` | none | the `git` of the registry module: no repository above the root, and no config file of a person |

## Rules

1. A test names a `Service`. It never names a program, a module or a
   language.
2. A test imports no package of a service. It does not import `attendance`,
   `caregiver`, `chaperone`, a door or `agent_family`.
3. A test reads only what crossed a process boundary:
   - an HTTP status, an HTTP body or the SSE stream
   - an exit code
   - a file under the root: a journal, the audit file, a file in a mount
   - the record of a stand-in
   - the git repository of the registry, through `git`
   - what a program wrote on its terminal
   - what a command wrote on its stdout or its stderr
4. Nothing under test may be faked. The four stand-ins are not under test.
5. A stand-in is a program on disk. Do not give a service a Python object.
6. Every file that a service reads is in the root. A writer in `proc_tree.py`
   or in `proc_registry.py` makes it from a contract, never from a module of
   a service.
7. A service gets its whole environment from the test. No variable of the
   shell that runs the suite reaches a service, except `PATH`, `LANG` and
   `TMPDIR`.
8. No variable of a stand-in is in the environment of a service. The `sbx`
   wrapper sets what the sandbox image supplies.
9. A socket is a Unix socket in the root or a loopback port. Never bind
   `0.0.0.0`. Keep the root path short: macOS refuses a socket path over 104
   bytes. Take a port from `Supervisor.free_port`, which keeps the port for
   the test. The lock files of the ports are in `creche-proc-ports-<uid>` in
   the system temporary directory, the one place outside the root.
10. Each service leads its own process group. The teardown sends `SIGTERM` to
    each group. Then it waits 20 seconds at most for each group to become
    empty. It sends `SIGKILL` only to a group that is not empty after that
    time. A group that ended before the teardown gets no signal. A process
    that ended and that waits for its parent to reap it is not a process of
    the group.
11. No test leaves a process behind. A teardown that had to kill a process
    fails the test. This applies to each process of a group, not only to the
    service. A group that no teardown ended fails the session.
12. A scenario that acts during a turn slows the pi stand-in first, with
    `set_pi_env`. The tuning reaches the next pi process that starts. A
    session keeps its pi process, so set the tuning before the first turn of
    the session.
13. Wait for the journal, not for the stream. The `turn_settled` line comes
    after the client's `[DONE]`.
14. Assert on a contract. When a contract is silent, take the most
    conservative reading and add a `CONTRACT-QUESTION:` comment.
15. One scenario, one behaviour, one name. A scenario that an old suite also
    has keeps the name of the old one.
16. Run the suite five times before you add a test to it. Remove or fix a
    test that fails once.
17. Read an HTML page through `proc_html.py`. Assert on an element: a row, a
    link, a field. Do not compare a whole page with a text.
18. Run `git` only through `proc_registry.py`. It gives `git` a whole
    environment, so no config file of a person reaches a test.
19. Start a program that a person types with `spawn_on_terminal`. End it as
    a person does: with a key, with a signal or with a closed terminal.
    Read the lease and the exit code. Do not assert on a sentence that the
    program shows, unless a contract gives the sentence.
20. A scenario that needs pi on a terminal starts on a session that ran a
    turn and that no terminal held before. The first four Known gaps of the
    terminal door say why.

## The `caregiver` topology

`proc_caregiver.py` holds the topology. The fixtures are `caregiver_alone`
and `house`. `caregiver_prepared` and `house_prepared` write the root and
start no service, for a scenario that changes the registry first.

1. A test changes the registry and nothing else. `caregiver` publishes each
   family from it. Do not write a status document, a grant file, a
   credential file or an env file in this topology. One scenario removes
   the grant file. It stops `caregiver` first.
2. An edit to one registry file is the one action of a scenario. Do not send
   a signal to make `caregiver` look, unless the signal is the scenario.
3. Change the registry with a function of `proc_tree.py`:
   - `write_family_file`, `write_family_prose`, `write_skill` and
     `write_registry_file` replace one file by rename.
   - `publish_family` adds one family: its prose first, then its file.
   - `remove_family` removes the directory of one family in one rename.
4. Wait for a file that `caregiver` writes: the status document or the grant
   file. Use `wait_until` in a fixture and in a test with no request in
   flight. Use `until` of `proc_chat.py` when a request is in flight.
5. Assert on a boundary of `caregiver`:
   - the status document
   - the grant file
   - the credential file
   - the config mount
   - an env file
   - a fault file
   - the record of a stand-in
   - the state of a stand-in
   - an HTTP answer
   - an exit code

   Do not read `sandboxes.json` or the `applied` directory. Each one is the
   private state of `caregiver`.
6. Start `attendance` before `caregiver`. `caregiver` asks for the first
   handshake in its first pass. When no `attendance` answers, the next
   attempt comes 20 seconds later.

The start command is the `ExecStart` of `creche-caregiver.service`.
`test_proc_table.py` holds each flag against the unit. The suite adds three
flags that the unit does not have:

| Flag | Why |
|---|---|
| `--litellm-base-url` | The unit dials LiteLLM on the LAN address of the site file. No test binds that address. |
| `--release-root` | `caregiver` refuses a state root of a test with the release root of the host. |
| `--poll-interval-s` | The default look is one in 2 seconds. The suite looks one time in 0.2 seconds, so an edit lands sooner. |

`caregiver` looks again at a family when the registry changes, or 20
seconds after its last pass. No flag changes the 20 seconds. So a scenario
that needs a second pass with no edit takes 20 seconds or more.

## Add a stand-in program

A stand-in takes the place of a program that a service starts or dials, and
that the suite cannot run. Add one only when a scenario needs it.

1. Decide how the service finds the real program. It finds it by name
   through `PATH`, by a path in a variable, or by an address in a variable.
2. Write the stand-in as a program. Put its source in this directory, or use
   a program that another package owns. This suite uses `fake_sbx.py` and
   `fake-pi.mjs` that way.
3. Add an `install_` function to `proc_standins.py`. It writes a wrapper into
   the `bin` directory of the root. The wrapper records the call, then runs
   `exec` on the stand-in. The recorded pid is then the pid of the stand-in.
4. Add the name of the stand-in to `_WRAPPED`, so the teardown checks that
   each of its processes ended.
5. Read the record back with `calls_of`. Assert on the pid and the arguments.
   Do not read the memory of a process.
6. Give a stand-in that listens a Unix socket in the root or a loopback port
   from `Supervisor.free_port`.

Do not grow `fake-pi.mjs` here. It belongs to `playpen/`.

The `sbx` wrapper does two more things for the terminal door. It drops `-it`
after it recorded the call, because `fake_sbx.py` takes no such word. It
sets `SESSIOND_SANDBOX_SESSIONS_MOUNT` from the family of the sandbox id,
because the launcher finds the session store through it (contract 03 §7.6
rule 4). The launcher itself is the real bundle, not a stand-in.

### The stand-in programs of this directory

Three programs are in this directory. The docstring of each one lists its
verbs, its files and its tunings.

| Program | Takes the place of | How a service finds it |
|---|---|---|
| `standin_sbx.py` | `sbx`, with each verb that `caregiver` runs | by name, through `PATH` |
| `standin_systemctl.py` | `systemctl --user` | by name, through `PATH` |
| `standin_litellm.py` | the key API of LiteLLM | by an address in an argument |

Each program keeps its state in files under `standins/<name>-state` in the
root. A test reads those files with a function of `proc_standins.py`. It
does not ask the program.

A tuning is a file under `tune` in the state directory of a stand-in. It
changes one call: the call waits, the call fails, or the call gives another
answer. Write one with `tune`. Remove it with `untune`. Tune a stand-in
only to make a fault that the real program can have.

A stand-in fails closed. It refuses a verb that it does not know, so a
service that runs a new command fails here and gets no silent success.
`test_proc_standin_programs.py` has one test for each rule of a stand-in
that a scenario relies on. Add a test there when you add a rule.

## Add a topology

1. Add a module that holds a class on `Stack` in `proc_stack.py`.
2. Give the class a function that returns the environment of each new
   service. Use the variables of the unit file and no others.
3. Write each new file of the root in `proc_tree.py`, from its contract.
4. Start a service that listens on a port with `start_on_port`.
5. Add a fixture to `conftest.py`. Make it depend on `supervisor`, which ends
   every process.

A family file has two forms. One function writes each form into the
registry: `write_registry_file` of `proc_tree.py`, which replaces a file by
rename. Use the form of the table for a new topology.

| Form | Functions | Topology |
|---|---|---|
| a mapping | `family_body` and `write_family_file` of `proc_tree.py` | the third. `caregiver` reads the file, and a scenario changes one field of it. |
| a text with a comment line | `family_text` and `write_family` of `proc_registry.py` | the fourth and the fifth. A save of the noticeboard must keep the comment. |

The first, the second and the sixth topology have no registry.

A service that cannot start without a code change stops the work. Report the
file, the constant and the variable that is missing. Do not change product
code for this suite.

## Reading a failure

A failed test carries a section named `processes at call`. It holds the root
path, the stdout and the stderr of each process, each playpen log and each
status document. A program on a terminal has one part there, named
`terminal`. A test that fails in its teardown carries the same output in
the text of the failure. Work down this list.

1. Every topology test skips: the bundle is missing. Build it. With
   `CRECHE_PROC_NO_SKIP=1`, each of these tests fails with the same text.
2. `exited N before it was ready`: the service refused to start. Read its
   stderr in the same message.
3. `was not ready in 30.0 s`: the service runs and does not answer HTTP at
   its address. Read its stderr.
4. The stream ends with an error chunk: read the journal of the session
   under `sessions/` in the root, and the playpen log.
5. `the teardown had to end a process`: a service ignored `SIGTERM`, a
   process of its group outlived it, or a stand-in outlived its service.
   The line `names a process outside this test` has one of two causes. A
   stand-in ended and another program got its pid, or a stand-in started
   its own session. The teardown sends no signal to that pid.
6. `a test left a process behind`: a test started a process outside the
   `supervisor` fixture. This is a defect of the test.

## Known gaps

- **CONTRACT-QUESTION, the name of the `caregiver` token.** Contract 02 §3.1
  names the token `caregiver`. `attendance` reads `tokens/managerd.token`,
  and contract 05 §4.1 names the principal `managerd`. The suite writes
  `managerd.token`, the name on the host. A change costs one name in
  `proc_tree.py`.
- **CONTRACT-QUESTION, the exit code of a refused start.** Contract 02 §3
  rule 7 names no exit code. The suite accepts each code that is not 0. A
  change to one fixed code costs one assertion in `test_proc_owui_start.py`.
- **CONTRACT-QUESTION, a new `config_rev` and the pi process of a session.**
  Contract 03 §6 rule 5 gives three reasons to reap the pi process that a
  session keeps. A new `config_rev` is not one of them. The playpen starts a
  new process at the next turn, and `test_proc_status.py` checks that. A
  change costs one count in that file.
- **CONTRACT-QUESTION, the model list and a family that was never valid.**
  Contract 05 §3.1 and contract 02 §5.1 give only the refusal
  `family_invalid` by `attendance`. No contract says what the door lists.
  The door hides the family, and `test_proc_status.py` checks that. A change
  costs one assertion in that file.
- **CONTRACT-QUESTION, the exit codes of `caregiver`.** No contract names
  one. `caregiver/AGENTS.md` gives three codes: 0, 1 and 2. The suite holds
  those three where `caregiver` selects one. `caregiver` ends with no code
  of its own in two cases: a start with no `LITELLM_MASTER_KEY`, and a
  `delete` that fails. There the suite accepts each code that is not 0. A
  change costs one assertion in each scenario of
  `test_proc_caregiver_start.py`.
- **No flag for the session store in `caregiver`.** `SESSIONS_ROOT` in
  `caregiver/src/caregiver/paths.py` is a constant. The first mount of each
  `sbx create` is `/srv/agents/sessions/<family>`, and `attendance` of a
  test reads another directory. No scenario proves that the two services
  name one session store.
- **No flag for three intervals of the loop.** `HEARTBEAT_S`,
  `SPEND_INTERVAL_S` and `BACKOFF_FIRST_S` in
  `caregiver/src/caregiver/loop.py` are constants: 20, 60 and 5 seconds. A
  scenario that waits for one of them takes that long.
- **No flag for the model cache time.** `MODEL_CACHE_S` in
  `caregiver/src/caregiver/reconcile.py` is 10 seconds. No scenario changes
  the model of a family.
- **No flag for the install root of the MCP servers.** `installed_root` in
  `caregiver/src/caregiver/mcp_release.py` is `/opt/mcp`. A registry that
  declares an MCP server makes `caregiver` read that directory of the test
  machine. No scenario declares one.
- **The stand-ins copy facts that no test of this suite can check.** The
  `sbx` stand-in follows the facts that `caregiver/src/caregiver/driver.py`
  records about the real program. The `systemctl` stand-in refuses a
  `disable` of a unit with no file. That rule follows
  `caregiver/src/caregiver/timers.py`. No run of the real program checked
  it for this suite.
- **CONTRACT-QUESTION, `sbx create` with a name that exists.** Contract 05
  §10 row 7 leaves it open. The `sbx` stand-in fails that create. A change
  costs one check in `standin_sbx.py` and its test.
- **CONTRACT-QUESTION, a second key for one alias.** Contract 05 §6.3 has
  two keys for one alias during a rotation. Contract 05 §10 row 5 says that
  no probe of LiteLLM shows that it takes the second one. The LiteLLM
  stand-in refuses it. A `caregiver` that mints the new key under the same
  alias before it deletes the old key fails the rotation scenario. A change
  costs one check in `standin_litellm.py` and its test.
- **CONTRACT-QUESTION, a failed create and its id.** Contract 05 §4.3, last
  paragraph, and §10 row 7 leave the choice to the reconciler. The suite
  holds the choice of `caregiver/AGENTS.md`: a failed create burns its id. A
  change costs one id in `test_a_failed_create_burns_its_id`.
- **CONTRACT-QUESTION, the value of `config_rev`.** Contract 01 §6.1 gives
  the config mount a revision counter of its own. No contract says which
  value the counter holds. The suite holds that `config_rev` equals
  `applied_rev` in the first document of a family. A change costs one term
  of one assertion in `test_proc_caregiver_files.py`.
- **CONTRACT-QUESTION, a stop inside a step.** No contract says what a stop
  inside `sbx create` leaves. The suite holds what the unit file and
  `caregiver/AGENTS.md` say:
  - the step ends whole
  - the exit code is 0
  - the document stays `reconciling`, and the sandbox stays `creating`
  - the next start keeps that sandbox

  A change costs the assertions after the signal in one scenario of
  `test_proc_caregiver_start.py`.
- **CONTRACT-QUESTION, `blocks_turns` of `sandbox_start_failed`.** The table
  of contract 05 §3.3 fixes `yes` for that code. Contract 05 §5.3 rule 8
  says that the family keeps serving after a refused switch. The suite holds
  `false` for the fault of an incoming sandbox while another sandbox serves.
  `integration/tests` holds the same. A change costs one assertion in
  `test_proc_caregiver_stage2.py`, and the turn after that assertion then
  fails.
- **CONTRACT-QUESTION, a registry with no family.** No contract says what
  it means. The suite holds the rule of `caregiver/AGENTS.md`: an empty
  registry deletes no family. A change costs one scenario in
  `test_proc_caregiver_files.py`.
- **The document between a switch and the end of a destroy.** `caregiver`
  publishes no document between the answer of the switch call and the end
  of the destroy. Contract 05 §3.4 publishes the block before each step, and
  §4.3 step 7 sets `ready` after the handshake. The kill scenario of
  `test_proc_caregiver_stage2.py` holds the ids of the two sandboxes at that
  moment, and it holds no state.
- **One process table for each sandbox of a test.** A sandbox on the host
  is a microVM with a process table of its own. A test has no microVM, so
  each playpen of the machine is in one table. On Linux the playpen counts
  each process of that table whose arguments hold `--mode` and `rpc` as two
  words (contract 03 §3 rule 4). The pi wrapper gives its program one word,
  `--mode=rpc`, so no playpen counts the pi stand-in of another sandbox.
  Without that, each replacement of a sandbox with a resident pi process
  ends `degraded` with the fault `orphan_processes` on Linux, and `in_sync`
  on macOS. The cost is that no scenario can show that fault.
- **No scenario for `apply-once`.** It is a verb of `caregiver` with no
  scenario here.
- **The epoch after `rotate`.** Contract 05 §6.3 step 3 publishes the new
  epoch in the status document. The `rotate` verb writes the credential
  file and the grant file. `caregiver serve` publishes the epoch at its
  next pass, 20 seconds later at most. The scenario of `rotate` does not
  wait for that pass.
- **The two seams of `integration/tests_manager` with a bridge are not
  here.** `test_bridge_to_chaperone.py` and `test_cp_approval_to_chaperone.py`
  need stand-ins that do not exist: the embedding service, Home Assistant
  and the approval hook.
- **No bridge on the path.** In `test_proc_delegate.py` a test sends the
  requests that the bridge sends. The playpen bundle fixes `PEP_URL` at build
  time: the LAN address of the site, port 8300. A pi process under the real
  playpen cannot dial a chaperone on another port.
- **A terminal on a new session meets a pi process of the playpen.**
  `attendance` starts a pi process for a session when a door creates it, and
  does not wait (contract 03 §4.7 rules 8 and 9). The terminal door creates
  the session, takes the lease and asks `attendance` to release that process
  (contract 02 §5.11). The start is not complete then, so `attendance`
  answers `released: false` and sends nothing. The cause is in the product,
  and this suite changes no product code.
- **The result of a terminal on a new session changes from run to run.**
  The launcher and the playpen act in an order that no rule fixes. When the
  launcher looks first, two pi processes hold one session store. When the
  playpen starts its process first, the launcher exits 8.
  `test_ct_a_new_session_exists_before_pi_runs` asserts only what holds in
  each order.
- **The answer to a release comes before the end of the pi process.**
  `attendance` answers `released: true` when it sent `stop_process`
  (contract 02 §5.11). The pi process ends later. A launcher that looks
  immediately can find the process and exit 8. On the host `sbx exec -it`
  takes longer than the end of pi. Here the launcher starts in about
  100 ms.
- **A second terminal directly after a first one can exit 8.** A pi process
  that runs and waits ends before the launcher looks, so a scenario on a
  session that ran a turn is safe. A pi process that just started needs
  more time. The playpen starts one when a `tui` lease ends
  (contract 02 §10.5). So the result of the second terminal changes from run
  to run. `test_force_takes_an_idle_lease_from_another_terminal` stops at
  the lease for that reason.
- **CONTRACT-QUESTION, the exit code of a refusal of the terminal door.** No
  contract names one. Contract 03 §7.6 names the codes of the launcher, and
  the suite asserts that the door passes code 10 through. For a refusal of
  the door itself the suite accepts each code that is not 0. A change costs
  one assertion per scenario in `test_proc_tui_start.py`.
- **CONTRACT-QUESTION, the terminal door at a signal.** Contract 02 §5.10
  gives the release call. No contract says what the door does at a signal.
  The suite holds the rule of `door-tui/AGENTS.md`: a signal releases the
  lease and never ends pi. A change costs three assertions in one scenario
  of `test_proc_tui_terminal.py`.
- **No scenario for a renewal or an expiry of a lease.** The terminal door
  renews every 20 seconds, and the lease of `attendance` lives 60 seconds
  (contract 02 §7.1, §7.4). Neither number has a variable, so each scenario
  would wait that long.
- **The pi stand-in is not the interactive pi.** On a terminal it reads one
  command per line, and it ends at the end of the input. No scenario says
  what the real pi does with a key.
- **A closed terminal differs by system.** Linux sends `SIGHUP` to the
  leader of the session and to the foreground group. macOS sends it to the
  leader alone. The scenario reads the lease and not the end of pi.
- **CONTRACT-QUESTION, the exit code of `agent-trigger fire`.** No contract
  names one. The suite reads 0 as a firing that `attendance` accepted or
  that the quiet check skipped. It reads each other code as a firing that
  started nothing. A change costs one assertion per scenario in
  `test_proc_trigger_fire.py`.
- **CONTRACT-QUESTION, the answers of the webhook listener.**
  `docs/rework/spec.md` §7.3 and §11.5 give the 202 and the one 404. No
  contract gives the body of the 202, or the status of another refusal. The
  suite holds what the old stage 5 suite holds. A change costs one assertion
  per scenario in `test_proc_trigger_webhooks.py`.
- **What the suite holds for the webhook listener.** The 202 names the
  session and the state. A body over the limit gets 413. A body that is not
  JSON gets 400. A refusal of `attendance` keeps the status of
  contract 02 §14.
- **No chaperone beside the trigger door.** Three things have no scenario
  for that reason. The quiet check reads the board and the jobs of a family
  through the chaperone (contract 01 §3.15, wake reasons 2 and 4). A gated
  call of a job waits for a phone (contract 04 §8). The old stage 5 suite
  holds the gate scenarios inside one test process. Here they need a
  stand-in for the approval transport, and the bridge on the path.
- **No scenario for the `enqueue` verb.** A family calls `enqueue` at the
  chaperone (contract 04 §4.1), and the chaperone calls `POST /dispatch` of
  `attendance` (contract 02 §13.4). `integration/tests/test_eq_enqueue.py`
  holds those scenarios in one test process. A scenario here needs a grant
  file with the verb, and a status document with `triggers.enqueue: true`.
  `write_grants` and `write_status` of `proc_tree.py` write neither.
- **No scenario for a copy into Open WebUI.** Contract 02 §10.4 writes a
  session that a terminal made into Open WebUI. The old stage 4 suite holds
  those scenarios. An object in its test process takes the place of Open
  WebUI. Here they need a stand-in program for Open WebUI, and none exists.
- **The floor of the quiet check has no scenario.** `floor_hours` is 1 hour
  at least, and the door has no variable for its clock. A scenario would
  wait one hour.
- **A webhook call while `attendance` is down has no scenario.** No contract
  gives the answer of the listener for that case.
- **CONTRACT-QUESTION, the exit code of a refused start of the noticeboard.**
  `docs/rework/spec.md` §8.3 rule 2 names exit code 2 for a LAN bind with no
  key. No section names a code for a wildcard bind or for a key file that
  cannot be read. The suite accepts each code that is not 0 there. A change
  costs one assertion per scenario in `test_proc_board_start.py`.
- **CONTRACT-QUESTION, the markup of a page of the noticeboard.**
  `docs/rework/spec.md` §8.1 says what each page shows. No contract gives
  the markup. The suite reads the markup of the templates as the interface.
  A change of the markup costs the names in `test_proc_board_pages.py` and
  `test_proc_board_edit.py`.
- **The names that the suite reads in the markup.** The suite finds a table
  by its class, and a cell by the text of its column head. It finds a report
  by the classes `problem`, `problems` and `issues`.
- **CONTRACT-QUESTION, the answer to a save of the noticeboard.**
  `docs/rework/spec.md` §8.2 says what a save writes. No section gives the
  answer to the browser. The suite holds the answer of the noticeboard as it
  is. That answer is a 303 to the page of the family, with the start of the
  commit id in `saved`. A change costs three assertions in
  `test_proc_board_edit.py`.
- **A save of the noticeboard ends at the commit.** No `caregiver` runs
  beside the noticeboard, so no scenario proves that a saved family file
  converges.
- **The state of an ended process.** The harness reads it from `/proc` on
  Linux and from `ps` on macOS. On another system, a process that ended
  counts as a process that runs until its parent reaps it. On Linux, the
  same applies when `/proc` lists a process and does not give the state of
  that process.

## Layout

| File | Holds |
|---|---|
| `proc_services.py` | the service table, and the rule for the override variable |
| `proc_harness.py` | a child in its own process group, the wait for an address, the teardown, the check at session end |
| `proc_terminal.py`, `proc_login.py` | the test side of a pseudo-terminal, and the program that gives a command its controlling terminal |
| `proc_tree.py` | the root, and one writer for each file a service reads |
| `proc_registry.py` | the registry of the root: the text of a family file, the git repository, and what `git` reports |
| `proc_ids.py` | the ids that a door mints |
| `proc_html.py` | the reader of an HTML page: an element, a table, a form |
| `proc_standins.py` | the wrapper of each stand-in, the record each one leaves, and the readers of its state |
| `standin_sbx.py`, `standin_systemctl.py`, `standin_litellm.py` | the three stand-in programs of this directory |
| `proc_stack.py` | `attendance`, its environment, and the start of a service on a free port |
| `proc_owui.py`, `proc_delegate.py`, `proc_caregiver.py`, `proc_trigger.py`, `proc_board.py`, `proc_tui.py` | one topology each |
| `proc_chat.py`, `proc_sse.py` | what Open WebUI sends, and how a test reads the SSE stream back |
| `proc_report.py` | what a failed test carries, and the end of the processes of one test |
| `conftest.py` | the fixtures, the `slow` mark, the report hook, the check of the variables |
