# bin

Every script that runs on the host, plus the three shell libraries, one tool
for a development machine and the tests behind them. One house style across
every file. The root `AGENTS.md` applies here too.

Bash only: `#!/usr/bin/env bash`. A script that may run on a development
machine stays bash 3.2-clean: no associative arrays, no `mapfile`, no
`${var,,}`, no `&>>`.

Two scripts are exceptions. Each one is Python, with the first line
`#!/usr/bin/env python3`. The gate checks each one with ruff and not with
pyright: `pyrightconfig.json` names no path under `bin/`.

- `journal-scan.py`. Reason: it reads JSON, and the standard library of
  Python has a JSON reader. It needs only the standard library, so the Python
  of the host starts it without the venv. It refuses a Python version that is
  older than the version in its header.
- `yaml-to-toml.py`. Reason: it reads YAML with PyYAML. The workspace venv
  holds PyYAML, so start the script with `uv run`.

## Every script's header says who runs it

`ROOT`, `OPERATOR` or `CI`, in the first three lines, with the exact command.
A reader must not infer it from the `id -u` guard. A root script asserts
`[[ "$(id -u)" == "0" ]]`. An operator script asserts the opposite.

| Family | Files | Contract |
|---|---|---|
| Setup | `provision-library.sh` | Idempotent, not a no-op. Every step checks before it writes. A value that has a current answer is upserted in place. A token minted once is never replaced in silence. |
| Operations | `creche-deploy`, `creche-handover`, `creche-handover-intake`, `rework-watchdog.sh`, `rework-registry-sync.sh`, `sbx-drift-check.sh`, `sync-code-corpus.sh` | Run unattended from units and timers. Fail loudly into the journal. |
| Checks | `quality-gate.sh`, `rust-gate.sh`, `rust-coverage.sh`, `systemd-proof.sh` | Run from the hooks and from CI. `rust-coverage.sh` and `systemd-proof.sh` run in CI only. No unit runs them. They use `set -euo pipefail`. In `quality-gate.sh`, in `rust-gate.sh` and in `systemd-proof.sh`, the first failed check stops the run. `rust-coverage.sh` stops when its cargo step fails. It then reports each failure of its checks. |
| Library | `lib/envfile.sh`, `lib/docsrule.sh`, `lib/rustrule.sh` | Sourced only, never executed. Say "Sourced only" in the header. That marker exempts the file from the mode rule below. |
| Tools | `yaml-to-toml.py` | Run by hand on a development machine, and from a test in CI. No unit runs a tool, and the host does not need one. A tool reads the files that its command names and changes no file. |
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
| `rework-watchdog.sh` | OPERATOR, every minute | The outage alarm. Checks the chaperone's `/healthz`, `caregiver`'s freshness, `attendance`'s socket and `is-failed` for the units. Pushes once per change of verdict. `--last` prints the verdict. Shares no fate with what it watches. |
| `rework-registry-sync.sh` | OPERATOR, every minute | `git fetch --prune`, then `git merge --ff-only`, on the registry checkout. Never rebases, resets, cleans or pushes. |
| `sbx-drift-check.sh` | OPERATOR, daily | Read-only. Alarms when the global sbx policy holds any network allow, or when a per-sandbox rule allows a host that is not the LAN address and not in the seeded allowlist. |
| `journal-scan.py` | OPERATOR, by hand | Read-only. A person starts it, and the one argument is the sessions root. It counts the journal lines in which UTF-8 cannot encode a key or a value. It also counts the `pi_event` lines whose event the host cut for another cause than its size. It prints each count. The only text of a journal line that the output can show is a `kind` word. |
| `sync-code-corpus.sh` | OPERATOR, hourly | Refreshes the dedicated code clones the library indexes. The repository list lives outside the corpus. |
| `provision-library.sh` | OPERATOR | One corpus: the image, the sandbox, TEI-only egress, the timer. Needs `AGENT_LAN_ADDRESS` from the site file. |
| `quality-gate.sh` | OPERATOR, from the hooks and CI | ruff, ruff format, pyright, then pytest as asked: `--tests`, `--tests-for PATH...` or `--docs`. For a change that touches `rust/`, it also runs `rust-gate.sh`. For a push that changes `vectors/`, it runs `rust-gate.sh` when `cargo` is on `PATH`. For a push that changes a file under `integration/proc/` that is not prose, it runs the process-level suite. |
| `rust-gate.sh` | OPERATOR and CI, from `quality-gate.sh` and from the `rust` job | The `[lints]` check, three text checks, `cargo fmt`, `cargo clippy` and `cargo deny` on the workspace under `rust/`. The text checks are the include check, the panic check and the public-field check. The script has a list of the crates that the public-field check does not read yet. `--tests` adds `cargo test`. `cargo deny` runs where `cargo-deny` is on `PATH`. |
| `rust-coverage.sh` | CI and OPERATOR, from the `rust-coverage` job | The coverage rule of the workspace under `rust/`. Runs the tests with `cargo llvm-cov`, then checks the report against `rust/coverage-files.txt`. Fails when a region or a line of a listed file ran in no test. Takes each count from the segments of the report, and none from a summary. Prints one line with the counts of each crate, and one line for each failure. `--report FILE` checks a report of an earlier run and starts no cargo. `--branch` adds the branches, on a nightly toolchain. Needs `python3`, `cargo-llvm-cov` and the component `llvm-tools-preview`. Sources `lib/rustrule.sh` for the name of the Rust directory. `rust/AGENTS.md` has the rule. |
| `systemd-proof.sh` | CI, from the `systemd-proof` job and from the scope of each workflow | Proves the restart rule of the daemon units on the systemd of the runner, with three transient units. Then gives each unit file under `systemd/` to `systemd-analyze verify`. `--unchanged FROM TO` says if a change needs no proof. |
| `yaml-to-toml.py` | OPERATOR on a development machine, and CI | Converts one YAML file of the four kinds to TOML and prints the text. The four kinds are the family file, the server file, the component manifest and the roster. `--check` proves one converted pair: the values are equal, and the count of comments is equal. Status 1 means a pair that differs. Each other fault gives status 2. The header of the script holds the layout and each refusal. The script is temporary. Packet `toml-converter-leave` deletes the script when the tree holds no YAML file of the four kinds. |

