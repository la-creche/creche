# bin

This directory holds the scripts that run on the host and the scripts of CI.
It also holds four shell libraries, one tool for a development machine and
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
| Setup | `provision-library.sh`, `fleet-up` | Idempotent, not a no-op. Every step checks before it writes. A value that has a current answer is upserted in place. A token minted once is never replaced in silence. |
| Operations | `creche-deploy`, `creche-handover` | Run unattended from units and timers. Fail loudly into the journal. |
| Checks | `quality-gate.sh`, `systemd-proof.sh`, `vm-smoke-setup.sh` | Run from the hooks and from CI. `systemd-proof.sh` and `vm-smoke-setup.sh` run in CI only. No unit runs them. They use `set -euo pipefail`. The first failed check stops the run. |
| Build | `build-artifacts.sh` | It runs in CI only, from the job `artifacts` of `release.yml` and the job `vmm-archive` of `gate.yml`. No hook and no unit runs it, and the host never runs it. It uses `set -euo pipefail`: the first fault stops the run with one line, and the run then writes no digest list. It can use the GNU tools of a runner. It stays bash 3.2-clean, because its tests run it on a development machine. |
| Library | `lib/envfile.sh`, `lib/docsrule.sh`, `lib/artifactrows.sh`, `lib/chbuild.sh`, `lib/elfstatic.sh` | Sourced only, never executed. Say "Sourced only" in the header. That marker exempts the file from the mode rule below. |
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
| `creche-deploy` | ROOT, the single sudoers entry | Puts this repository at `/opt/creche` and the site at `/opt/private-docs`, fills the venv. The site checkout serves the compose stack and the personal units. `/etc/creche` links into the `host` release instead. It restarts no unit. Origin is the operator's checkout, so root needs no GitHub credential. It installs no MCP server. |
| `creche-handover` | ROOT, from `creche-handover.path` | Decrypts the site's sops file and execs `handover-exec` under it. Installed to `/usr/local/sbin` by hand, never by a release. Refuses a path another account can write. |
| `provision-library.sh` | OPERATOR | One corpus: the image, the sandbox, TEI-only egress, the timer. Needs `AGENT_LAN_ADDRESS` from the site file. |
| `fleet-up` | OPERATOR | Draft. `check`, `up`, `down` of the fleet layer: the tokens, the keys, the env files, the state directories and the user units. `check` is read-only and lists the root checklist. `up` records each write, and `down` removes only a recorded path. `docs/fleet-bootstrap.md` is the design. |
| `quality-gate.sh` | OPERATOR, from the hooks and CI | ruff, ruff format, pyright, then pytest as asked: `--tests` or `--docs`. |
| `systemd-proof.sh` | CI, from the `systemd-proof` job and from the scope of each workflow | Proves the restart rule of the daemon units on the systemd of the runner, with three transient units. Then gives each unit file under `systemd/` to `systemd-analyze verify`. `--unchanged FROM TO` says if a change needs no proof. |
| `vm-smoke-setup.sh` | CI, from the `kvm` job of each workflow | Readies the runner for the kvm_ tests with the runner's own sudo (decision CH36 G20): opens `/dev/kvm` and `/dev/vhost-vsock`, loads `vhost_vsock`, gives the user manager the hard `RLIMIT_NOFILE` of Hyperion, 1048576, so `vmm serve` reaches its fd need of 102720 (decision CH67 rule 4), and fails unless a user unit gets it, then starts the user manager. Installs `musl-tools`, the pinned toolchain and the musl target itself, because the job runs the script before its `toolchain` step (CH62 rule 3). Builds CH and ch-remote on every run with the 13 words of `lib/chbuild.sh` for `x86_64-unknown-linux-musl`, with no cache (decision CH75). That build starts first, in the background, with its own `CARGO_TARGET_DIR` and a limit of 12 minutes. The other steps run beside it, and its two other builds run under `nice -n 19`. Fetches the guest kernel by its digest into `CRECHE_KVM_DIR`. Builds `guest-init` and the stand-in facade `standin_facade` static for musl there, refuses either program when it has a PT_INTERP header, after it proves that the check refuses the runner's dynamic `/bin/bash`. The stand-in is a test program in no release row, and the guest-init kvm tests put it on their root disks (CH62 rule 12, CH64 rule 2). Packs `guest-init` as `/init` in `guest-init.cpio` with `dev/console` 5:1, and fails unless GNU cpio lists exactly those newc entries, owned by root (CH7-5). Builds a busybox initramfs there and keeps the static `busybox` program. Loads the slot profiles of CH, and fails unless each `creche-ch-slot-<k>` label is in enforce mode (decisions CH8-1, CH55). Builds the test programs of `rust/` with `cargo test --no-run`. Then waits for the CH build: a failed build prints its log and fails the run. CH and ch-remote must be static, and CH must print `v54.0.0` (fact 78) before any test. Installs CH at `/opt/components/vmm/bin/cloud-hypervisor`. Logs `df -h /`. Exports `XDG_RUNTIME_DIR` to the later steps of the job. Refuses to run outside GitHub Actions. |
| `build-artifacts.sh` | CI, from the job `artifacts` of `release.yml` | It builds, walks, packs and checks the archive of each binary component. "The archives of the binary components" has the rules. |
| `chaperone-install-draft.py` | OPERATOR on a development machine, and CI | Reads the server files of a registry and prints a draft of the Rust chaperone's `install.toml` and one effect manifest for each server, or writes them into a new directory. The operator reviews the draft and installs it as root. The header of the script holds each fact it takes from a server file and each refusal. |
| `yaml-to-toml.py` | OPERATOR on a development machine, and CI | Converts one YAML file of the three kinds to TOML and prints the text. The three kinds are the family file, the server file and the component manifest. `--check` proves one converted pair: the values are equal, and the count of comments is equal. Status 1 means a pair that differs. Each other fault gives status 2. The header of the script holds the layout and each refusal. The script is temporary. Packet `toml-converter-leave` deletes the script when the tree holds no YAML file of the three kinds. |

