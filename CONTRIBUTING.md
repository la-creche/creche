# Contributing

`AGENTS.md` holds the rules that apply to every edit. This file holds the
workflow: branches, hooks, CI, tags and releases.

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
under `rust/`, it also runs `cargo fmt` and `cargo clippy`.

`githooks/pre-push` runs the same checks, then tests:

| The push changes | The hook runs |
|---|---|
| a path under a package, for example `chaperone/app.py` | that package's suite, `--tests-for`. For a product package, also `vectors/tests` |
| a path in no package, for example `uv.lock` or `pyproject.toml` | the full suite. The hook passes `--tests-for`, and the gate runs every suite |
| a path under `vectors/`, for example `vectors/data/index.json` | `vectors/tests`, and the cargo steps when `cargo` is on `PATH` |
| a path under `rust/`, for example `rust/Cargo.lock` | `cargo fmt`, `cargo clippy` and `cargo test`, and no pytest suite for that path |
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

No hook checks a commit message. Check the subject against the seven rules
in `AGENTS.md` yourself.

## CI

`.github/workflows/gate.yml` runs on every pull request and on every merge
group. It has six kinds of job:

1. `lint`: ruff, ruff format, pyright. On a docs-only pull request it also
   runs the tests marked `docs`.
2. `tests`: the full Python suite, as four shards. A test's own id puts it in
   exactly one shard.
3. `playpen`: `pnpm test`, `pnpm run typecheck` and `pnpm run build`.
4. `proc`: the process-level suite, `uv run pytest integration/proc -m slow`.
   The job builds the playpen first. A test that skips is a failure there.
5. `rust`: `bin/rust-gate.sh --tests`, with the toolchain that
   `rust/rust-toolchain.toml` names. When the pull request changes no path
   under `rust/` and no path under `vectors/`, the job skips those steps and
   passes. A change to the Rust checks themselves also runs the steps.
   `rust_gate_path` in `bin/lib/rustrule.sh` lists those files.
6. `gate`: red unless every other job passed. This is the one check the
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

## The docs rule

A change of nothing but Markdown runs only the tests marked `docs`. The one
copy of that rule is `bin/lib/docsrule.sh`. The pre-push hook, `gate.yml`
and `release.yml` all source it. A new test that reads a document carries the
`docs` marker, or a docs-only change skips it.

Every document is written in ASD-STE100 Simplified Technical English. Read
`docs/writing-standard.md` before you write one.