Production runs these scripts from `/opt/creche/bin/`. A change here is live
only after `sudo creche-deploy`. The host does not run `yaml-to-toml.py`.

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
  A test that needs the playpen bundle then fails when the bundle is missing.
- Build the bundle before the push. `integration/proc/AGENTS.md` gives the
  command.
- Prose under `integration/proc/` picks no suite, because no test reads it.
  A push of a package and one line of `integration/proc/AGENTS.md` then
  needs no bundle.
- `--tests` does not run the process-level suite. CI runs it for each code
  change.

`lib/docsrule.sh` holds the one copy of "does this change touch nothing but
prose?". The pre-push hook, `gate.yml` and `release.yml` source it.
`quality-gate.sh` sources it for the rule of prose under `integration/proc/`.

`quality-gate.sh` runs `rust-gate.sh` only for a change that touches `rust/`,
and for a push that changes `vectors/`. `lib/rustrule.sh` holds the one copy
of that rule. `quality-gate.sh`, `gate.yml` and `release.yml` source it.

| Mode | The change touches `rust/` when | `rust-gate.sh` runs |
|---|---|---|
| no flag | the index or the work tree differs from `HEAD` under `rust/` | the `[lints]` check, the three text checks, `cargo fmt`, `cargo clippy`, `cargo deny` |
| `--tests-for` | one path or more is under `rust/` | the same, then `cargo test` |
| `--tests` | always | the same, then `cargo test` |
| `--docs` | never | nothing |

- A change that touches nothing under `rust/` needs no `cargo` on `PATH`,
  because some sessions commit from a sandbox that has no Rust toolchain. It
  starts no cargo step, with one exception: a push that changes `vectors/`.
- A change that touches `rust/` with no `cargo` on `PATH` fails before the
  first check.
- The commit that concludes a merge is a special case of the no-flag mode.
  When the index and the work tree hold exactly the `rust/` of the other
  side, the change touches no `rust/`. A session with no `cargo` can then
  merge `main`. An own edit under `rust/` in that commit still counts.
- When git cannot read the state, the no-flag mode runs `rust-gate.sh`. The
  gate names that cause in its line.
- In `--tests-for` mode a path under `rust/` picks no pytest suite. It is not
  a path in no package, so it does not start the full Python suite.
