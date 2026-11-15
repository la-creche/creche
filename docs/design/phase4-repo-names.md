# Phase 4: the repository names, without a hand install

> Status as of 2026-11-14: Done. The repository names changed in the clean cut of decision D6.1 (`docs/decisions/consolidation.md`).
> Host names, addresses and accounts are placeholders. The operator's private notes hold the real values.

Date: 2026-10-29. Status: decided 2026-10-29: two releases, with a one-release alias (see the correction below). Part of `reframe/agent-control-removal-plan.md`.

## What a `Repo` value does today

`handover/src/handover/catalog.py` holds `class Repo`: `agent-control`, `agent-mcp`, `agent-registry`, `chaperone`. One value does three jobs:

1. **The catalog key.** Each component manifest names its repository in `repo`. The executor refuses a manifest whose `repo` is not a `Repo` value.
2. **The corpus directory.** root reads the source of a release from `/srv/agents/code/<value>` (`handover/src/handover/executor/source.py`, `CORPUS_ROOT`).
3. **The site key.** `AGENT_GITHUB_REPO_<VALUE>` gives the GitHub name (`handover/src/handover/site.py`). The host sets `AGENT_GITHUB_REPO_AGENT_CONTROL=creche`, `…_AGENT_MCP=mcp`, `…_AGENT_REGISTRY=registry`.

The new values are `creche`, `mcp`, `registry`. `chaperone` stays.

## The problem

The installed `handover` executes each release, the release of `handover` included. An executor refuses a manifest with a `repo` value it does not know (runbook A3, 2026-10-26). So the old executor cannot release a `handover` whose own manifest says `repo = "creche"`.

## The proposal: two releases, catalog first

1. **PR A (creche, `handover` only).** `Repo` takes the new values. Every manifest keeps its old `repo` text, `handover/component.yaml` included.
   - The old executor reads `handover`'s manifest with the old value, so it accepts it, and releases the new `handover`.
   - The new `handover` knows the new values only. Every manifest on `main` still holds an old value, so the new `handover` refuses every release until step 3. That gap is accepted: a clean cut.
2. **The host, in one window:**
   1. `mv` the three clones: `/srv/agents/code/agent-control` → `creche`, `agent-mcp` → `mcp`, `agent-registry` → `registry`.
   2. `code-repos.txt` drops the second word of those three lines.
   3. The site file drops the three `AGENT_GITHUB_REPO_*` keys. The default of a site key is the value itself, which is now the GitHub name (a `private-docs` PR).
   4. `mv /home/<user>/code/agent-control /home/<user>/code/creche`, and set the origin of `/opt/creche` to it (root).
3. **PR B (creche, mcp, registry, merged in the window).** Each manifest says the new value: every `component.yaml` in creche, `mcp`'s manifest, and `registry`'s `component.yaml`. The env catalog drops `AGENT_GITHUB_REPO_AGENT_*`. Under "another value needs another key", those keys go and no new ones come. `bin/fleet-up` and the runbook name the new `--root` paths. The family `code` keeps its mount of `/srv/agents/code`.
4. **The first release by the new `handover`:** the set of every component whose manifest changed. One tap.

PR A is small and does not change how `handover` runs. Only the names of four enum values change. PR B is mechanical.

## What else reads a `Repo` value

1. `handover/bin/allocate-tags.sh` takes `TAG_REPO`, which defaults to the name part of `GITHUB_REPOSITORY`. Today that default is `creche`, which the planner refuses (a dry run here printed `invalid choice: 'creche'`), so creche's `release.yml` sets the value. After PR A, the default is right.
2. `mcp`'s workflows check creche out as `.agent-control` and pin `AGENT_CONTROL_REF`. Phase 5 renames them. They read no `Repo` value.
3. The deny list of `github-code` keeps the old GitHub names on purpose.

## Risks

1. Between the release of PR A and the end of the window, no component can be released. Keep the window short. Do not start it while a fix waits.
2. If the release of PR A fails, the old `handover` stays. Nothing else changed, so stop there.
3. If the first release of step 4 fails, its restore uses the new `handover`. The old manifests are gone from `main`, so a rollback of `handover` itself means the `.prev` tree and a hand step (runbook E).

## Your decision

1. Two releases, catalog first, as above, or root installs the new `handover` by hand from its tag.

## Correction (2026-10-29)

The first version of this note said release A changes the catalog alone. It cannot: the repository's own tests read every in-tree manifest through the catalog, so CI fails and `release.yml` mints no tag. Release A therefore also maps the three old names to the new values, for one release (`catalog.OLD_REPO_NAMES`). Release B removes the map. The PRs: la-creche/creche#288 (A), la-creche/creche#289, la-creche/mcp#87, la-creche/registry#41 and la-creche/private-docs#32 (B). The host steps are runbook step 11.

## What happened (2026-10-30)

creche#289 had creche#288's branch as its base. Its merge put release B into that branch, and #288 then merged A and B together as `a4b8ba2e`. Handover `0.1.45` cannot read a manifest that says `repo: creche`, so it can release nothing from `main`. The operator chose the hand install: root installs handover `0.1.46` from its tag (runbook step 11.2), then the window goes on. The restore pull request, la-creche/creche#290, is closed. Lesson: a pull request that must land after another one takes `main` as its base, never the other pull request's branch.