Production runs these scripts from `/opt/creche/bin/`. A change here is live
only after `sudo creche-deploy`. The host does not run `yaml-to-toml.py`.

The scripts of the host's own units are not here: the watchdog, the registry
sync, the corpus sync and the drift check. They are files of creche-release,
with their tests, and a `host` release installs them at
`/opt/components/host/bin`.

## Quality gate

`quality-gate.sh` runs ruff, ruff format and pyright in every mode. The
two hooks pass no flag, so they run no test. `--tests` is the full suite in
one process. `--docs` is the tests marked `docs`, for a Markdown-only PR in
CI. CI runs the full suite as shards with `pytest --shard K/N`.

`lib/docsrule.sh` holds the one copy of "does this change touch nothing but
prose?". `gate.yml` and `release.yml` source it.

## The archives of the binary components

`build-artifacts.sh` makes the archive of each binary component. The job
`sign` of `release.yml` signs each archive, and the tag script attaches the
archive and its signature bundle to the Release of the component. The host
fetches the two files and builds nothing.

The script reads the rows from `handover artifacts --repo "$ARTIFACT_REPO"`.
A row holds these values:

- the component and the target
- the names of the two assets
- the name of the install variable
- three limits
- each build command of the manifest

The script holds no copy of a value of a row. Each variable that names a
path must hold an absolute path. The script changes its directory, so it
refuses a relative path with one line.

`ARTIFACT_PLANNED` names the components that get a tag in the run, one word
each. The job `plan` of `release.yml` gives it, from the dry run of the tag
script. The script builds only the rows of these components. A planned name
of a disk-image row is skipped: the script reads the names of
`handover disk-assets --repo "$ARTIFACT_REPO"` only for a planned name with
no binary row, and another job builds the files of that row (decision CH76
rule 5). Each other planned name with no row stops the run. Unset or empty
means no planned component.