- CI takes a wider answer than the hooks. There, a change to a file of the
  Rust checks also runs `rust-gate.sh`: the script, `rust-coverage.sh`,
  `lib/rustrule.sh`, `gate.yml`, `release.yml` and the scope action. The same
  change runs `rust-coverage.sh` in its job. The tests here use a fake
  `cargo`, so only that run proves such a change. A commit or a push of those
  files needs no `cargo`.
- The Rust tests read `vectors/data`. In CI, a code change under `vectors/`
  runs `rust-gate.sh`. In `--tests-for` mode, a path under `vectors/` runs
  `rust-gate.sh --tests` when `cargo` is on `PATH`. Without `cargo`, the gate
  prints one line and passes. A session with no Rust toolchain can then
  regenerate the vectors and push. A commit of a vector starts no cargo step.
- The `[lints]` check refuses a crate that has no `[lints]` table with the
  line `workspace = true`. Such a crate builds with no lint of the workspace.
  The check also fails when it finds no crate.
- The include check refuses a Rust source file that includes a Markdown
  file. A change of Markdown only runs no cargo step, so such a file can
  break a doc test with no cargo run.
- The panic check permits the word `catch_unwind` in three places only.
  It refuses each other Rust source file under `rust/crates` with that
  word. A reviewer then knows where each panic boundary is. The three
  places are:
  1. The crate `creche-runtime`.
  2. `src/entry.rs` in a crate whose `Cargo.toml` does not name
     `creche-runtime`.
  3. Test code.
- The public-field check refuses `pub` on a field of a struct, in each
  crate under `rust/crates`. It permits the forms `pub(crate)` and
  `pub(super)`. It reads no test code.
- The public-field check does not read each crate yet. The list
  `FIELD_CHECK_SKIPS` of the script names the crates that it skips. Add no
  name to it. "Known gaps" of `rust/AGENTS.md` has the names, and the
  packets that delete them.
- The panic check and the public-field check read the text and need no
  `cargo`. `rust/AGENTS.md`, "Checks", has the rules of both, and what
  test code is.
- Each of the two checks fails for a file that ends inside a test module.
  The check then read no code below the first line of that module.
- `cargo deny --locked check` runs after `cargo clippy`. It holds the locked
  crates to `rust/deny.toml`: the licenses, the sources, the bans and the
  advisories. `rust/AGENTS.md` has the table.
- That step needs `cargo-deny` on `PATH`. rustup does not install it.
  Without it, `rust-gate.sh` prints one line and runs each other step.
- In CI, `rust-gate.sh` fails without `cargo-deny`. The script reads `CI`,
  which a runner sets. The `rust` job installs `cargo-deny` from a release
  archive. It checks the SHA-256 of the archive before the unpack.

## The systemd proof

