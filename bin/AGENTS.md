# bin

This directory holds the scripts that run on the host and the scripts of CI.
It also holds two shell libraries, one tool for a development machine and
the tests behind them. One house style across every file. The root
`AGENTS.md` applies here too.

Bash only: `#!/usr/bin/env bash`. A script that may run on a development
machine stays bash 3.2-clean: no associative arrays, no `mapfile`, no
`${var,,}`, no `&>>`.

Two scripts are exceptions. Each one is Python, with the first line
`#!/usr/bin/env python3`. The gate checks them with ruff and not with
pyright: `pyrightconfig.json` names no path under `bin/`.

- `yaml-to-toml.py`. Reason: it reads YAML with PyYAML. The workspace venv
  holds PyYAML, so start the script with `uv run`.
- `chaperone-install-draft.py`. Reason: it reads TOML with `tomllib` and
  writes TOML. Bash has no TOML reader. Start the script with `uv run`.

## Every script's header says who runs it

`ROOT`, `OPERATOR` or `CI`, in the first three lines, with the exact command.
A reader must not infer it from the `id -u` guard. A root script asserts
`[[ "$(id -u)" == "0" ]]`. An operator script asserts the opposite.

| Family | Files | Contract |
|---|---|---|
| Setup | `provision-library.sh` | Idempotent, not a no-op. Every step checks before it writes. A value that has a current answer is upserted in place. A token minted once is never replaced in silence. |
| Operations | `creche-deploy`, `creche-handover`, `creche-handover-intake`, `rework-watchdog.sh`, `rework-registry-sync.sh`, `sbx-drift-check.sh`, `sync-code-corpus.sh` | Run unattended from units and timers. Fail loudly into the journal. |
| Checks | `quality-gate.sh`, `systemd-proof.sh` | Run from the hooks and from CI. `systemd-proof.sh` runs in CI only. No unit runs them. They use `set -euo pipefail`. The first failed check stops the run. |
| Library | `lib/envfile.sh`, `lib/docsrule.sh` | Sourced only, never executed. Say "Sourced only" in the header. That marker exempts the file from the mode rule below. |
| Tools | `yaml-to-toml.py`, `chaperone-install-draft.py` | Run by hand on a development machine, and from a test in CI. No unit runs a tool, and the host does not need one. A tool reads the files that its command names and changes no file. `chaperone-install-draft.py --out-dir` makes one new directory and writes only into it. |
| Tests | `tests/test_*.py`, `tests/test_*.sh` | pytest collects the `.py` files. The `.sh` files run by hand: `bash bin/tests/<name>.sh`. None needs a host. |

## Secrets

- Never echo a secret. Decrypt with `sops exec-env` or `sops -d` and pass the
  value through the environment.
- `umask 077` before the write, not `chmod` after. A redirect that creates a
  file under a subshell umask has no readable window. A `chmod` afterward
  always has one. `envfile_upsert` and `envfile_append_once` already do this.
- A secret never goes on `curl`'s argv, including in a header value. Use
  `-H @<tmpfile>` or `--config <tmpfile>`, with the file created under
  `umask 077` and removed right after the call.
- Never hand-roll a `cat >` over an env file. A re-run truncates every other
  key. Use `envfile_upsert`. Read one value with `envfile_value`, never with
  `set -a` and a `source`.
- Read a key of the catalog of the product with `creche_env_get`. It runs
  the command `creche-env` of the checkout. `env/AGENTS.md` has the rules of
  the files.
- `curl -u user:pass`, never credentials in a URL.

## Idempotent setup, gates and operations

A setup script uses `set -euo pipefail`. The first failure stops everything.

A gate uses `set -uo pipefail`, without `-e`. A failed check must count, not
abort the run:

```bash
FAILS=0
pass() { say "PASS: $*"; }
fail() { say "FAIL: $*"; FAILS=$((FAILS + 1)); }
[[ "$FAILS" -eq 0 ]] && say "GATE: PASS" || { say "GATE: FAIL ($FAILS)"; exit 1; }
```

