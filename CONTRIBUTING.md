# Contributing

`AGENTS.md` holds the rules that apply to every edit. This file holds the
workflow: branches, hooks, CI, tags, releases and the removal of a Python
package.

## Branches and worktrees

- Use one linked worktree per task. Remove it with `git worktree remove`
  when its branch merges.
- Delete a remote branch when its pull request lands.
- Both hooks unset `GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`,
  `GIT_COMMON_DIR` and `GIT_OBJECT_DIRECTORY` before they run. A hook that
  runs from a linked worktree then does not leak those into the test suite's
  own `git` subprocesses.
- The root `conftest.py` removes the same five variables before pytest
  imports a test. git also sets them for `git rebase --exec` and for
  `git bisect run`. A test run from there then cannot write into the
  repository of the caller.
- The root `conftest.py` also sets `GIT_CONFIG_GLOBAL` to a file of its own
  and `GIT_CONFIG_NOSYSTEM` to `1`. A `git` child of a test then reads no
  config file of the person who runs the suite, and none of the system. A
  fixture that needs a setting sets it in its own repository or with
  `git -c`.
- The root `conftest.py` gives `core.excludesFile` and `core.attributesFile`
  an empty file, through `GIT_CONFIG_COUNT`. It sets `GIT_ATTR_NOSYSTEM` to
  `1`. A `git` child then reads no ignore file and no attributes file of
  that person, and no attributes file of the system. The two settings
  outrank the config of a repository. A fixture that needs one of the two
  passes it with `git -c`.
- The file of `GIT_CONFIG_GLOBAL` holds `maintenance.auto=false` and
  `gc.autoDetach=false`, and `GIT_CONFIG_COUNT` carries the same two
  settings. After a commit, a merge or a fetch, `git` starts its maintenance
  and does not wait for it. A repository that receives a push does the same.
  With `maintenance.auto=false` and `gc.autoDetach=false`, no `git` child
  that inherits the environment of the test run starts that process. A test
  can then remove or copy a throwaway repository right after a command.
- The settings `maintenance.auto` and `gc.autoDetach` of `GIT_CONFIG_COUNT`
  also outrank the config of a repository. A fixture that needs the
  maintenance passes `maintenance.auto=true` with `git -c`.
- A fixture that builds the full environment of `git` gets neither the file
  nor the settings of `GIT_CONFIG_COUNT`. Pass `-c maintenance.auto=false`
  in such a fixture. A fixture that names its own global config file gets
  only the settings of `GIT_CONFIG_COUNT`. `git` removes those settings,
  and each setting of `git -c`, from the environment of a repository that
  receives a push. When one of these two fixtures pushes, write
  `maintenance.auto=false` to the config of that repository.

## Hooks

`githooks/pre-commit` runs `bin/quality-gate.sh` with no flag: ruff, ruff
format and pyright. When the index or the work tree differs from `HEAD`
under `rust/`, it also runs `cargo fmt` and `cargo clippy`. It then runs
`cargo deny` where `cargo-deny` is on `PATH`.

`githooks/pre-push` runs the same checks, then tests:

| The push changes | The hook runs |
|---|---|
| a path under a package, for example `chaperone/app.py` | that package's suite, `--tests-for`. For a product package, also `vectors/tests` |
| a path in no package, for example `uv.lock` or `pyproject.toml` | the full suite. The hook passes `--tests-for`, and the gate runs every suite |
| a path under `vectors/`, for example `vectors/data/index.json` | `vectors/tests`, and the cargo steps when `cargo` is on `PATH` |
| a path under `rust/`, for example `rust/Cargo.lock` | `cargo fmt`, `cargo clippy`, `cargo deny` where `cargo-deny` is on `PATH`, and `cargo test`. No pytest suite for that path |
| a path under `integration/proc/`, for example `integration/proc/proc_tree.py` | the process-level suite on four workers, and no other suite for that path. A test that skips is a failure, as in the `proc` job of CI. A Markdown file there, outside `tests/` and `fixtures/`, picks no suite |
| Markdown only, outside `tests/` and `fixtures/` | the tests marked `docs`, `--docs` |
| nothing, or a deleted branch | no test |

A package is the directory above a `testpaths` entry in the root
`pyproject.toml`. A new suite needs no change to the hook. A product package
is each package but `bin/` and `vectors/`.

The cargo steps are `bin/rust-gate.sh`. The rule that starts them is
`bin/lib/rustrule.sh`.

- A commit or a push with no path under `rust/` needs no `cargo` on `PATH`.
  It starts no cargo step, with one exception.
- The exception is a push with a path under `vectors/`. The Rust tests read
  `vectors/data`, so that push starts the cargo steps when `cargo` is on
  `PATH`. Without `cargo`, the gate prints one line and passes.
- A commit or a push with a path under `rust/` needs `cargo`. Without it the
  gate fails before the first check.
- The commit that concludes a merge needs no `cargo` when only the other
  side changed `rust/`. An own change under `rust/` in that commit needs
  `cargo`.
- A push that changes Rust and Python runs the cargo steps and the Python
  suites.