`systemd-proof.sh` proves the restart rule of `rust/AGENTS.md`, "The config
of a process". Only CI runs it. The `systemd-proof` job runs the proof, and
the scope of each workflow asks `--unchanged`.

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
| `test_yaml_to_toml.py` | The layout that `yaml-to-toml.py` writes, each refusal, the proof of `--check` and each exit status. The test holds its own YAML texts. It also converts each tracked YAML file of the four kinds and checks each pair. A tree with no such file passes. The values of each TOML text equal what `yaml.safe_load` gives for the YAML text. |
| `test_bin_hook_env.py`, `test_env_upsert.sh` | A re-run never drops another key from a shared env file. |
| `test_pre_push_select.sh`, `test_pre_push_scope.py` | What a push tests. |
| `test_rust_gate.py` | When the gate runs cargo, the exact cargo steps, the refusal with no `cargo` on `PATH`, the rule for `vectors/`, the `[lints]` check and the include check. The `cargo deny` step: it runs where `cargo-deny` is on `PATH`, a machine without it passes with one line, and CI without it fails. The panic check: each of the three places passes, and a file in another place fails. The public-field check: what fails, what passes, and each name of its list. Both checks: what test code is, and a file that ends inside a test module fails. |
| `test_rust_workspace.py` | Each entry of the lint gate in `rust/Cargo.toml`, and the two `[profile]` tables there. Each table of `rust/deny.toml`. No Cargo file is outside `rust/`. No Rust source file holds a table of differences. The test looks for the name `DEVIATIONS` and for a struct whose name starts with `Deviation`. The test has a list of the crates that it does not read yet, and it fails for a name with no crate. A change under `rust/` mints no tag. |
| `test_rust_config_units.py` | Each Rust config type names one daemon unit. Each daemon unit holds `Restart=always` and no `RestartPreventExitStatus`. Three daemon units hold an `ExecStartPre=` check, and the Rust config type of each one says so. Each variable of a unit has a constant in the Rust module of its daemon. That constant holds the name as a text, or it reads the name from the Rust module `endpoints`. The chaperone module reads two names from that module. No other file of the Rust config code holds the text of a name of `endpoints`. |
| `test_rust_coverage.py` | `rust-coverage.sh` against a small report, with no `cargo`. A listed file fails when one of its regions or lines ran in no test. The counts come from the segments of the report: two copies of a function can each run one part of it. One report holds the segments that `llvm-cov` wrote for a real file. A listed file that is not in the report fails. The text `coverage(off)` in a listed file fails. A file of `rust/crates/chaperone-policy/src` outside the list fails. A list or a report in another form stops the run. A fake `cargo` shows the cargo step, word for word. The Python program of the script imports no module of the caller, and ruff passes it. |
| `test_gate_workflow.py`, `test_retest_workflow.py` | The two CI files hold to the same shard command, the same `proc` job, the same `rust` job and the same `systemd-proof` job. `!retest` restarts one run. The `rust` job checks the SHA-256 of the `cargo-deny` archive before the unpack. The cache of that job holds the copy of the crates.io index that cargo keeps. The key of the cache holds a hash of the toolchain file and of the lock file. The `systemd-proof` job runs the script on the runner itself, with no container. A scope skips the proof only when the script answers `unchanged`. Only `gate.yml` has the `suites` job. The table `ONLY_IN` of the test holds that one difference. The job has the setup of the `proc` job and one pytest run for each of the two old suites of `integration/`. Its last step fails for a test that skipped and for a suite that ran no test. The two files also hold the same `rust-coverage` job. That job runs `rust-coverage.sh` with no flag, and it checks the SHA-256 of the `cargo-llvm-cov` archive before the unpack. The check `gate` needs the job. |
| `test_systemd_proof.py` | `systemd-proof.sh`, against a fake systemd. Each case fails on a wrong result, and a value of `NRestarts` that is no count fails. The script removes its units after each failure. Only the line for an absent program passes a unit file. `--unchanged` says yes only for a change with no file of the proof. The status and the unit line of the script equal the two Rust constants. |
| `test_handover_wrapper_owner.sh` | `creche-handover` refuses any of its three paths another account can write. |
| `test_unique_test_basenames.py` | No two test modules share a basename across the workspace. |
| `test_git_env_dropped.py` | A test run that git starts writes nothing into the repository of the caller. The root `conftest.py` drops the five variables that the hooks unset. |
| `test_git_config_dropped.py` | No `git` child of a test run reads the config file of a person or of the system. The same holds for the ignore file and the attributes file of a person. The root `conftest.py` sets the variables that do this. |
| `test_git_background_dropped.py` | No `git` command that inherits the environment of a test run starts the maintenance that `git` does not wait for. That holds for a commit, a merge, a fetch and a repository that receives a push. The root `conftest.py` sets the two settings that do this. A fixture that asks for the maintenance gets it in the foreground. |
| `test_ignored_signal_kept.py` | A test run that starts with SIGINT, SIGTERM or SIGHUP ignored passes the same tests, and the signal stays ignored. The fixture of the root `conftest.py` still fails a handler that a test leaves. |
| `test_journal_scan.py` | Each count of `journal-scan.py` equals a known answer. A run changes no file and no directory. The output holds no family name, no session name and no marker text of a line. The three copied constants, the place of a journal and the keys of a cut event equal their source in `attendance`. The oldest Python version of the program equals the one in the root `pyproject.toml`. |
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

- `yaml-to-toml.py`, a null value in a list. TOML has no null. The script
  drops a null value of a mapping key and names it on stderr. No rule says
  what a null item of a list becomes, and no tracked file holds one. The
  script refuses such a file, because a dropped item moves each later item.
  A change costs one branch in the reader of a list.