Never mix the two. Under `set -e` the counter idiom aborts at the first
failed command.

A gate never reads `sbx exec` output. The attach races process exit. Verify
through a file or a health endpoint, and give a short in-VM command a
`sleep 8` tail.

## The scripts

| Script | Runs as | What it does |
|---|---|---|
| `creche-deploy` | ROOT, the single sudoers entry | Puts this repository at `/opt/creche` and the site at `/opt/private-docs`, fills the venv, restarts one unit: the chaperone. Then polls `is-active` and `NRestarts` for about 20 s. Origin is the operator's checkout, so root needs no GitHub credential. It installs no MCP server. |
| `creche-handover` | ROOT, from `creche-handover.path` | Decrypts the site's sops file and execs `handover-exec` under it. Installed to `/usr/local/sbin` by hand, never by a release. Refuses a path another account can write. |
| `creche-handover-intake` | ROOT, from `creche-handover-intake.service` | The same, for the secret intake. |
| `rework-watchdog.sh` | OPERATOR, every minute | The outage alarm. Checks the chaperone's `/healthz`, `caregiver`'s freshness, `attendance`'s socket and `is-failed` for the units. Pushes once per change of verdict. `--last` prints the verdict. Shares no fate with what it watches. The unit check also reads each firing unit. After the checks, the script sends one notice for each service, surface and rule that a notice file records. "The watchdog" has the two rules. |
| `rework-registry-sync.sh` | OPERATOR, every minute | `git fetch --prune`, then `git merge --ff-only`, on the registry checkout. Never rebases, resets, cleans or pushes. |
| `sbx-drift-check.sh` | OPERATOR, daily | Read-only. Alarms when the global sbx policy holds any network allow, or when a per-sandbox rule allows a host that is not the LAN address and not in the seeded allowlist. |
| `sync-code-corpus.sh` | OPERATOR, hourly | Refreshes the dedicated code clones. `handover` reads the source tree of a release from them. The repository list lives outside the corpus. |
| `provision-library.sh` | OPERATOR | One corpus: the image, the sandbox, TEI-only egress, the timer. Needs `AGENT_LAN_ADDRESS` from the site file. |
| `quality-gate.sh` | OPERATOR, from the hooks and CI | ruff, ruff format, pyright, then pytest as asked: `--tests`, `--tests-for PATH...` or `--docs`. For a push that changes a file under `integration/proc/` that is not prose, it runs the process-level suite. |
| `systemd-proof.sh` | CI, from the `systemd-proof` job and from the scope of each workflow | Proves the restart rule of the daemon units on the systemd of the runner, with three transient units. Then gives each unit file under `systemd/` to `systemd-analyze verify`. `--unchanged FROM TO` says if a change needs no proof. |
| `chaperone-install-draft.py` | OPERATOR on a development machine, and CI | Reads the server files of a registry and prints a draft of the Rust chaperone's `install.toml` and one effect manifest for each server, or writes them into a new directory. The operator reviews the draft and installs it as root. The header of the script holds each fact it takes from a server file and each refusal. |
| `yaml-to-toml.py` | OPERATOR on a development machine, and CI | Converts one YAML file of the four kinds to TOML and prints the text. The four kinds are the family file, the server file, the component manifest and the roster. `--check` proves one converted pair: the values are equal, and the count of comments is equal. Status 1 means a pair that differs. Each other fault gives status 2. The header of the script holds the layout and each refusal. The script is temporary. Packet `toml-converter-leave` deletes the script when the tree holds no YAML file of the four kinds. |

Production runs these scripts from `/opt/creche/bin/`. A change here is live
only after `sudo creche-deploy`. The host does not run `yaml-to-toml.py`.

## The watchdog