- With no planned row, the script prints one line and starts no `rustup`
  and no `cargo`. It writes an empty `SHA256SUMS` and an empty step output
  `digests`.
- With a planned row or more, it installs the toolchain of
  `rust/rust-toolchain.toml` and adds the target of each planned row.
- It runs each command of a row in the repository root, as a list of words.
  No shell reads a word. The install variable of the row names an empty
  tree. `RUSTFLAGS` holds one `--remap-path-prefix` each for the workspace,
  the cargo home and the temporary directory of the runner.
- The tree must hold one program or more directly in `bin/`, and nothing
  else.
- `handover check-tree --kind binary` walks the tree. The needles are the
  workspace, the home, the cargo home and the temporary directory of the
  runner.
- The pack gives each member the name `<component>/bin/<program>`, the
  owner 0, the group 0, the time 0 and the mode 0755. The members are in
  sorted order, and the archive has no directory member. gzip writes no name
  and no time.
- The script refuses an archive over a limit of its row. The three limits
  are the bytes of the archive, the count of members and the bytes of the
  tree. Root refuses such an archive, so CI must not sign it.
- At the end, `SHA256SUMS` holds one line of `sha256sum` for each archive,
  and the step output `digests` holds the same text.

`lib/artifactrows.sh` is the one reader of the rows. The reader refuses
each text that is not in the line form of `handover artifacts`. It also
refuses two rows with one component, one archive or one bundle. Its
`read_disk_names` reads the line form of `handover disk-assets` and keeps
the component of each row. It refuses each other text and two rows with one
component. The reader gives the reason of a refusal to its caller. The
script then stops with one line.

The job `sign` of `release.yml` signs an archive `*.tar.gz` and each of the
four files of a disk-image row: `digests.json`, `vmlinux-x86_64.zst`,
`base.ext4.zst` and `python.ext4.zst`. The cosign fetch of the `sign` job
and of the `release` job runs when the directory holds any one of them.
The job `disks` builds those four files with
`disks/bin/playpen-release-disks` when the plan names `disks`
(`disks/AGENTS.md`). The `sign` job reads the `digests` output of both the
`artifacts` job and the `disks` job (decision CH76 rule 5).