- When the hook cannot read what a push changes, it runs the full suite and
  the cargo steps. That push needs `cargo`.
- The `cargo deny` step needs the program `cargo-deny`. rustup does not
  install it. Without it on `PATH`, the gate prints one line and runs each
  other step. In CI, the gate fails without it.
- The `cargo deny` step reads the advisory database from the network.
  `rust/AGENTS.md` has what the step checks.

No hook checks a commit message. Check the subject against the seven rules
in `AGENTS.md` yourself.

## CI

`.github/workflows/gate.yml` runs on every pull request and on every merge
group. It has ten kinds of job:

1. `lint`: ruff, ruff format, pyright. On a docs-only pull request it also
   runs the tests marked `docs`.
2. `tests`: the full Python suite, as four shards. A test's own id puts it in
   exactly one shard.
3. `playpen`: `pnpm test`, `pnpm run typecheck` and `pnpm run build`.
4. `proc`: the process-level suite, `uv run pytest integration/proc -m slow`.
   The job builds the playpen first. A test that skips is a failure there.
5. `proc-rust`: `bin/proc-rust.sh`, the process-level suite as the judge of
   a Rust program. For each file `rust/proc/*.run`, the script builds one
   program of the Cargo workspace. It then runs the scenarios that the file
   selects, with that program in the place of one service. A test that skips
   is a failure there. `rust/AGENTS.md` has the rules for a file.
6. `suites`: the two suites `integration/tests` and
   `integration/tests_manager`. The job builds the playpen first. It runs
   each suite with a pytest command of its own. A test that skips is a
   failure there. Only `gate.yml` has this job. `integration/AGENTS.md` has
   its rules.
7. `rust`: `bin/rust-gate.sh --tests`, with the toolchain that
   `rust/rust-toolchain.toml` names. The job installs `cargo-deny` before
   the script runs. When the pull request changes no path under `rust/` and
   no path under `vectors/`, the job skips those steps and passes. A change
   to the Rust checks themselves also runs the steps.
   `rust_gate_path` in `bin/lib/rustrule.sh` lists those files.
8. `rust-coverage`: `bin/rust-coverage.sh`, the coverage rule of the Rust
   workspace. The job installs `cargo-llvm-cov` before the script runs. It
   skips its steps for the same changes as the `rust` job. `rust/AGENTS.md`
   has the rule.
9. `systemd-proof`: `bin/systemd-proof.sh`, on the systemd of the runner.
   It proves that a unit with `RestartPreventExitStatus=78` stays stopped
   after exit status 78. It also gives each unit file to
   `systemd-analyze verify`. When the pull request changes no file of the
   proof, the job skips that step and passes. `bin/AGENTS.md` lists those
   files.
10. `gate`: red unless every other job passed. This is the one check the
    merge queue and the release executor read.

Comment `!retest` on a pull request to restart its CI on the same commit.
The comment is the command and nothing else. One run restarts per comment.
None restarts while a run is still going.

A green local push is a fast catch, not proof. CI runs on Linux and is the
merge gate.

## Tags and releases

`.github/workflows/release.yml` runs on every push to `main`. It runs the
gate again, then tags every component whose paths changed since that
component's own newest tag.

- A tag is `<component>-vX.Y.Z`. No file holds a version.
- The level is `patch`. A `bump:minor` or `bump:major` label on the pull
  request raises it for the components that pull request changed.
- A component's paths include every workspace package its build installs. A
  change under `family/` moves `attendance`, `caregiver` and `noticeboard`.
  A change to `uv.lock` moves every venv component.
- A change under `rust/` moves no component today, because no component is a
  binary component. A change to `rust/Cargo.lock`, `rust/Cargo.toml` or
  `rust/rust-toolchain.toml` moves every binary component and no other kind.
- Prose moves no tag. A `.md` file outside `tests/` and `fixtures/` is
  prose. A merge that changes only `playpen/AGENTS.md` makes no tag. The next
  code change to `playpen/` makes one tag, and that tag covers both merges.
- A new tag asks the operator for a release. `creche-follow.timer` on the
  host files the request for each component that has had a release.
- Only CI mints a Release. The release executor refuses a Release that
  `github-actions[bot]` did not author. Do not push a tag by hand.
- A re-run on the same commit is a no-op. A tag is never moved or deleted.

Read the plan before a merge, from any checkout:

```bash
GITHUB_REPOSITORY=<owner>/agent-control GITHUB_SHA=$(git rev-parse HEAD) handover/bin/allocate-tags.sh --dry-run
```

One release per merged change. A request names the components to move. Root
builds and deploys them, and each component's verify hook runs. A release
that stacks unrelated changes enlarges the blast radius of a bad one.

## Removal of a Python package

In the port to Rust, a Rust binary takes the place of a Python package. The
Python package stays on `main` until each component whose build installed
it is proven. An agent then creates one pull request that removes the
package.

Reason: a revert is the way back from a cutover release. That revert needs
the Python package on `main`.