`rework-watchdog.sh` makes five checks. Then it reads the notice files. Two
rules apply to these two parts.

**Rule 1: a notice file is untrusted input, and only the watchdog sends its
notice.** A notice file is the place where a service records each JSON text
that it refused. The files are `notices/<service>.json` under the state
root, and `_notices.json` in the fault directory of the chaperone.

- The script reads only a regular file whose name ends in `.json`. A lock
  file and a temporary file can be beside a notice file. Their names have
  another end.
- The script reads 65,536 bytes of a file at most. It holds the line of the
  key `push` to an exact pattern. A token of that line is
  `<surface>:<rule>:<seconds>`.
- The script evaluates no byte of a file. It never gives a file to `source`.
- The script calls no `jq` and no Python, because it must run while a
  component tree is in a release. Its header lists each program that it
  needs. Add a new program to that list.
- A line that breaks the pattern gets one line in the journal and no notice.
- The summary of a notice is `json_not_strict <service> <surface> <rule>`.
  The notice goes through `send_notice`, so the bearer is on no command
  line. No service holds that bearer.
- The script sends a notice for a token that it did not send, and for a
  token with a newer `<seconds>`.
- Two limits apply. The script sends one notice for one service, surface and
  rule in 24 hours. It sends 6 notices of this kind at most in one hour, for
  all services together. A later run sends a notice that a limit holds.
  Both limits count in the wall-clock time of the host, so a jump of that
  clock can reset the count of one hour.
- A token whose `<seconds>` is more than one hour after the time of the run
  gets no notice. The journal gets one line with the count of such tokens.
- A run that is older than `NOTICE_STEP_BUDGET_S` seconds starts no send. A
  later run sends the notices that are left.
- The file `watchdog/notices-sent` under the state root is the record of the
  sent notices. It has the modes of the verdict file. The line of a notice
  goes to the record before the send. A run stops the step at the first
  send that fails.
- A send that surely did not reach the hook puts the old record back, and
  a later run sends that notice. Two cases prove it: the host has no hook,
  or curl ends with status 6 or 7. When the script cannot write the old
  record back, that notice is missed.
- No other status proves that the hook took no notice, so the script counts
  the notice as taken. The record keeps the line of that notice, and that
  notice is missed. So a fault gives a missed notice of this kind, never a
  second one. The outage notice keeps its rule: the next run sends it again
  after each failed send.
- The journal line of a failed send names the curl status and the HTTP
  code: `(curl exit <status>, HTTP <code>)`. When curl writes no code, the
  line says `HTTP nothing`.
- Both limits come from the record. A run that cannot read the record, or
  cannot write it, sends no notice of this kind. A record that holds bytes
  and no record line counts as a record that the run cannot read. The
  script reads 262,144 bytes of the record at most, and it writes no larger
  record.
- A record that holds bytes and no record line stops the step in each run.
  The operator removes `watchdog/notices-sent` to repair it. After that,
  each service, surface and rule of the notice files can get one more
  notice, 6 in one hour at most.
- The step changes no verdict and no exit status. It runs in a shell of its
  own, after the script wrote the verdict.
- Only a run of the unit sends a notice of this kind. systemd gives each run
  of a unit an `INVOCATION_ID` of 32 hex digits. A run with no value of
  this form sends none and writes one line in the journal. A run by hand
  can overlap a run of the timer, and both runs would read the same record.
- A shell that systemd started can give its own `INVOCATION_ID` to a run
  by hand. That run then sends.

**Rule 2: a firing unit fails only with status 78.** A firing unit is an
instance of `creche-trigger@`. Check 4 reads the status of the last run of
each instance.

- Status 78 counts as a failed unit. A program ends with that status when
  its config is not valid.
- Each other status counts as no failure. A firing ends with status 1 when
  the session service refuses the job, and no config causes that.
- The failure pushes after two runs in a row, as each other check does.
- The script gives a unit name to `systemctl show` only when the name has
  the exact form `creche-trigger@<family>.service`.