`lib/chbuild.sh` holds the one copy of the CH build of decision CH35 rule 3:
the 13 words `CH_BUILD` and the version `CH_BUILD_VERSION` that the
build prints. `vm-smoke-setup.sh` runs the words for the kvm job (decision
CH75). `tests/test_artifactrows.py` reads its vmm row from the file, and
holds the rev and the version equal to `CH_REV` and `CH_VERSION` of
`rust/crates/vmm-store/src/key.rs`.

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
| `test_artifactrows.py` | `read_rows` of `lib/artifactrows.sh`, sourced in bash, with `set -e` and without it. The rows are the real line form of `handover artifacts`. The reader keeps each word of each command, also for two commands in one row. A last row with no line feed is a whole row. Each text in another form is refused with its reason, and the reader exits no shell. `read_disk_names` reads the names of the real line form of `handover disk-assets` and refuses each other text with its reason. The CH build of `lib/chbuild.sh` is one command of 13 words, and its rev and version are `CH_REV` and `CH_VERSION` of the key check. |
| `test_build_artifacts.py` | `build-artifacts.sh`, against stand-ins for `rustup`, `cargo`, `tar`, `gzip` and the `handover` command. The rows are the real line form. No row: one line, an empty digest list and no call of `rustup` or `cargo`. One row: the words and the variables of each call, the needles of the walk, the member names and the digest file. Each refusal stops the run with one line and writes no digest list. The refusals are a tree with no program, a tree with another thing, a program name that is not plain and a directory in `bin/`. The run also stops for a walk fault, a build fault, each of the three limits and a row in another form. A relative path in a variable stops the run before the first call. No word of a command passes through a shell. Only the rows that `ARTIFACT_PLANNED` names are built. No planned row builds nothing, and a planned name with no row stops the run. A planned disk-image row is skipped, and the script reads `handover disk-assets` only for a planned name with no binary row. `build-artifacts.sh` sources `lib/artifactrows.sh`. No other file under `bin/` outside `tests/` defines `read_rows` or `read_disk_names`, or names the key `max-unpacked-bytes` or `out-env`. |
| `test_bin_path_refs.py` | Every repository path, console script and sibling a script or unit names is in the tree. Marked `docs`. |
| `test_chaperone_install_draft.py` | The print for the fixture registry of `family` equals `install_draft_golden.txt`. `--out-dir` writes the same files, and refuses a directory that exists. A secret reference is a secret and never a word of the command. A state directory names its variable last. Each refusal of a server file. `integration/proc/test_proc_install_draft.py` gives the golden file to the real `chaperone check`. |
| `test_yaml_to_toml.py` | The layout that `yaml-to-toml.py` writes, each refusal, the proof of `--check` and each exit status. The test holds its own YAML texts. It also converts each tracked YAML file of the three kinds and checks each pair. A tree with no such file passes. The values of each TOML text equal what `yaml.safe_load` gives for the YAML text. |
| `test_envfile_shell.py`, `test_env_upsert.sh` | A re-run never drops another key from a shared env file. |
| `test_envfile_vectors.py` | The command `creche-env` gives the result of each vector of `envfile.layer` and `envfile.raw`. `creche_env_check` and `creche_env_get` of `lib/envfile.sh` give it too, for each vector whose pair asks no account. Each function runs the command of its own tree, with its words, and keeps status 78. A `cd` after the source does not change that tree. |
| `test_envfile_catalog.py` | The catalog of the product: the example value of each required key is a value of its type. The two files name no IPv4 address outside 192.0.2.0/24 and 127.0.0.0/8. Each key of `root-keys.txt` is a catalog key. No key of `default.env` has another value than in an earlier version of the file on the base branch. A copy of `example.env` stops the command with status 78 and names each required key. |
| `test_gate_workflow.py`, `test_retest_workflow.py` | The two CI files hold to the same shard command, the same `proc` job, the same `suites` job, the same image build and the same `systemd-proof` job. `!retest` restarts one run. The `systemd-proof` job runs the script on the runner itself, with no container. A scope skips the proof only when the script answers `unchanged`. The last job of `gate.yml` is `gate`, and the last job of `release.yml` is `release`. Each other job name is in both files, but `plan`, `artifacts`, `disks` and `sign`: only the release has them. The test fails for another job name that only one file has. The `plan` job is pinned as a whole, with the whole text of its run step. It needs no job and sets no permission. The tests run that text against a stand-in dry run: the output `components` names each binary component of a `would attach` line, and a dry run that fails does not fail the step. The `artifacts` job is pinned as a whole: a checkout that keeps no token, uv with no cache, `build-artifacts.sh` with its three variables, and the upload. The `disks` job is pinned as a whole too: its one build step runs the build argv of `disks/component.toml` only when the plan names `disks`, and the tests run that text against a stand-in script. It has no rule, so the verdict needs its success in each scope. The `sign` job holds the identity token and no other permission, and it has no checkout and no local action. The test holds its steps and the whole text of its run step. The step compares each archive with the outputs of the `artifacts` and `disks` jobs before the first word `cosign`. After the verdict, the `release` job takes the three workflow artifacts and then cosign, with the text of the `sign` job. The cosign step has a time limit, and the job and the tag step have none. The tag step gets `ASSET_DIR` and `COSIGN`. The `sign` job names each asset with the two suffixes of the catalog and the file names of `artifacts.disk_files`, and the test imports them. The root disks of the workflow file are the flavors of `catalog.DISK_FLAVORS`. The cosign fetch runs for an archive or any one disk file, and for no bundle alone. The tests run the text of the two steps against a stand-in `curl`. The test also names each key at the top level of the two files. One more key there fails the test, for example `defaults` or `env`: such a key changes each job of its file. The one exception is the `env` key of the release. It holds the two constants of cosign and no other variable. The `suites` job has the setup of the `proc` job and one pytest run for each of the two old suites of `integration/`. Its last step fails for a test that skipped and for a suite that ran no test. |
| `test_playpen_release_disks.py` | `disks/bin/playpen-release-disks`, against stand-ins for `docker`, `curl` and `sha256sum`. The output is the four files of `artifacts.disk_files` and `SHA256SUMS`. `digests.json` has the keys, the file names and the byte form of `rust/crates/wire/vectors/digests.json`, and each sha256 is the digest of the decompressed bytes. The flavors are `catalog.DISK_FLAVORS` with the targets of `playpen-ci`. A failed step leaves `DISK_OUT` empty. |
| `test_systemd_proof.py` | `systemd-proof.sh`, against a fake systemd. Each case fails on a wrong result, and a value of `NRestarts` that is no count fails. The script removes its units after each failure. Only the line for an absent program passes a unit file. `--unchanged` says yes only for a change with no file of the proof. |
| `test_fleet_up.py` | `fleet-up` against a prefix for `/`, a fake `HOME` and binstubs. `check` on an empty host names each missing item and changes nothing. A second `up` changes no mode, time or byte. `down` removes only recorded paths, and a key that was there before `up` survives `down`. A record line outside the two roots refuses the whole record. The list of token files equals `attendance.auth.Principal`. `vmm.env` holds each key of `rust/crates/vmm/src/config.rs` but `RUNTIME_DIRECTORY`, with the value that `creche-env get` gives, so a missing key, an extra key or a wrong value fails (decision CH59 rule 1). A later `up` takes a new site value into `vmm.env`, and it writes the catalog value back when the site file drops it. |
| `test_handover_wrapper_owner.sh` | `creche-handover` refuses any of its three paths another account can write. |
| `test_unique_test_basenames.py` | No two test modules share a basename across the workspace. |
| `test_strictjson_copies.py` | Each copy of a copied module has the same bytes. A copied module is a file `strictjson.py` or `notices.py` at `<package>/src/<module>/`. A name with one copy passes, and a name with no copy passes. |
| `test_rfc3339_copies.py` | Each copy of `rfc3339.py` has the same bytes. A copy is a file `rfc3339.py` at `<package>/src/<module>/`. One copy passes. The test fails when the search does not find the copy of the noticeboard. The search and the comparison are the two functions of `test_strictjson_copies.py`. |
| `test_git_env_dropped.py` | A test run that git starts writes nothing into the repository of the caller. The root `conftest.py` drops the five variables that the hooks unset. |
| `test_git_config_dropped.py` | No `git` child of a test run reads the config file of a person or of the system. The same holds for the ignore file and the attributes file of a person. The root `conftest.py` sets the variables that do this. The registry writer of the noticeboard, `noticeboard/src/noticeboard/registrywrite.py`, starts `git` with `HOME` and with none of those variables. The root `conftest.py` gives each test under `noticeboard/tests` an empty home directory. That child then starts no program that a config file below `HOME` names, but it still reads the config file of the system. |
| `test_git_background_dropped.py` | No `git` command that inherits the environment of a test run starts the maintenance that `git` does not wait for. That holds for a commit, a merge, a fetch and a repository that receives a push. The root `conftest.py` sets the two settings that do this. A fixture that asks for the maintenance gets it in the foreground. |
| `test_ignored_signal_kept.py` | A test run that starts with SIGINT, SIGTERM or SIGHUP ignored passes the same tests, and the signal stays ignored. The fixture of the root `conftest.py` still fails a handler that a test leaves. |
| `test_creche_deploy.py`, `test_provision_library.py` | Each script, against binstubs and a temp root. |

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
