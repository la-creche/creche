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

## Hooks

`githooks/pre-commit` runs `bin/quality-gate.sh` with no flag: ruff, ruff
format and pyright.

`githooks/pre-push` runs the same checks, then tests:

| The push changes | The hook runs |
|---|---|
| a path under a package, for example `chaperone/app.py` | that package's suite, `--tests-for` |
| a path in no package, for example `uv.lock` or `pyproject.toml` | the full suite, `--tests` |
| Markdown only, outside `tests/` and `fixtures/` | the tests marked `docs`, `--docs` |
| nothing, or a deleted branch | no test |

A package is the directory above a `testpaths` entry in the root
`pyproject.toml`. A new suite needs no change to the hook.

No hook checks a commit message. Check the subject against the seven rules
in `AGENTS.md` yourself.

## CI

`.github/workflows/gate.yml` runs on every pull request and on every merge
group. It has four kinds of job:

1. `lint`: ruff, ruff format, pyright. On a docs-only pull request it also
   runs the tests marked `docs`.
2. `tests`: the full Python suite, as four shards. A test's own id puts it in
   exactly one shard.
3. `playpen`: `pnpm test`, `pnpm run typecheck` and `pnpm run build`.
4. `gate`: red unless every other job passed. This is the one check the
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
  A change to `uv.lock` moves every venv component. A change to
  `rust/Cargo.lock`, `rust/Cargo.toml` or `rust/rust-toolchain.toml` moves
  every binary component and no other kind.
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