- Each `systemctl` call has the time limit of the other checks. One call
  with no answer in that time ends the read of the firing units for the run.

## Quality gate

`quality-gate.sh` runs ruff, ruff format and pyright in every mode. The
pre-commit hook passes no flag. The pre-push hook passes `--tests-for` with
the paths the push changes, or `--docs` for a Markdown-only push. `--tests`
is the full suite in one process. CI runs the full suite as shards with
`pytest --shard K/N`. Every scoped run also runs
`tests/test_unique_test_basenames.py`.

A scoped run for a product package also runs `vectors/tests`. A product
package is each package but `bin/` and `vectors/`. A change in a product
package can move a vector. That suite fails when a committed vector differs
from what the Python code does.

A scoped run for a path under `integration/proc/` runs the process-level
suite in a pytest process of its own, on four workers. Such a path does not
start the full suite, because the full suite holds no test in that directory.
A push with no other path runs no other suite. One test of `bin/tests` reads
`integration/proc/proc_services.py`. CI runs it.

- The gate sets `CRECHE_PROC_NO_SKIP=1` for that run, as the `proc` job does.
  A test that needs the facade build then fails when the build is missing.
- Build the facade before the push. `integration/proc/AGENTS.md` gives the
  command.
- Prose under `integration/proc/` picks no suite, because no test reads it.
  A push of a package and one line of `integration/proc/AGENTS.md` then
  needs no build.
- `--tests` does not run the process-level suite. CI runs it for each code
  change.

`lib/docsrule.sh` holds the one copy of "does this change touch nothing but
prose?". The pre-push hook, `gate.yml` and `release.yml` source it.
`quality-gate.sh` sources it for the rule of prose under `integration/proc/`.

## The systemd proof

`systemd-proof.sh` proves the restart rule of the daemon units. A daemon
that refuses its start exits with status 78. A unit with
`RestartPreventExitStatus=78` then stays stopped. Only CI runs the script.
The `systemd-proof` job runs the proof, and the scope of each workflow asks
`--unchanged`.

The machine must run Linux with systemd as its first process, and `sudo`
must not ask for a password. On another machine, the script fails before it
starts a unit.

The script makes three units of its own with `systemd-run`, as root. All
three carry the same restart lines, and `RestartPreventExitStatus=78` is one
of them. `RESTART_RULE` in the script lists the lines.

| Case | The unit | What the script demands |
|---|---|---|
| 1 | The main process exits with 78. | The state of the unit is `failed`, and `NRestarts` stays 0. |
| 2 | The main process exits with 1. | `NRestarts` is above 0. |
| 3 | The check process of `ExecStartPre=` exits with 78. The main process never starts. | `NRestarts` is above 0. |

- The script waits after the three starts. It then reads case 2 and case 3
  until each one shows a restart, with a limit on the reads. The constants
  are at the top of the script.
- The script reads case 1 last. Case 2 and case 3 are also the control of
  the measure. They show that a restart reaches `NRestarts` on that machine
  in the time that case 1 had.
- The log has one line for each case, with its `NRestarts`. The three lines
  are there also when a case fails.
- The script removes the three units at its end, also after a check that
  failed. It fails when systemd still holds one of them.
- The three names are fixed, and each one starts with `creche-proof-`. No
  daemon unit has such a name.

The script then gives each file under `systemd/` to `systemd-analyze
verify`, one file in each call. It skips a Markdown file.

- A runner has no component tree, so the tool refuses a unit whose program
  is absent. For such a unit the script checks the syntax only: the tool
  must print no other line. The script prints one line that says so and
  names the absent program.
- Each other line of the tool refuses the unit, for example a key that
  systemd does not know. A failure of the tool with no line refuses the unit
  too.
- The tool does not run as root, because it starts nothing. It reads each
  file as a system unit, a user unit too.

