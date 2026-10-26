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
- Some code under test starts `git` with `HOME` and with no variable of
  git. The registry writer of the noticeboard,
  `noticeboard/src/noticeboard/registrywrite.py`, is such code. The root
  `conftest.py` gives each test under `noticeboard/tests` an empty home
  directory. That `git` child then finds no config file of a person below
  `HOME`. It still reads the config file of the system. Each other test
  keeps its `HOME`, because some tests give it to a real tool such as
  `cargo`.

## Hooks

`githooks/pre-commit` runs `bin/quality-gate.sh` with no flag: ruff, ruff
format and pyright.

`githooks/pre-push` runs the same checks, then tests:

| The push changes | The hook runs |
|---|---|
| a path under a package, for example `chaperone/app.py` | that package's suite, `--tests-for`. For a product package, also `vectors/tests` |
| a path in no package, for example `uv.lock` or `pyproject.toml` | the full suite. The hook passes `--tests-for`, and the gate runs every suite |
| a path under `vectors/`, for example `vectors/data/index.json` | `vectors/tests` |
| a path under `integration/proc/`, for example `integration/proc/proc_tree.py` | the process-level suite on four workers, and no other suite for that path. A test that skips is a failure, as in the `proc` job of CI. A Markdown file there, outside `tests/` and `fixtures/`, picks no suite |
| Markdown only, outside `tests/` and `fixtures/` | the tests marked `docs`, `--docs` |
| nothing, or a deleted branch | no test |

A package is the directory above a `testpaths` entry in the root
`pyproject.toml`. A new suite needs no change to the hook. A product package
is each package but `bin/` and `vectors/`.

No hook checks a commit message. Check the subject against the seven rules
in `AGENTS.md` yourself.

## CI

`.github/workflows/gate.yml` runs on every pull request and on every merge
group. It has nine kinds of job:

1. `lint`: ruff, ruff format, pyright. On a docs-only pull request it also
   runs the tests marked `docs`.
2. `tests`: the full Python suite, as four shards. A test's own id puts it in
   exactly one shard.
3. `playpen`: `pnpm test`, `pnpm run typecheck` and `pnpm run build`.
4. `agent`: `pnpm test` and `pnpm run typecheck` in `agent/`.
5. `facade`: `pnpm test` and `pnpm run typecheck` in `facade/`. Its
   `pnpm test` builds `dist/` first.
6. `proc`: the process-level suite, `uv run pytest integration/proc -m slow`.
   The job builds the playpen first. A test that skips is a failure there.
7. `suites`: the suite `integration/tests_manager`. The job builds the
   playpen first. It runs the suite with a pytest command of its own. A test
   that skips is a failure there. `integration/AGENTS.md` has its rules.
8. `systemd-proof`: `bin/systemd-proof.sh`, on the systemd of the runner.
   It proves that a unit with `RestartPreventExitStatus=78` stays stopped
   after exit status 78. It also gives each unit file to
   `systemd-analyze verify`. When the pull request changes no file of the
   proof, the job skips that step and passes. `bin/AGENTS.md` lists those
   files.
9. `gate`: red unless every other job passed. This is the one check the
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
  A change under a door directory, `door-acp/` included, moves `attendance`.
  A change to `uv.lock` moves every venv component.
- No job of `release.yml` builds or signs an archive. This repository has
  no binary component. The tag script refuses a binary component that has
  no archive, so such a component gets no tag.
- `chaperone` is a binary component of the chaperone repository. The
  `release.yml` of that repository builds, signs and tags it. Its manifest
  is at the root of that repository (`handover/AGENTS.md`).
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

A removal pull request removes one Python package from `main`. Two lines say
who does what:

- An agent creates a removal pull request.
- Only the owner of the repository merges a removal pull request.

The body of the pull request is the text of
`.github/PULL_REQUEST_TEMPLATE/remove-package.md`, with each line complete.

Create the pull request only when each of these three conditions holds:

1. The owner of the repository asked for the removal of the package.
2. In the component catalog, no row of a venv component names the directory
   of the package. This applies to the path of a row and to its bundles.
   `handover/src/handover/catalog.py` holds the catalog.
3. No `pyproject.toml` of another package names the package.

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
   the package. It removes each stand-in that only the package needed.
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
describes no defect.

The way back from a removal is a revert of the removal pull request.

### Known gaps

Two points of this section wait for the owner of the repository:

1. Who merges a removal pull request. One line of this section holds that
   value. The template does not hold it.
2. When a package is ready for its removal. No rule gives that point. The
   reading here is the most conservative one: an agent creates a removal
   pull request only when the owner asks for it. Condition 1 holds that
   reading.

## The docs rule

A change of nothing but Markdown runs only the tests marked `docs`. The one
copy of that rule is `bin/lib/docsrule.sh`. The pre-push hook, `gate.yml`
and `release.yml` all source it. A new test that reads a document carries the
`docs` marker, or a docs-only change skips it.

Every document is written in ASD-STE100 Simplified Technical English. Read
`docs/writing-standard.md` before you write one.
