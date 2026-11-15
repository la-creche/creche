# Plan: one `code` family, `coding-sandbox`, and no `agent-control`

> Status as of 2026-11-14: Done. The name `agent-control` left the repositories and the host, and the `agent-control` repository is archived. The plan below is the record. A later decision replaced answer 1 (see `code-creche-split.md`).
> Host names, addresses and accounts are placeholders. The operator's private notes hold the real values.

Date: 2026-10-29. Status: proposal. Your answers of 2026-10-29:

1. `agent-control` and `code` become one attended family, `code`. They hold the same permissions.
2. `code-sandbox` stays `chat`'s thin code runner. Its new name is `coding-sandbox`, so it does not read as a part of `code`.
3. The name `agent-control` leaves every place that uses it.

## What the host and the repositories hold today

| Where | Uses of `agent-control` |
|---|---|
| creche | 117 files, 1193 lines. Most are test fixtures (`example-owner/agent-control`) and docs. The ones that act: `handover`'s catalog value `Repo.AGENT_CONTROL` (the `repo` field of every manifest), `family`'s `PLATFORM_ALLOWLIST` and `PLATFORM_MIRRORS`, the env keys `AGENT_GITHUB_REPO_AGENT_CONTROL` |
| registry | 21 files: the families `agent-control` and `agent-control-worker`, the server `vikunja-agent-control`, the pin `AGENT_CONTROL_FAMILY_REF` |
| mcp | 18 files: `AGENT_CONTROL_REF` and the checkout `.agent-control` in its workflows |
| chaperone | 3 files of text |
| private-docs | 44 files: the site keys, `/opt/agent-control` in old scripts, docs |
| The host | `install.toml` (`--requested-by agent-control`, the plugin `vikunja-agent-control`), `policy.toml`, two plugin manifests, the account `mcp-vikunja-agent-control`, `/opt/mcp/vikunja-agent-control`, the clone `/srv/agents/code/agent-control`, `/home/<user>/code/agent-control` (the deploy origin), `code-repos.txt`, the state of the families `agent-control` and `agent-control-worker` |

## What changes in the design

The platform fence of spec §4.4 has four layers today: the two GitHub tokens, the chaperone's repository lists, the family rules in code, and the sandbox. After the merge, one family holds both GitHub servers, the whole corpus read-only, the vault read-only, and `release`. Two layers stay:

1. **D5.1:** no family holds `merge_pull_request` on `github-platform`. You merge each platform pull request.
2. **D5.3:** each release waits for your tap.

The family now reads repositories it does not own through `github-code`. `--lockdown-mode` refuses content from repositories that the token cannot write, which limits text from strangers.

## Kept on purpose

1. The deny lists of `github-code` keep the old names (`la-creche/agent-control-archive`, `<owner>/agent-control`, …). GitHub redirects an old name, so dropping one opens a way around the deny.
2. `docs/archive/`, `docs/rework/` and git history.
3. Names in Vikunja itself (the project, the bot user `bot-agent-control`). You change them in Vikunja. The plan renames what the repositories and the host call them.

## The phases

Each phase ends with its check. A phase may wait for an earlier one.

### Phase 1. The single-file read (independent)

1. la-creche/chaperone#68: an embedded text resource passes as text. **Open.**
2. A chaperone PR: version 0.3.1. The merge cuts the release.
3. Host: one tap, then restart the chaperone.

Check: `github-platform__get_file_contents` shows `ran` in the audit.

### Phase 2. The family rules in code (creche, `family`)

