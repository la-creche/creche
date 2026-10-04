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
missing. A silent pass would be worse than a skip. The suite needs `node` on
`PATH`. Every test is marked `slow` in `conftest.py`.

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

Three topologies exist. `attendance`, the `sbx` stand-in and the `pi`
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
| `test_proc_override.py` | door and `attendance` | each service starts through its variable |
| `test_proc_harness.py`, `test_proc_table.py` | none | the harness and the table, checked against their own rules |
| `test_proc_standins.py`, `test_proc_sse.py` | none | the record of a stand-in, and the SSE reader |
| `test_proc_standin_programs.py` | none | each rule of a stand-in program that a scenario relies on |

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
4. Nothing under test may be faked. The four stand-ins are not under test.
5. A stand-in is a program on disk. Do not give a service a Python object.
6. Every file that a service reads is in the root. A writer in `proc_tree.py`
   makes it from a contract, never from a module of a service.
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

## The `caregiver` topology

`proc_caregiver.py` holds the topology. The fixtures are `caregiver_alone`
and `house`. `caregiver_prepared` and `house_prepared` write the root and
start no service, for a scenario that changes the registry first.

1. A test changes the registry and nothing else. `caregiver` publishes each
   family from it. Do not write a status document, a grant file, a
   credential file or an env file in this topology.
2. An edit to one registry file is the one action of a scenario. Do not send
   a signal to make `caregiver` look, unless the signal is the scenario.
3. Write a registry file with `write_family_file`, `write_family_prose` or
   `write_registry_file`. Each one replaces the file by rename.
4. Wait for a file that `caregiver` writes: the status document or the grant
   file. Use `wait_until` in a fixture and in a test with no request in
   flight. Use `until` of `proc_chat.py` when a request is in flight.
5. Assert on a boundary of `caregiver`: the status document, the grant file,
   the credential file, the config mount, an env file, a fault file, the
   record of a stand-in, the state of a stand-in, an HTTP answer or an exit
   code. Do not read `sandboxes.json` or the `applied` directory. Each one
   is the private state of `caregiver`.
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

A service that cannot start without a code change stops the work. Report the
file, the constant and the variable that is missing. Do not change product
code for this suite.

## Reading a failure

A failed test carries a section named `processes at call`. It holds the root
path, the stdout and the stderr of each process, and each playpen log. A
test that fails in its teardown carries the same output in the text of the
failure. Work down this list.

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
  those three where `caregiver` selects one. It accepts each code that is
  not 0 where `caregiver` ends with no code of its own: a start with no
  `LITELLM_MASTER_KEY`, and a `delete` that fails. A change costs one
  assertion in each scenario of `test_proc_caregiver_start.py`.
- **No flag for the session store in `caregiver`.** `SESSIONS_ROOT` in
  `caregiver/src/caregiver/paths.py` is a constant. The first mount of each
  `sbx create` is `/srv/agents/sessions/<family>`, and `attendance` of a
  test reads another directory. No scenario proves that the two services
  name one session store.
- **No flag for three times of the loop.** `HEARTBEAT_S`, `SPEND_INTERVAL_S`
  and `BACKOFF_FIRST_S` in `caregiver/src/caregiver/loop.py` are constants:
  20, 60 and 5 seconds. A scenario that waits for one of them takes that
  long.
- **No flag for the model cache time.** `MODEL_CACHE_S` in
  `caregiver/src/caregiver/reconcile.py` is 10 seconds. No scenario changes
  the model of a family.
- **No flag for the install root of the MCP servers.** `installed_root` in
  `caregiver/src/caregiver/mcp_release.py` is `/opt/mcp`. A registry that
  declares an MCP server makes `caregiver` read that directory of the test
  machine. No scenario declares one.
- **The stand-ins copy facts that no test of this suite can check.** The
  `sbx` stand-in follows the facts that `caregiver/src/caregiver/driver.py`
  records about the real program.
- **CONTRACT-QUESTION, `sbx create` with a name that exists.** Contract 05
  §10 row 7 leaves it open. The `sbx` stand-in fails that create. A change
  costs one check in `standin_sbx.py` and its test.
- **CONTRACT-QUESTION, a second key for one alias.** Contract 05 §6.3 has
  two keys for one alias during a rotation. Contract 05 §10 row 5 says that
  no probe of LiteLLM shows that it takes the second one. The LiteLLM
  stand-in refuses it. A change costs one check in `standin_litellm.py` and
  its test.
- **The document between a switch and the end of a destroy.** `caregiver`
  publishes no document between the answer of the switch call and the end
  of the destroy. Contract 05 §3.4 publishes the block before each step, and
  §4.3 step 7 sets `ready` after the handshake. The kill scenario of
  `test_proc_caregiver_stage2.py` holds the ids of the two sandboxes at that
  moment, and it holds no state.
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
- **No scenario for `door-trigger`, `door-tui` and `noticeboard`.** Each has
  a row in the service table and no topology.
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
| `proc_tree.py` | the root, and one writer for each file a service reads |
| `proc_standins.py` | the wrapper of each stand-in, the record each one leaves, and the readers of its state |
| `standin_sbx.py`, `standin_systemctl.py`, `standin_litellm.py` | the three stand-in programs of this directory |
| `proc_stack.py` | `attendance`, its environment, and the start of a service on a free port |
| `proc_owui.py`, `proc_delegate.py`, `proc_caregiver.py` | the three topologies |
| `proc_chat.py`, `proc_sse.py` | what Open WebUI sends, and how a test reads the SSE stream back |
| `proc_report.py` | what a failed test carries, and the end of the processes of one test |
| `conftest.py` | the fixtures, the `slow` mark, the report hook, the check of the variables |