| Word | Meaning |
|---|---|
| cutover release | The release that moves a component from its Python package to its Rust binary. `rust/AGENTS.md` has its rules. |
| cutover tag | The tag of the cutover release. |
| cutover commit | The commit of `main` that the cutover tag names. |
| proof time | The time after the cutover release in which the Rust binary must run with no fault. |
| removal pull request | The pull request that removes one Python package. |
| evidence | The data that shows that each check holds for one component. |

### When a component is proven

Six checks decide if a component is proven. Each check must hold:

1. **CI.** The `proc` job and the `rust` job of `release.yml` passed at the
   cutover commit. In that run, the default command of each service of the
   component started the Rust binary. `integration/proc/AGENTS.md` has the
   service table.
2. **The host.** The ledger entry of the cutover release has the status
   `succeeded`. Each `verify` row of the component in that entry says `ok`.
   A release with the status `restored` does not count.
3. **Time.** The proof time is complete.
4. **No fault.** In the proof time, each of these three facts holds:
   - No release of the component has the status `restored` or `failed`.
   - systemd restarted no unit of the component by itself.
   - No unit of the component had an exit status other than 0.
5. **Use.** In the proof time, the component did its usual work on the
   host. A component that had no work in that time is not proven.
6. **Defects.** No defect of the Rust version of the component is open with
   the severity high or medium.

Two lines give the proof time:

- The proof time starts when the cutover release succeeds.
- The proof time is 7 days of 24 hours each.

When one check does not hold, the component is not proven. No rule gives the
start of a new proof time. The owner of the repository decides it.

### The removal pull request

One removal pull request removes one Python package. Two lines say who
does what:

- An agent creates a removal pull request.
- Only the owner of the repository merges a removal pull request.

The body of the pull request is the text of
`.github/PULL_REQUEST_TEMPLATE/remove-package.md`, with each line complete.

Create the pull request only when each of these three conditions holds:

1. One component or more installed the package in its Python build. Each of
   those components is proven.
2. In the component catalog, no row of a venv component names the directory
   of the package. This applies to the path of a row and to its bundles. The
   row of a binary component can keep that directory as its path.
   `handover/src/handover/catalog.py` holds the catalog.
3. No `pyproject.toml` of another package names the package.

A package that no component installed in its Python build has no cutover
release. The six checks thus cannot prove it. Do not create its removal pull
request until the owner of the repository gives its checks.

The pull request makes these six changes:

1. It removes three things of the package: its source, its tests and its
   `pyproject.toml`.
2. It removes the entries of the package from the root `pyproject.toml` and
   from `pyrightconfig.json`. The root `pyproject.toml` also holds the ruff
   config.
3. It writes `uv.lock` again with `uv lock`.
4. In each module under `vectors/surfaces/`, it removes each surface whose
   entry point is in the package. It then removes each module there that no
   surface needs. It keeps each file under `vectors/data`. It freezes each
   data file that has no generator after the change. `vectors/README.md`,
   "A frozen file", has the steps. The index then holds the SHA-256 of each
   frozen file, and a test fails for an edit that a person made by hand.
5. Outside the directory of the package, it removes each test that imports
   the package. It removes each stand-in that only the Python service
   needed.
6. It changes or removes each line of a rule file that names a file of the
   package. It does the same for each comment of a unit file under
   `systemd/`. A rule file is an `AGENTS.md` file or this file.

A surface or a test can also check a product package that stays on `main`.
"Hooks" defines a product package. Change 4 and change 5 thus have one stop
case each:

1. Change 4: a surface has its entry point in a product package that stays.
   That surface also uses the package.
2. Change 5: a test also imports a product package that stays.

For a stop case, do not remove the surface or the test. Do not create the
pull request. Tell the owner of the repository, who decides the case. The
body of the pull request then names each stop case and its decision.

The merge of a removal pull request can mint tags. For example, a change
to `uv.lock` moves every venv component. Each new tag asks the operator for
a release. The body of the pull request thus shows what the merge starts:

1. Run the tag allocator with `--dry-run` on the head of the branch. "Tags
   and releases" has the command.
2. Put the output in the body of the pull request. The output lists each
   tag that the merge can mint.
3. Below the output, name each release that the merge starts.

This repository is public, and each pull request is public too. The
sanitization rule of `AGENTS.md` applies to the text of a removal pull
request. That text names no host and no family of a deployment. It
describes no defect, and it holds no evidence.

Keep the evidence in a private place. The template has one table row for
each component. On that row, give only a link to the evidence, for the
owner. The link names no host, no user and no domain. A file name is
sufficient.

The way back from a removal is a revert of the removal pull request.

### Known gaps

Four points of this section wait for the owner of the repository:

1. The length of the proof time. One line of this section holds that value.
2. Who merges a removal pull request. One line of this section holds that
   value.
3. The start of a new proof time after a check that does not hold.
4. The checks for a package that no component installed in its Python
   build.

The template holds neither of the two values.

## The docs rule

A change of nothing but Markdown runs only the tests marked `docs`. The one
copy of that rule is `bin/lib/docsrule.sh`. The pre-push hook, `gate.yml`
and `release.yml` all source it. A new test that reads a document carries the
`docs` marker, or a docs-only change skips it.

Every document is written in ASD-STE100 Simplified Technical English. Read
`docs/writing-standard.md` before you write one.
