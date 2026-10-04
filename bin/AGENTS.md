# bin

Every script that runs on the host, plus the three shell libraries and the
tests behind them. One house style across every file. The root `AGENTS.md`
applies here too.

Bash only: `#!/usr/bin/env bash`. A script that may run on a development
machine stays bash 3.2-clean: no associative arrays, no `mapfile`, no
`${var,,}`, no `&>>`.

## Every script's header says who runs it

`ROOT`, `OPERATOR` or `CI`, in the first three lines, with the exact command.
A reader must not infer it from the `id -u` guard. A root script asserts
`[[ "$(id -u)" == "0" ]]`. An operator script asserts the opposite.

| Family | Files | Contract |
|---|---|---|
| Setup | `provision-library.sh` | Idempotent, not a no-op. Every step checks before it writes. A value that has a current answer is upserted in place. A token minted once is never replaced in silence. |
| Operations | `creche-deploy`, `creche-handover`, `creche-handover-intake`, `rework-watchdog.sh`, `rework-registry-sync.sh`, `sbx-drift-check.sh`, `sync-code-corpus.sh`, `quality-gate.sh`, `rust-gate.sh` | Run unattended from units and timers. Fail loudly into the journal. |
| Library | `lib/envfile.sh`, `lib/docsrule.sh`, `lib/rustrule.sh` | Sourced only, never executed. Say "Sourced only" in the header. That marker exempts the file from the mode rule below. |
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
| `sync-code-corpus.sh` | OPERATOR, hourly | Refreshes the dedicated code clones the library indexes. The repository list lives outside the corpus. |
| `provision-library.sh` | OPERATOR | One corpus: the image, the sandbox, TEI-only egress, the timer. Needs `AGENT_LAN_ADDRESS` from the site file. |
| `quality-gate.sh` | OPERATOR, from the hooks and CI | ruff, ruff format, pyright, then pytest as asked: `--tests`, `--tests-for PATH...` or `--docs`. For a change that touches `rust/`, it also runs `rust-gate.sh`. |
| `rust-gate.sh` | OPERATOR and CI, from `quality-gate.sh` and from the `rust` job | The `[lints]` check, the include check, `cargo fmt` and `cargo clippy` on the workspace under `rust/`. `--tests` adds `cargo test`. |

Production runs these scripts from `/opt/creche/bin/`. A change here is live
only after `sudo creche-deploy`.

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

`lib/docsrule.sh` holds the one copy of "does this change touch nothing but
prose?". The pre-push hook, `gate.yml` and `release.yml` source it.

`quality-gate.sh` runs `rust-gate.sh` only for a change that touches `rust/`.
`lib/rustrule.sh` holds the one copy of that rule. `quality-gate.sh`,
`gate.yml` and `release.yml` source it.

| Mode | The change touches `rust/` when | `rust-gate.sh` runs |
|---|---|---|
| no flag | the index or the work tree differs from `HEAD` under `rust/` | the `[lints]` check, the include check, `cargo fmt`, `cargo clippy` |
| `--tests-for` | one path or more is under `rust/` | the same, then `cargo test` |
| `--tests` | always | the same, then `cargo test` |
| `--docs` | never | nothing |

- A change that touches nothing under `rust/` starts no cargo step. It needs
  no `cargo` on `PATH`, because some sessions commit from a sandbox that has
  no Rust toolchain.
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
  Rust checks also runs `rust-gate.sh`: the script, `lib/rustrule.sh`,
  `gate.yml`, `release.yml` and the scope action. The tests here use a fake
  `cargo`, so only that run proves such a change. A commit or a push of those
  files needs no `cargo`.
- The `[lints]` check refuses a crate that has no `[lints]` table with the
  line `workspace = true`. Such a crate builds with no lint of the workspace.
  The check also fails when it finds no crate.
- The include check refuses a Rust source file that includes a Markdown
  file. A change of Markdown only runs no cargo step, so such a file can
  break a doc test with no cargo run.

## Tests

| Test | Pins |
|---|---|
| `test_bin_modes.py` | Every tracked file under `bin/` with a `#!` line is mode 100755 in the index. `creche-deploy` copies modes as they are. |
| `test_bin_path_refs.py` | Every repository path, console script and sibling a script or unit names is in the tree. Marked `docs`. |
| `test_bin_hook_env.py`, `test_env_upsert.sh` | A re-run never drops another key from a shared env file. |
| `test_pre_push_select.sh`, `test_pre_push_scope.py` | What a push tests. |
| `test_rust_gate.py` | When the gate runs cargo, the exact cargo steps, the refusal with no `cargo` on `PATH`, the `[lints]` check and the include check. |
| `test_rust_workspace.py` | Each entry of the lint gate in `rust/Cargo.toml`. No Cargo file is outside `rust/`. A change under `rust/` mints no tag. |
| `test_gate_workflow.py`, `test_retest_workflow.py` | The two CI files hold to the same shard command, the same `proc` job and the same `rust` job, and `!retest` restarts one run. |
| `test_handover_wrapper_owner.sh` | `creche-handover` refuses any of its three paths another account can write. |
| `test_unique_test_basenames.py` | No two test modules share a basename across the workspace. |
| `test_git_env_dropped.py` | A test run that git starts writes nothing into the repository of the caller. The root `conftest.py` drops the five variables that the hooks unset. |
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
