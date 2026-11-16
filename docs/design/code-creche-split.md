# Two attended coding families: `code` and `creche`

> Status as of 2026-11-14: Done. The families `code` and `creche` are live, and every pull request of the header merged.
> Superseded in part on 2026-11-15: the attended `code` family is deleted. Its non-platform coding reach moved to the autonomous `code-worker`, which `principal` starts with `enqueue` ([registry#90](https://github.com/la-creche/registry/pull/90)). The rule of §1 still holds: no family that writes a non-platform repository can name a platform one. `creche` is unchanged.
> Host names, addresses and accounts are placeholders. The operator's private notes hold the real values.

Date: 2026-10-30. Status: in progress. Merged 2026-10-31 03:27 UTC:
[private-docs#34](https://github.com/la-creche/private-docs/pull/34) (step 1)
and [registry#44](https://github.com/la-creche/registry/pull/44) (step 3).
Open: [private-docs#35](https://github.com/la-creche/private-docs/pull/35)
(`creche` alone, step 5), [registry#45](https://github.com/la-creche/registry/pull/45)
(github-platform text), [registry#46](https://github.com/la-creche/registry/pull/46)
(private-docs deny line).

**Host state at 03:30 UTC:** the registry change is applied. `code` is live
with no platform grant. `creche` is `invalid`: "'creche' is outside the
platform fence; only code may hold 'github-platform'", because
`/opt/private-docs/site.env` still says `code`. #34 was merged but not
deployed. Platform work through a family is blocked until §4 step 1 runs.
Merge #35 first, then one deploy covers both.

From your direction of 2026-10-30:

> `code` is my generic coding agent, that acts on all repos where it cannot
> widen its own permission. There should be a separate family `creche` that
> has full permissions on creche to edit whatever it needs to edit. Both are
> attended.

This replaces answer 1 of 2026-10-29 (`reframe/agent-control-removal-plan.md`:
one family `code` holds both). The name `agent-control` still leaves every
place. Step 7 of the cleanup runbook is unchanged.

## 1. The goal, as a rule

A repository is **platform** when a merge into it changes what a family may
do. A family that can change its own reach has no fence. So:

| Repository | Why it is platform | Who may change it |
|---|---|---|
| `la-creche/creche` | the family rules, caregiver, attendance, release tool | `creche` proposes, you merge |
| `la-creche/registry` | family files and server files become the chaperone policy, applied within a minute of a merge, with no tap | `creche` proposes, you merge |
| `la-creche/mcp` | the servers that run with the secrets | `creche` proposes, you merge |
| `la-creche/chaperone` | the proxy that decides every tool call | `creche` proposes, you merge |
| `la-creche/private-docs` | `/etc/creche/site.env` is a link into it, and that file holds the two fences (`AGENT_PLATFORM_FAMILIES`, `AGENT_MOUNT_ROOTS`) and the sops secrets | **you only.** Neither token reaches it |

Every other repository is `code`'s. Your rule of 2026-10-31: `code` holds no
mount and no permission that touches `la-creche` at all. So it does not mount
the corpus root `/srv/agents/code` (which holds the four platform clones) but
one clone per repository, read-only: today
`/srv/agents/code/home-assistant-analytics`. The mount list mirrors the
token's repository list: a repository joins both or neither
([registry#47](https://github.com/la-creche/registry/pull/47)). It holds no
GitHub credential that reaches a platform repository, no `release` verb, and
no platform board. Open point: `code` still delegates to `vault-oracle`, which
mounts the whole corpus and answers with paths.

Two decisions from before stay:

1. **D5.1.** No family holds `merge_pull_request` on `github-platform`. The
   validator refuses the grant (`PLATFORM_WITHHELD`). "Full permissions to
   edit" means every other verb: branch, write files, open and update pull
   requests, review, comment. You land each platform change, because a merge
   into `registry` applies with no tap.
2. **D5.3.** Each release waits for your phone tap.

## 2. The two tokens (github.com → Settings → Developer settings → Fine-grained tokens)

A fine-grained token has one resource owner. That is the strongest fence
here: a `<owner>` token cannot name a `la-creche` repository at all.

| | `github_token_code` (new) | `github_token_platform` (exists) |
|---|---|---|
| Resource owner | `<owner>` | `la-creche` |
| Repository access | Only select repositories | Only select repositories |
| Repositories | `home-assistant-analytics` to start. Add one when a task needs it | `creche`, `registry`, `mcp`, `chaperone`. Not `private-docs`, not `port-notes` |
| Contents | Read and write | Read and write |
| Pull requests | Read and write | Read and write |
| Issues | Read and write | Read and write |
| Metadata | Read (added by GitHub) | Read |
| Expiration | the longest offered. Write the date in private-docs beside the secret | check the date, write it down too |
| Held by | `code`, through `github-code` | `creche`, through `github-platform` |

Why "select" and not "all" for `code`: it holds every `github-code` verb,
`merge_pull_request` included. "All repositories" would let it merge into
`vps`, `.config`, `ansible-playbooks` and the other 50.

Why `home-assistant-analytics` first: it is the one non-platform repository the
host already clones under `/srv/agents/code`. `code` has made no successful
GitHub call outside the platform yet (audit: two `search_code` calls, both
`401`), so there is no history to copy.

`--lockdown-mode` refuses content from a repository the token cannot push to.
A repository absent from the list is **unreadable** through the server, not
read-only. The clones under `/srv/agents/code` are the read path for anything
else.

`la-creche/port-notes` is not platform, but a `<owner>` token cannot reach
it. If `code` needs it, move the repository to `<owner>` or leave it to
`creche`.

**Check (as root, on the host):** both print `200`.

```bash
sudo bash -c 'for f in github_token_code github_token_platform; do printf "%s " $f; curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $(tr -d "\n" < /etc/chaperone/secrets/$f)" https://api.github.com/user; done'
```

**Check the owner fence:** the code token answers `404` for a platform
repository and `200` for its own:

```bash
sudo bash -c 'T=$(tr -d "\n" < /etc/chaperone/secrets/github_token_code); for r in la-creche/creche <owner>/home-assistant-analytics; do printf "%s " $r; curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $T" https://api.github.com/repos/$r; done'
```

## 3. The family files (registry)

Both are attended, on `code-router`, with the same egress list as today's
`families/code/family.toml`. `creche` mounts `/srv/agents/code` and the vault,
read-only. `code` mounts one clone per repository and the vault, read-only
(registry#47). The other differences:

| Key | `families/code/family.toml` | `families/creche/family.toml` |
|---|---|---|
| `description` | Coding agent for every repository that is not the platform. | Platform family. Proposes changes to the four platform repositories and requests releases. |
| `tools.kagi` | the two tools | the two tools |
| `tools.github-code` | `"all"` | absent |
| `tools.github-platform` | absent | `"all"` |
| `tools.vikunja-code` | absent | `"all"` (the platform board, see §5) |
| `tools.vikunja-home-assistant` | `"all"` | absent |
| `tools.vikunja-networking` | `"all"` | absent |
| `verbs` | `{}` | `release.components` = the eight names |
| `delegates` | `["vault-oracle"]` | `["vault-oracle"]` |
| `shell`, `sandbox_tools`, `sandbox` | as today | as today |
| `skills` | as today | as today |

`families/creche/instructions.md`: today's `code` text minus the "every other
repository" parts, plus the release rules from the old `agent-control`
instructions. `families/code/instructions.md`: today's text with the platform
table kept (it reads the clones), the `github-platform` paragraph and the
release lines removed, and one line added: "If a task needs a change in a
platform repository, say so and stop."

The header comment of `mcp/github-platform/server.toml` and its `identity`
string say "Only the `code` family". They become `creche`. Text only.

Add `la-creche/private-docs` to the `arg_denies` list of
`mcp/github-code/server.toml`. The owner fence already covers it. The deny
list is layer two and should say it.

## 4. The order

The caregiver reads `AGENT_PLATFORM_FAMILIES` from `/etc/creche/site.env` at
start and refuses a family outside it that holds `github-platform` or
`release`. So the site key moves first, with both names during the window.

1. **Deploy the site key (as <user> on the host).** `/etc/creche/site.env` is a
   link into `/opt/private-docs`, which `creche-deploy` resets to the `main`
   of `/home/<user>/code/private-docs`. The deploy also refreshes `/opt/creche`
   from `/home/<user>/code/creche`.

   ```bash
   git -C /home/<user>/code/private-docs pull --ff-only
   sudo creche-deploy
   systemctl --user restart creche-caregiver.service
   ```

   Check: `grep -n '^AGENT_PLATFORM_FAMILIES' /etc/creche/site.env` prints the
   merged value, and within a minute `caregiver status` shows `creche`
   `in_sync (kind=attended)` and `code` `in_sync`.
2. **Tokens (github.com):** create `github_token_code` per §2. Verify the
   platform token's list is exactly the four. Write the new secret as root:

   Better, this prompts for the token and copies owner and mode from the
   platform file, which works today:

   ```bash
   sudo bash -c 'umask 077; read -rs -p "github_token_code: " T; echo; printf %s "$T" > /etc/chaperone/secrets/github_token_code.new && chown --reference=/etc/chaperone/secrets/github_token_platform /etc/chaperone/secrets/github_token_code.new && chmod --reference=/etc/chaperone/secrets/github_token_platform /etc/chaperone/secrets/github_token_code.new && mv /etc/chaperone/secrets/github_token_code.new /etc/chaperone/secrets/github_token_code'
   sudo systemctl restart creche-chaperone.service
   ```

   Then the `200` check of §2. Update `github_token_code` in private-docs
   `pep/secrets.enc.yaml` through `sops pep/secrets.enc.yaml` (the editor,
   not `sops set`: an argument lands in shell history). Commit and merge.
3. **registry PR (registry#44):** add `families/creche`, shrink
   `families/code`. You merge. `registry-sync` applies within a minute. Check:
   `caregiver status` shows `code` and `creche` `in_sync`. The two server-file
   edits of §3 (the `github-platform` text, the `private-docs` deny line) are
   two follow-up PRs: one idea per PR, and a server file change is a different
   row of the registry's merge table.
4. **Host, install.toml:** the release plugin still carries
   `"--requested-by", "code"`. Change it to `creche` and restart the chaperone,
   until the family-isolation chain removes the flag (creche#300 started
   that). Check: `grep requested-by /etc/chaperone/install.toml`.
5. **private-docs#35:** `AGENT_PLATFORM_FAMILIES=creche`. If it is merged
   before step 1 runs, step 1 deploys it and this step is done. Check:
   `caregiver status` shows both `in_sync`, and the caregiver journal has no
   `outside the platform fence`.
6. **Panda:** add the agent `creche` at `wss://<chat-domain>/acp/creche`.
   The first connection after a restart takes up to 20 s (two `sbx exec`).
7. **Checks through the door:**
   - Ask `creche`: "Use the github-platform tool `get_file_contents` to read
     `handover/component.yaml` from `la-creche/creche` at `main`." Audit line:
     `"app":"creche"`, `"tool":"github-platform__get_file_contents"`, `"event":"ran"`.
   - Ask `code`: "Use the github-code tool `get_file_contents` to read
     `README.md` from `<owner>/home-assistant-analytics` at `main`." Audit:
     `"app":"code"`, `github-code__get_file_contents`, `ran`.
   - Ask `code`: "Open a branch on `la-creche/creche`." Expect a refusal from
     the chaperone (`denied`), not a GitHub error.

Steps 1, 3 and 5 are pull requests you merge. Steps 2, 4 and the restarts are
root on the host. Nothing here needs a tap: no component releases.

## 5. Open decisions

1. **The platform board's names.** `vikunja-code`, the bot `bot-code`, the
   epic `code` and the worker `code-worker` all describe the platform work,
   renamed from `agent-control` on 2026-10-30. Under this split they belong to
   `creche`. Options: (a) grant `vikunja-code` to `creche` now and rename the
   four to `creche` later in one window (the step C procedure in
   `the host-cleanup-remaining.md` fits, with the names changed); (b) rename
   now, in the same registry PR as step 3. Recommendation: (a). The rename
   is a second `mcp-servers` release and a Vikunja window, and it blocks
   nothing.
2. **`port-notes`.** Stays out of both tokens, moves to `<owner>`, or goes to
   `creche`'s token. Recommendation: out of both until a task needs it.
3. **Merge.** Confirm D5.1 holds for `creche`: you merge every platform pull
   request. If you want `creche` to merge, that is a validator change in
   creche and a contract change, and the registry would then apply a family's
   own merge with no tap. Recommendation: keep D5.1.