`systemd-proof.sh --unchanged FROM TO` exits with 0 only when the change from
`FROM` to `TO` holds no file of the proof. The scope of `gate.yml` and the
scope of `release.yml` ask it.

- The files of the proof are each path under `systemd/` and the script.
- A CI file is no file of the proof. `tests/test_gate_workflow.py` holds
  each key of the job and the two scope questions.
- A change that the script cannot read is not unchanged.
- A script that fails gives no answer. The scope then runs the proof.
- The tests in `tests/test_systemd_proof.py` use a fake systemd. Only a run
  of the job proves a change to the script.

## Tests

| Test | Pins |
|---|---|
| `test_bin_modes.py` | Every tracked file under `bin/` with a `#!` line is mode 100755 in the index. `creche-deploy` copies modes as they are. |
| `test_bin_path_refs.py` | Every repository path, console script and sibling a script or unit names is in the tree. Marked `docs`. |
| `test_chaperone_install_draft.py` | The print for the fixture registry of `family` equals `install_draft_golden.txt`. `--out-dir` writes the same files, and refuses a directory that exists. A secret reference is a secret and never a word of the command. A state directory names its variable last. Each refusal of a server file. `integration/proc/test_proc_install_draft.py` gives the golden file to the real `chaperone check`. |
| `test_yaml_to_toml.py` | The layout that `yaml-to-toml.py` writes, each refusal, the proof of `--check` and each exit status. The test holds its own YAML texts. It also converts each tracked YAML file of the four kinds and checks each pair. A tree with no such file passes. The values of each TOML text equal what `yaml.safe_load` gives for the YAML text. |
| `test_bin_hook_env.py`, `test_env_upsert.sh` | A re-run never drops another key from a shared env file. |
| `test_envfile_vectors.py` | The command `creche-env` gives the result of each vector of `envfile.layer` and `envfile.raw`. `creche_env_check` and `creche_env_get` of `lib/envfile.sh` give it too, for each vector whose pair asks no account. Each function runs the command of its own tree, with its words, and keeps status 78. A `cd` after the source does not change that tree. |
| `test_envfile_catalog.py` | The catalog of the product: the example value of each required key is a value of its type. The two files name no IPv4 address outside 192.0.2.0/24 and 127.0.0.0/8. Each key of `root-keys.txt` is a catalog key. No key of `default.env` has another value than in an earlier version of the file on the base branch. A copy of `example.env` stops the command with status 78 and names each required key. |
| `test_pre_push_select.sh`, `test_pre_push_scope.py` | What a push tests. |
| `test_gate_workflow.py`, `test_retest_workflow.py` | The two CI files hold to the same shard command, the same `proc` job, the same `suites` job and the same `systemd-proof` job. `!retest` restarts one run. The `systemd-proof` job runs the script on the runner itself, with no container. A scope skips the proof only when the script answers `unchanged`. The last job of `gate.yml` is `gate`, and the last job of `release.yml` is `release`. Each other job name is in both files, and the test fails for a job name that only one file has. The test also names each key at the top level of the two files. One more key there fails the test, for example `defaults` or `env`: such a key changes each job of its file. The `suites` job has the setup of the `proc` job and one pytest run for each of the two old suites of `integration/`. Its last step fails for a test that skipped and for a suite that ran no test. The tag step of the release gets the token and the catalog name, and no variable of a binary component. |
| `test_systemd_proof.py` | `systemd-proof.sh`, against a fake systemd. Each case fails on a wrong result, and a value of `NRestarts` that is no count fails. The script removes its units after each failure. Only the line for an absent program passes a unit file. `--unchanged` says yes only for a change with no file of the proof. |
| `test_handover_wrapper_owner.sh` | `creche-handover` refuses any of its three paths another account can write. |
| `test_unique_test_basenames.py` | No two test modules share a basename across the workspace. |
| `test_strictjson_copies.py` | Each copy of a copied module has the same bytes. A copied module is a file `strictjson.py` or `notices.py` at `<package>/src/<module>/`. A name with one copy passes, and a name with no copy passes. |
| `test_rfc3339_copies.py` | Each copy of `rfc3339.py` has the same bytes. A copy is a file `rfc3339.py` at `<package>/src/<module>/`. One copy passes. The test fails when the search does not find the copy of the noticeboard. The search and the comparison are the two functions of `test_strictjson_copies.py`. |
| `test_git_env_dropped.py` | A test run that git starts writes nothing into the repository of the caller. The root `conftest.py` drops the five variables that the hooks unset. |
| `test_git_config_dropped.py` | No `git` child of a test run reads the config file of a person or of the system. The same holds for the ignore file and the attributes file of a person. The root `conftest.py` sets the variables that do this. The registry writer of the noticeboard, `noticeboard/src/noticeboard/registrywrite.py`, starts `git` with `HOME` and with none of those variables. The root `conftest.py` gives each test under `noticeboard/tests` an empty home directory. That child then starts no program that a config file below `HOME` names, but it still reads the config file of the system. |
| `test_git_background_dropped.py` | No `git` command that inherits the environment of a test run starts the maintenance that `git` does not wait for. That holds for a commit, a merge, a fetch and a repository that receives a push. The root `conftest.py` sets the two settings that do this. A fixture that asks for the maintenance gets it in the foreground. |
| `test_ignored_signal_kept.py` | A test run that starts with SIGINT, SIGTERM or SIGHUP ignored passes the same tests, and the signal stays ignored. The fixture of the root `conftest.py` still fails a handler that a test leaves. |
| `test_creche_deploy.py`, `test_rework_watchdog.py`, `test_rework_registry_sync.py`, `test_sbx_drift_check.py`, `test_sync_code_corpus.py`, `test_provision_library.py`, `test_rework_intake_unit.py` | Each script, against binstubs and a temp root. |