[la-creche/creche#287](https://github.com/la-creche/creche/pull/287), open. One PR:

1. `PLATFORM_ALLOWLIST = {"code"}`. `code` may hold `github-platform`, `release` and the platform clones.
2. The rule "a family that edits the platform holds no other mount" goes. The platform family may mount any allowed root.
3. `PLATFORM_MIRRORS` goes: `code` mounts the corpus root `/srv/agents/code` read-only, which holds the four clones.
4. The allowed root `/srv/agents/work/platform` goes, so no family can mount a working copy again.
5. The allowed root `/srv/agents/work/code-sandbox` becomes `/srv/agents/work/coding-sandbox`.

The merge mints `caregiver` and `noticeboard` tags. **Do not tap them before Phase 3 is ready.** The new caregiver refuses the old family files, and the old caregiver refuses the new ones.

### Phase 3a. The registry (one PR, applied right after the Phase 2 release)

[la-creche/registry#39](https://github.com/la-creche/registry/pull/39), draft. It does items 1 to 4 and 6 below. Item 5, the Vikunja server, moves to Phase 3b: a registry rename before the host has the new upstream makes `chaperone check` refuse the policy. Two changes from the first draft of this plan: `code` mounts no read-write root at all (a change goes into a scratch clone under `/tmp`), and it keeps the base image (it has `python3` and `git`).

**Your step in Vikunja, before the merge:** rename the epic `agent-control` to `code`. `scrum-lead` routes a ticket to `<epic>-worker`, and the worker is now `code-worker`.


1. `families/code`: the union of `agent-control` and `code`. It mounts `/srv/agents/code` read-only, `/srv/agents/vault` read-only, and nothing read-write. It holds `github-platform`, `github-code`, `kagi`, the three Vikunja servers, `release` and `bash`.
2. `families/agent-control` goes.
3. `families/code-sandbox` becomes `families/coding-sandbox`, at `/srv/agents/work/coding-sandbox`. `chat` delegates to it and mounts that root.
4. `families/agent-control-worker` becomes `families/code-worker`.
5. (Phase 3b) `mcp/vikunja-agent-control` becomes `mcp/vikunja-code`.
6. `validate.yml`: `AGENT_CONTROL_FAMILY_REF` becomes `CRECHE_FAMILY_REF`, at the Phase 2 commit.

Host, one window for 3a:

1. Tap the caregiver release of Phase 2, then merge the Phase 3a PR at once. `registry-sync` applies it within one minute.
2. `mv /srv/agents/work/code-sandbox /srv/agents/work/coding-sandbox`.
3. `install.toml`: `--requested-by code`. Restart the chaperone.
4. Remove the state of the old families: `caregiver delete` for `agent-control`, `agent-control-worker` and `code-sandbox`.

Check: `caregiver status` shows `code`, `code-worker` and `coding-sandbox` `in_sync`, and none of the three old names.

### Phase 3b. The Vikunja server name

A registry PR renames `mcp/vikunja-agent-control` to `mcp/vikunja-code`, and `board-lead`'s `VIKUNJA_EPICS` says `code`. Host, in one window:

1. `install.toml`: the plugin `vikunja-code`, the secret `vikunja_token_code`.
2. Rename the secret file, the plugin manifest directory, and the account (`usermod -l mcp-vikunja-code mcp-vikunja-agent-control`).
3. An `mcp-servers` release builds `/opt/mcp/vikunja-code`. Then restart the chaperone.

Check: the audit shows a `vikunja-code__` call `ran`.

### Phase 4. The repository names (the old step 5)

This is the hard one. `handover`'s catalog value `agent-control` is the `repo` field of every component manifest. A release is executed by the `handover` that is installed, and an old `handover` refuses a manifest with a `repo` value it does not know (as in runbook A3 on 2026-10-26). So the first `handover` with the new value cannot be released by the old one in the usual way.

Proposal: a `handover` PR that renames `Repo.AGENT_CONTROL`, `AGENT_MCP` and `AGENT_REGISTRY` to `CRECHE`, `MCP` and `REGISTRY`, with the values `creche`, `mcp` and `registry`. Root installs that one release by hand, from the tag, as for the first `handover` (runbook A3). Then every manifest, the site keys (`AGENT_GITHUB_REPO_CRECHE`, …), the clones (`/srv/agents/code/creche`, `mcp`, `registry`), `code-repos.txt`, `/home/<user>/code/creche` and the origin of `/opt/creche` move in one window.

This needs a design note before code, because it changes how root's release tool is installed once. The note is `reframe/phase4-repo-names-design.md`. It proposes two releases (the catalog first, the manifests second), so that no hand install is needed.

### Phase 5. The text

Mechanical PRs in each repository: test fixtures, docs, comments, workflow names (`AGENT_CONTROL_REF` in mcp). No release, except where a component's own files change.

Check: `git grep -I agent-control` over the five repositories, without `docs/archive`, `docs/rework` and the deny lists, prints nothing.

## Needs your answer before Phase 4

1. Root installs the first `handover` with the new repository names by hand (proposal above), or another way.

The other phases follow from your answers above. Phase 1 is open. Phase 2 can start now.