## Adding a script

1. Write the header: what it does, who runs it, the exact command, the
   prerequisites.
2. Pick the `set` line for the family.
3. Make it idempotent (setup) or self-cleaning (gate).
4. If a unit calls it, the `ExecStart` path is `/opt/creche/bin/<name>`. Add
   the unit under `systemd/`.
5. `quality-gate.sh` does not lint bash. Review it yourself.
6. Commit it executable: `chmod +x` before the first `git add`, or
   `git update-index --chmod=+x <path>` after.

## Retiring a script

Delete a script in the same pass that retires what it drove. Its history
stays in the git log.

## Known gaps

- `rework-watchdog.sh`, the form of a notice file. No contract text gives
  that form yet. The script takes this reading: the key `push` is on a line
  of its own, behind one space, with one space after the colon. A JSON
  writer with sorted keys and an indent of one space writes that line. A
  service name has 64 characters at most, and one line has 256 tokens at
  most. A `<seconds>` that is more than one hour after the time of the run
  gets no notice. A change costs the patterns of the script and their tests.
- `rework-watchdog.sh`, a failed send of a JSON notice. No contract text
  says which failed send the hook can have taken. The script takes this
  reading: only two cases count as proof that the hook took no notice. The
  two cases are a host with no hook, and curl status 6 or 7. A curl status
  of 22 means that the hook answered with an HTTP error. The owner of the
  repository decided this case. The record keeps the line of that notice,
  so that notice is missed. So a hook that refuses the bearer misses each
  JSON notice. The journal line of each failed send names the HTTP code. A
  change costs the list of statuses and its tests.
- `yaml-to-toml.py`, a null value in a list. TOML has no null. The script
  drops a null value of a mapping key and names it on stderr. No rule says
  what a null item of a list becomes, and no tracked file holds one. The
  script refuses such a file, because a dropped item moves each later item.
  A change costs one branch in the reader of a list.
