# The release path on Forgejo: design

> Status as of 2026-11-16: The operator decided FR1 to FR4 on 2026-11-15, all as recommended, and FR16 on 2026-11-16. This note proposes FR5 to FR15. Nothing is built. After FR16, the revision of 2026-11-16 proposes a signer that attaches to the generic runner from outside (FR5, FR6). Next: the open questions of section 10, then PR 2 of section 8. On 2026-11-16 most Forgejo replies that this note marked "not measured" were measured on a scratch repository. Four of them change the design: the secrets guards (M18), check 2 of the signer (section 4.4), the label read of the allocator (M11) and the size of the tag message (section 4.5, a proposal). A line dated 2026-11-16 marks each change. Also on 2026-11-16, the builds of PR 2 and PR 4 of section 8 found two corrections: the control lines of the attachment are version 2 of the runner design (sections 4.3 and 4.5), and section 8 proposes a split of PR 4.
> Host names, addresses and accounts are placeholders. The operator's private notes hold the real values.

Source: the operator's private migration plan (section 0c, the Actions facts measured on Forgejo 17.0.1 with runner v13.2.1, and the verification appendix), its raw inventory of GitHub dependencies, and the code of the five repositories. File evidence was checked at `origin/main` on 2026-11-15: creche `96fab11c`, chaperone `a3dd242`, mcp `dadfa2b`, registry `7673e0f`, creche-release `9f0ac54`. A "measured" fact comes from section 0c of the plan. A fact "measured on a scratch repository" on 2026-11-16 comes from the recorded replies of Forgejo 17.0.1 in the operator's private notes. Any other fact about Forgejo is marked "not measured".

"The runner design" in this note is `docs/design.md` of the repository `ci-runner` in the organization `infra` on the Forgejo instance, read at commit `7cdaa18` of its branch `import/standalone` on 2026-11-16. This note cites it by section title, not by section number.

Changed on 2026-11-16: the attachment of the signer follows a newer runner design, at commit `3b9ce23` of its branch `spike/privilege-separation`. Its section 6 "Attachments of outside services" and its section 10 "Privilege separation" replace the attach text of `7cdaa18`. This note cites those two sections by number. As of 2026-11-16, that runner design is not yet implemented. Its mechanisms S1 to S13 (section 10.8 "Spike before the code") are not yet measured on a host.

## 1. Goal and non-goals

The repositories move from GitHub to a self-hosted Forgejo 17.0.1. Wave 4 of the move is the five platform repositories: creche, mcp, chaperone, registry and creche-release. The release path of each one runs through creche code. This note designs that path on Forgejo.

Goal:

1. A merge to `main` of each of the five repositories tags and releases on Forgejo, as it does on GitHub today.
2. Root's executor on the host verifies each release against Forgejo, with checks P1 to P6.
3. The signing key of FR1 lives on the host and never in Forgejo or in a job VM. Only a job of a push to the protected `main`, of a named release workflow, gets a signature.
4. The five repositories move in one frozen window behind a bridge release (FR4).

Non-goals:

1. The repositories of the code family. They moved in wave 2 and have no release path.
2. Panda. It waits for the runner VMs of the runner design (wave 3).
3. The agent MCP servers and their fences (the `forgejo-platform` server, `git-platform`, the bot `platform-bot`). They are a separate track.
4. The runner itself. The runner design covers it. This note adds nothing to the runner. The signer uses the runner's generic attachment interface, through a row of the runner's policy file (section 4.4). Question 9 asks who changes that file.
5. A public transparency log for the signatures (FR12).

## 2. Decisions

The operator decided FR1 to FR4 on 2026-11-15 and FR16 on 2026-11-16:

| # | Topic | Decision | The operator's words | Date |
| - | ----- | -------- | -------------------- | ---- |
| FR1 | Signing | Cosign key-pair signing replaces keyless Sigstore. The host verifies with the public key. | "as recommended" | 2026-11-15 |
| FR2 | Trust anchor of P1 | The signing key is the only trust anchor of P1. No author check. | "as recommended" | 2026-11-15 |
| FR3 | Runners | CI runs in throwaway Cloud Hypervisor VMs on the host. The runner is generic infrastructure of the Forgejo instance (the runner design), and creche is one of its users. The daemon runs as the system user `ci-runner` with a system unit. | "as recommended" | 2026-11-15 |
| FR4 | Cutover | The five repositories move in one frozen window behind a bridge release. The bridge is a release shipped from GitHub whose executor already speaks Forgejo and holds the signing public key. The current executor verifies the bridge. | "as recommended" | 2026-11-15 |
| FR16 | The runner and creche | The runner is independent of creche. It holds no signing logic and no creche concept. | "ci-runner needs to be generic enough that it can be used by any repo for any PR in any CI/CD pipeline" | 2026-11-16 |

On 2026-11-16 the wording of FR3 changed. The first wording made the runner a part of creche. FR16 rejects that coupling. The decision of FR3 itself does not change.

This note proposes these rows. The operator answered the open questions of the first draft on 2026-11-15 (section 9). Section 10 holds the questions that FR16 opens. The rows become decided when the operator merges this note.

| # | Topic | Proposal | Why |
| - | ----- | -------- | --- |
| FR5 | The signer | A separate program `ci-signer` holds the private key. It is a creche service that attaches to the VM of a release job from outside, through the runner's policy file (FR6). By the default of question 8, it is a creche component of its own. It runs as its own system user `ci-signer`, in its own unit `creche-ci-signer.service`. It ships through creche's own release path, not inside the runner. | The runner holds no signing logic (FR16). Before D9, each CH process ran as `ci-runner`. Under D9, each VM runs as a dynamic user (the runner design, section 10). A CH escape must not reach the key in either case. |
| FR6 | The channel | An `[[attach]]` row of the runner's policy file matches the release job of each repository of section 4.3. The runner passes each guest connection to one vsock port of that job's VM to the socket of the signer, as a file descriptor (the runner design, section 6.2). The runner gives that path only to a VM that a row matches. That limit is runner behaviour, and it holds only while the policy file is right and the user `ci-runner` is not compromised (section 4.10). The path decides nothing. The checks of the signer in section 4.4 decide which run is eligible. | This is the generic interface of the runner design, section 6 "Attachments of outside services". A vsock device is no network card. The fence of the runner design, section "The network fence", does not change. |
| FR7 | What is signed | One release statement per run. The signer writes the repository, the workflow file, the event, the ref, the commit and the run id from its own read of Forgejo. The job gives only the tags and the SHA-256 of each asset. | `verify-blob --key` alone binds no workflow, ref or commit. Keyless signing bound all three (`artifacts.py:332-345`). The statement puts that binding back. |
| FR8 | Where the statement travels | In the message of the annotated tag, with its bundle. The signer creates each release tag through the API with the token of the Forgejo bot `ci-signer`, the only user allowed on the tag protection of `*-v*` (section 6). The per-asset bundles `*.sigstore.json` retire. | A tag never moves. Root already reads the tag object for P2 (`provenance.py:248-257`). The follower fetches nothing new. |
| FR9 | P1 for every kind | Each Release of a component with `Releases.YES` carries a statement, also a `venv` and a `units` component. mcp and creche-release join `catalog.RELEASE_WORKFLOW`. | FR2. Without a statement, P1 of a `venv` component has no anchor at all. |
| FR10 | The public key on the host | A root-owned file `/etc/creche/release-signing.pub`, outside every release. The executor refuses P1 without it. A copy in creche serves the self-check of the allocator in CI. | The thing that deploys code is not deployed by code (`handover/AGENTS.md`, executor rule 8). A rotation then needs no release. See question 2. |
| FR11 | Reach to Forgejo | Root and the follower reach the Forgejo host by HTTPS only, at a DNS name from the site file. The executor reads a token of a read-only bot `release-reader`. | Root refuses plain HTTP today (`provenance.py:151`, `executor/source.py:252`). P1 to P5 over plain HTTP would trust the LAN. |
| FR12 | No transparency log | The signer signs with no log entry. Root verifies with no network. Child 1 of P6 (`provenance.py:552-557`) retires. | A key needs no trust root from the network. A log adds a public service to every release. |
| FR13 | Merge queue | Forgejo has none. The protection of `main` requires the gate status and blocks an outdated branch (section 6). `merge_group` leaves the workflows. | Verification appendix of the plan: Forgejo ignores `merge_group`. |
| FR14 | `!retest` | `retest.yml` and its test retire. A rerun goes through `POST /repos/{o}/{r}/actions/runs/{id}/rerun`. | The comment payload has no `author_association` (verification appendix). The rerun API answers 204 (measured). |
| FR15 | Old Releases | A Release made on GitHub has no statement. The bridge executor refuses it. A `rollback` to a version from before the window is refused. | A clean cutover outranks old data. Every component moves forward with its next Forgejo tag. |

## 3. Each mechanism of today, and its replacement

| # | Mechanism | Today (file:line) | On Forgejo | Rests on |
| - | --------- | ----------------- | ---------- | -------- |
| M1 | Signing | creche `release.yml:859-964` (job `sign`, `id-token: write` at `:863-864`, `sign-blob` at `:949`). chaperone `release.yml:261-296` (`sign-blob` at `:289`) | The job `sign` retires. The `release` job sends a statement request to `ci-signer` over vsock (section 4) | Measured: a branch run and a PR run read every secret, so no key may be a secret. Not measured: CH vsock on the host |
| M2 | P1 | `provenance.py:218-233`, `BOT_LOGIN` at `:101-103` | `GET /repos/{o}/{r}/releases/tags/{tag}`. No author check. Plus the statement of the tag (section 5) | Measured on a scratch repository on 2026-11-16: the reply has `tag_name`, `draft`, `prerelease` and `assets`. For a Release that a job token creates, the create reply names the Actions user `forgejo-actions` (id -2) as author, and a later read names the user who started the run. P1 reads no author (FR2) |
| M3 | P2 | `provenance.py:236-270` (`git/ref/tags/{tag}`, `git/tags/{sha}`) | `GET /repos/{o}/{r}/git/refs/tags/{tag}` and `GET /repos/{o}/{r}/git/tags/{sha}` | Measured on a scratch repository on 2026-11-16: the refs call is a prefix match and answers a list, also for one tag. A missing tag answers 404. So P2 takes only the entry whose `ref` is exactly `refs/tags/{tag}`. `git/tags/{sha}` gives the commit in `object.sha` and the message with one newline added at its end |
| M4 | P3 | `provenance.py:273-280`, `ON_MAIN` at `:105` | `GET /repos/{o}/{r}/compare/main...{sha}`. Pass when `total_commits` is 0 | Measured: the reply has `commits`, `files` and `total_commits`, and no `status`. Measured on a scratch repository on 2026-11-16: `total_commits` is 0 for an ancestor of `main` and for `main` itself, and 1 for a commit one ahead and for a diverged commit. A missing commit answers 404 |
| M5 | P4 | `provenance.py:294-314` (`commits/{sha}/pulls`, a list) | `GET /repos/{o}/{r}/commits/{sha}/pull`, one object | Measured: 404 for a commit that is no PR merge. Measured on a scratch repository on 2026-11-16: a merge commit answers 200 with one PR object, `merged` true, `base.ref` `main` and `merge_commit_sha` equal to the commit. The PR head commit answers 404 |
| M6 | P5 | `provenance.py:317-341` (`actions/runs?head_sha=`) | `GET /repos/{o}/{r}/commits/{head}/status` | Measured: the context form `<workflow> / <job> (<event>)`, and a skipped job posts `skipped`. Measured on a scratch repository on 2026-11-16: the reply has `state`, `total_count` and `statuses`, with the latest entry of each context. Each entry names its state in `status`, not in `state` |
| M7 | P6 | `artifacts.py:71` (`TOKEN_ISSUER`), `:76` (`CODE_HOST`), `:82` (`WORKFLOW_DIR`), `:317-348` (`verify_words`), `provenance.py:369` (trust root), `:476` (2.5 GiB child) | `cosign verify-blob --key` on the statement. Root hashes each copy itself and compares | FR1. The exact cosign v3 flags for a key bundle with no log entry: not measured. On 2026-11-16 the build of the reading side of PR 4 (#626) found that no step of this note measured cosign at all (section 5, "Not measured") |
| M8 | Newest tag | `provenance.py:606-628` (`git/matching-refs/tags/{prefix}`) | `GET /repos/{o}/{r}/git/refs/tags/{prefix}`, then an exact match on each `ref` | Measured on a scratch repository on 2026-11-16: the path is a prefix match and answers a list. So the exact match on each `ref` is required |
| M9 | The API client | `provenance.py:90-94` (`api.github.com`, the API version header), `:146-176` (`Bearer`) | `https://<forgejo-host>/api/v1`, the header `Authorization: token <t>` | The API base is measured. HTTPS on the Forgejo host: not available today |
| M10 | The release token | `drain.py:76` (`RELEASE_GITHUB_TOKEN`), `:386`. `steps.py:981` | `RELEASE_FORGE_TOKEN`: a token of the bot `release-reader`, read-only collaborator on the five repositories | The plan's inventory: a Forgejo token has scopes and no list of repositories |
| M11 | The allocator | `allocate-tags.sh:183-187` (the PR read), `:200-210` (the bump labels), `:442-448` (the signature check), `:501-507` (tag push, `gh release create`) | curl to `/api/v1`: `commits/{sha}/pull`, the merged PRs with a `bump:` label from `issues?type=pulls&labels=<name>`, `POST releases` and one asset upload each. The signer creates the annotated tag with the statement (section 4.5) | Measured: the job token creates a Release (201). It pushes an unprotected tag, and tag protection refuses it on a protected name (422). Basic auth with `x-access-token` works. Measured on a scratch repository on 2026-11-16: `POST releases/{id}/assets?name=<n>` with the multipart field `attachment` answers 201 for each asset, and the name stays unchanged. `pulls?labels=` takes only numeric label ids and answers 500 for a name or a comma list. `issues?type=pulls&labels=` takes the name, and each item has `pull_request.merged`. Changed on 2026-11-16: the allocator reads the bump labels through the issues path, not through `pulls?labels=` |
| M12 | Artifact upload and download | creche `release.yml:752`, `:821`, `:953`, `:866`, `:871`, `:1002-1012`. chaperone `release.yml:254-327` | The forks of `data.forgejo.org/forgejo`, by full URL, pinned by SHA | Verification appendix: v4 and later of the upstream actions refuse Forgejo. The artifact limit for the disks (about 1 GB): not measured |
| M13 | Merge queue | creche `gate.yml:63-66` (`merge_group`), `:409` and `release.yml:486` (`GROUP_BASE`), `test_gate_workflow.py:154`, `:910-927` | FR13 | Verification appendix |
| M14 | `!retest` | `retest.yml:27` (`issue_comment`), `:40` (`author_association`), `:62-96` (`gh`). `bin/tests/test_retest_workflow.py` | FR14 | Measured: the rerun API |
| M15 | The scope read | creche `release.yml:156-168` (`gh run list` of the previous commit) | `GET /repos/{o}/{r}/commits/{before}/status`. Pass when each context `release / * (push)` is `success`. A failed read stays `code` | Measured on a scratch repository on 2026-11-16: the reply has the latest entry of each context, with its state in `status`. A tag that `POST /tags` creates starts push runs on the tagged commit, as a pushed tag does. The release workflows already limit `on: push` to `branches: [main]`. Not measured: that this filter keeps a tag push from starting the workflow on 17.0.1. The rehearsal of section 7 records it |
| M16 | Token scope and order | `permissions:` in each workflow. `concurrency: release-allocate` in creche `release.yml:977-979`, registry `tag.yml:38-40`, mcp `rework-component-tags.yml:47-49`, creche-release `tags.yml:41-43`. chaperone `release.yml:37-39` (group `release`) | `permissions:` is ignored, so each job has a write token. `concurrency:` stays. A race still moves no tag: the push of an existing tag fails | Measured: `permissions:` is ignored. The plan's research: `concurrency.group` is best-effort |
| M17 | The tag guard of creche-release | `.github/require-pr-merge.sh` (`gh api .../commits/$SHA/pulls`) | curl to `commits/{sha}/pull`. Check `merged`, `base.ref` is `main`, `merge_commit_sha` is the SHA | Measured: the 404 of a commit that is no merge. Measured on a scratch repository on 2026-11-16: the 200 reply of a merge commit has these three fields |
| M18 | The secrets guards of creche-release | `secrets-guard.yml:40-43` (`gh api --paginate .../files`, `previous_filename`, the operator's GitHub login). `gate.yml:29-35` (the same login) | curl to `GET /repos/{o}/{r}/pulls/{n}.diff`. The guard takes each path of each `diff --git`, `rename from` and `rename to` line. The guard refuses a diff line that it cannot parse and a quoted path. It reads `pulls/{n}` before and after the diff and refuses when `head.sha` is not the run's `commit_sha`. The operator's Forgejo login | Measured on a scratch repository on 2026-11-16: `pulls/{n}/files` reports a rename as `added` under the new name, with no `previous_filename` and no entry for the old path. That holds for a pure rename and for a rename with an edit. The diff names `rename from` and `rename to`. `on: pull_request_target` runs on 17.0.1, and it runs the copy of the workflow on the base branch. Changed on 2026-11-16: the guards read the diff, not the file list, because the file list hides the source of a rename. Proposal for the operator: the protection of `main` also names the guarded paths in its protected file patterns, which Forgejo enforces on the server. Not measured: that enforcement on 17.0.1, and whether the operator can still merge a PR that changes those paths. Not measured: whether the `.diff` of a large PR is complete |
| M19 | The checkout of creche at `CRECHE_REF` | chaperone `release.yml:48`, `:88-89`, `:314-315`. registry `tag.yml:54`, `:68-69` (`CRECHE_FAMILY_REF`), `:90-91`. mcp `rework-component-tags.yml:65`, `:98-99`. creche-release `tags.yml:56`, `:94-95` | `repository: <owner>/creche` resolves on the same Forgejo. A public creche needs no token | Measured: `github.api_url` names Forgejo. The visibility of creche on Forgejo: not checked |
| M20 | The git dependencies of creche-release | `pyproject.toml:41-44`: four packages from `https://github.com/<owner>/creche` | The Forgejo clone URL, then `uv lock` | The runner fence allows the Forgejo HTTP port (the runner design, section "The network fence") |
| M21 | The asset download | `artifacts.py:213-228`, `corpus/artifacts.py:89` (HTTPS only), `:293` | The same path shape on `<forgejo-host>` | The plan: Forgejo uses the same path shape. Measured on a scratch repository on 2026-11-16: `releases/tags/{tag}` gives each `browser_download_url` as `/<o>/<r>/releases/download/<tag>/<name>`. On a private repository that URL answers 200 with a token and 404 with none. The upload reply gives `/attachments/<uuid>` instead. So root takes the URL from the read of the tag, with the token of `release-reader`. HTTPS: see FR11 |
| M22 | The asset size | `catalog.py:350` (`DISK_FILE_MAX_BYTES`) | `[repository.release] FILE_MAX_SIZE` and `ALLOWED_TYPES` of Forgejo | Measured on a scratch repository on 2026-11-16: a `.zst` asset uploads, but `GET /settings/attachment` does not list `.zst`. So that list does not bind release assets. Not measured: the size limit of a release asset |

## 4. Key management and the signer

### 4.1 The shape

```
Forgejo host                     the host
+---------------+   HTTPS   +--------------------------------------------+
| Forgejo 17    |<----------| ci-runner daemon (user ci-runner)          |
|  runs, jobs   |           |   generic: reads the facts of each job,    |
|  PRs, tags    |<--+       |   matches the operator's [[attach]] rows,  |
+---------------+   |       |   passes each vsock fd of a matched VM     |
                    |       +----------------------+---------------------+
                    |                              | open, conn + fd, close
                    | HTTPS                        | (Unix socket)
                    |       +----------------------v---------------------+
                    +-------| ci-signer (user ci-signer), a creche part  |
                            |   checks the run itself, trusts no fact    |
                            |   holds the private key (a credential)     |
                            +----------------------^---------------------+
                                                   | vsock, fd passed
                            +----------------------+---------------------+
                            | VM of the `release` job                    |
                            |   sends tags and digests, gets statement   |
                            +--------------------------------------------+
```

The key never enters a VM. The job sends digests and gets back a signed statement. The runner passes the guest's connection to the signer and reads no byte of it. It holds no secret of the signer and no part of its code.

### 4.2 Who makes the key pair, and where

The operator makes the pair once, on the host, as root, with no network step:

1. Run `cosign generate-key-pair` in a new directory with mode 0700, with `COSIGN_PASSWORD` empty. Not measured: that cosign v3 signs with a key of an empty password and no prompt.
2. Move the private key to `/etc/creche/ci-signer/release-signing.key`, owner root, mode 0400.
3. Move the public key to `/etc/creche/release-signing.pub`, owner root, mode 0644.
4. Give the public key to the agent that writes PR 5 of section 8. That PR adds it as `handover/release-signing.pub`, for the self-check of the allocator.

A password beside the key would add no protection, so the key has none. The file mode is the protection. No second copy of the private key exists. Section 4.8 says why.

### 4.3 The signer

1. By the default of question 8, `ci-signer` is a creche component of its own. It ships through creche's release path, as each other creche component does: a tag, a Release, and P1 to P6 by root. The runner holds no part of it.
2. It runs as the system user `ci-signer`, which the operator makes with one `sudo useradd`.
3. The unit `creche-ci-signer.service` gives it the key with `LoadCredential=`. Only the unit reads the credential. The user `ci-runner` cannot read it.
4. The signer holds its own token of the bot `release-reader` and the token of the Forgejo bot `ci-signer`, each as a credential. It never uses the token of the runner, and it never gets one.
5. The signer listens on its own Unix socket, `/run/ci-signer/attach.sock`. The user `ci-signer` owns it, and the group `ci-runner` may connect. It speaks the version-2 control lines of the runner design, section 6.2 "The exchange": `open`, `conn` and `close`. Changed on 2026-11-16: the first text named version-1 relay lines. The runner design no longer has them. The signer relies on these facts, and refuses when one fails:
   1. The peer uid of each control connection (`SO_PEERCRED`) is `ci-runner`.
   2. Each `conn` line carries one guest connection as a file descriptor, passed by `SCM_RIGHTS`. The daemon reads no byte of it.
   3. Before the signer reads a byte of a passed descriptor, it checks the peer by section 6.3 "What the service can rely on": the peer process from `SO_PEERPIDFD`, its full cgroup path, which must be the unit of the slot of the session, and the peer still alive through the pidfd.
   4. A `conn` line or a `close` line of a session that the signer did not accept is refused.

   Section 6.3 of the runner design says what these facts do not cover: which job runs in the slot. That rests on the daemon (section 4.10, item 3).
6. The list of signing pairs is a file in the tree of the `ci-signer` component: each repository name with its release workflow file. A creche test holds the list equal to `catalog.RELEASE_WORKFLOW`. The owner comes from the host config of the signer, so a rehearsal names a shadow owner with no release.
7. The signer writes one journal line for each statement: the run id, the commit and the SHA-256 of the statement.

| Repository | Workflow file |
|---|---|
| creche | `release.yml` |
| chaperone | `release.yml` |
| mcp | `rework-component-tags.yml` |
| creche-release | `tags.yml` |

registry has no row. Its component `registry-data` is `Releases.NO` (`catalog.py:173`), so root never verifies one of its tags.

### 4.4 Which run gets a signature

Two parts select a run. A policy row of the runner is a first filter. The checks of the signer decide.

**The policy rows.** The runner's policy file is root-owned on the host, outside every release. By the default of question 9, only the operator changes it. For each pair of the list of section 4.3, the operator adds one `[[attach]]` row. The row for creche:

```toml
[[attach]]
name = "ci-signer-creche"
kind = "creche-release"
repository = "<owner>/creche"
workflow = "release.yml"
event = "push"
ref = "refs/heads/main"
vsock_port = 5000
socket = "/run/ci-signer/attach.sock"
```

The row names the full ref `refs/heads/main`, not `default-branch`. An admin of a repository can change its default branch. Check 3 of the signer asks for `refs/heads/main`.

The runner can match these job facts (the runner design, sections "Job facts" and 6.1 "The match"):

1. The slot kind of the job, from the labels of its `runs-on`.
2. The repository, as `owner/name`.
3. The workflow file.
4. The event.
5. The full ref. A tag never matches a branch.

The runner cannot match:

1. One job of a run. A row matches each job of the run that takes a slot of its kind.
2. Whether the commit is a merge of a pull request into `main` (check 4).
3. Whether the run already got a statement.

So the release job asks for the label `creche-release`, and the policy holds a kind `creche-release` with that one label (question 10). The runner serves every owner of the instance, and a label belongs to one kind only. So the label names its consumer, and no other user of the instance takes the name by chance. The kind lists the four repositories of section 4.3 in its `repositories`. A job of any other repository with that label stays waiting. The other jobs of a release workflow run on the plain kind and get no vsock device.

The runner reads its facts with its own token. The signer does not trust them. The runner design does not yet record the facts of a push run on 17.0.1 (its open question "The event and the ref of a run"). Measured on a scratch repository on 2026-11-16: for a merge push to `main`, the run reply gives `event` `push`, `trigger_event` `push` and `event_payload.ref` `refs/heads/main`. Which of these facts the runner reads for its row is a gap of the runner design (section 10, question 12).

**The checks of the signer.** The daemon sends one `open` line to the signer before the VM starts. The signer takes only the repository and the run id from that line, as the key of its own read. The run id comes from the run id field of the facts of the `open` line, never from its `session` field (the runner design, section 6.2, step 1). The `session` field is the runner's own run key, and the runner chooses its form. The signer uses it only to match the later `conn` and `close` lines to the accepted `open` line. It reads the run with its own token: `GET /repos/{o}/{r}/actions/runs/{id}`. Measured on 2026-11-15: the run reply has `workflow_id`, `event`, `trigger_event`, `prettyref`, `commit_sha` and `repository.full_name`. A run is eligible only when each check passes:

1. The repository and the workflow file are a pair of the list of section 4.3. The run reply names the file in `workflow_id`, for example `test.yml` (measured 2026-11-15).
2. `event` is `push`, and `trigger_event` is `push`.
3. The run reply has `event_payload`, a JSON text. Its `ref` is exactly `refs/heads/main`.

Changed on 2026-11-16: check 2 also reads `trigger_event`. Measured on a scratch repository: `event` is `pull_request` for a `pull_request` run and for a `pull_request_target` run, and `event` is empty for a `workflow_dispatch` run. Only `trigger_event` names each of them.
4. `GET /repos/{o}/{r}/commits/{commit_sha}/pull` answers one PR with `merged` true, `base.ref` `main` and `merge_commit_sha` equal to `commit_sha`.
5. The run is not finished.

Check 3 reads the full ref, not `prettyref`. `prettyref` is a short ref name. A tag named `main` gives a run with `event` `push` and `prettyref` `main`, the same as a push to the branch `main` (measured 2026-11-15 on a scratch repository: run 15, from a tag `main` made through the API). The runner design records `event_payload.ref` as a full ref, for example `refs/heads/vm-spike`, on a `workflow_dispatch` run (section "Job facts"). Measured on a scratch repository on 2026-11-16, from the run reply:

| Run | `event` | `trigger_event` | `prettyref` | `event_payload.ref` |
|---|---|---|---|---|
| merge push to `main` | `push` | `push` | `main` | `refs/heads/main` |
| push to another branch | `push` | `push` | the branch name | `refs/heads/<branch>` |
| pushed tag, or a tag that `POST /tags` creates | `push` | `push` | the tag name | `refs/tags/<tag>` |
| tag named `main` | `push` | `push` | `main` | `refs/tags/main` |
| `pull_request` | `pull_request` | `pull_request` | `#<n>` | absent |
| `pull_request_target` | `pull_request` | `pull_request_target` | `#<n>` | absent |
| `workflow_dispatch` | empty | `workflow_dispatch` | the branch name | `refs/heads/<branch>` |

So only `trigger_event` and `event_payload.ref` together tell these runs apart. Checks 2 and 3 read both. The rehearsal of section 7 records the same facts for the tests.

What each check refuses:

1. Check 1 refuses another repository and another workflow file.
2. Check 2 refuses a PR, a `pull_request_target`, a `workflow_dispatch` and each other event.
3. Check 3 refuses a branch other than `main`, and each tag. A tag `main` at an old merge commit of `main` passes checks 1, 2, 4 and 5. Only check 3 refuses it in the signer.
4. Check 4 refuses a commit that is no merge of a PR into `main`, for example a direct push past the protection of section 6.
5. Check 5 refuses a run that is already over.

The signer reads every fact of these checks itself. It trusts no fact of the runner. The `ref` of the runner's row and the tag protection of the name `main` (section 6) are two more layers in front of check 3. Neither replaces it, because a wrong policy row or an admin who removes the protection opens both.

The signer answers `accept` only when each of the five checks passes. Only then does the daemon bind a listener for the port of the row, start the VM with a vsock device, and pass each guest connection to the signer (the runner design, section 6.2, steps 3 and 4). A refusal, a fault or a timeout disables the port of the row. With no other accepted row, the VM gets no vsock device (section 6.2, rule 1). The job still runs, and its allocator gets no statement.

### 4.5 The exchange

1. The allocator in the `release` job plans its tags. It knows each tag and each asset before any tag exists.
2. It connects to the vsock port of the row. The daemon accepts the connection and reads no byte. It sends a `conn` line that names the session on the control connection of that session, with the descriptor of the connection passed by `SCM_RIGHTS` (the runner design, section 6.2, step 4). The signer checks the descriptor by section 4.3, item 5, before it reads a byte. It forgets a session at its `close` line. So the bytes come from the CH of the slot of that session, or from an escape out of that VM. The signer cannot tell which job runs in that slot (section 4.10, item 3). Changed on 2026-11-16: the first text named a version-1 `relay` line, which named only the session.
3. It sends one request over vsock: a list of tags, each with its assets as a name and a SHA-256. A `venv` or `units` tag has no asset.
4. The signer refuses a request that breaks a limit: at most 32 tags, at most 16 assets for each tag, a tag of the form `<component>-vX.Y.Z`, a plain asset name, 64 lower-case hex digits. Proposal of 2026-11-16, for the operator: also at most 64 bytes for each tag and each asset name (see the message size below).
5. The signer builds the statement. It writes its own fields from section 4.4 and the tags of the request. It signs the bytes with `cosign sign-blob --key`, with no log entry.
6. The signer answers the statement and its bundle. It serves one statement for each run id. A second request of the same run is refused.
7. The signer creates each annotated tag of the request at the run's commit, through `POST /repos/{o}/{r}/tags` with the token of the Forgejo bot `ci-signer`. Tag protection refuses that call to every other user, the job token included (measured, section 6). Measured on a scratch repository on 2026-11-16: `POST /tags` with a `message` makes an annotated tag (201), and that tag starts push runs with `event_payload.ref` `refs/tags/<tag>`. Check 3 refuses each of them.
8. The allocator reads the tags back and checks the bundle with `handover/release-signing.pub`. It then creates each Release and uploads its assets with the job token. A check that fails releases nothing.

The guest side of step 2. The allocator `allocate-tags.sh` is bash and curl, and curl has no vsock support. So a small Python 3 helper in creche makes the connection. The allocator runs it, and it connects to CID 2 (the host) at port 5000, the `vsock_port` of the row. The Ubuntu cloud image ships Python 3, and Python 3 on Linux has `socket.AF_VSOCK`. If both hold for the runner image, the helper needs nothing added to that image. Not checked: Python 3 in the runner image, and its `AF_VSOCK`. The runner design says that CH forwards a guest connection to host port `P` (section 6.2, step 3). Its spike S4 (section 10.8) connects from the guest to CID 2 at port 5000. So CID 2 agrees with the runner design. CH vsock is not measured on the host.

The statement, in its strict JSON form:

```json
{
  "statement": 1,
  "repository": "<owner>/creche",
  "workflow": "release.yml",
  "event": "push",
  "ref": "main",
  "commit": "<40 hex>",
  "run": 1234,
  "signed_at": "2026-11-15T12:00:00Z",
  "tags": [
    {"tag": "vmm-v0.1.24", "assets": [{"name": "vmm-x86_64-unknown-linux-musl.tar.gz", "sha256": "<64 hex>"}]},
    {"tag": "handover-v0.1.96", "assets": []}
  ]
}
```

The signer writes each tag of the run as an annotated tag. Its message has three lines: the word `release-statement-1`, the statement in base64, and the bundle in base64. Base64 has no character that `git tag --cleanup=verbatim` changes.

Measured on a scratch repository on 2026-11-16: `POST /tags` takes a message of at most 131071 bytes. A message of 131072 bytes or more answers 500 with `argument list too long`, the Linux limit for one argument. The limits of step 4 are 32 tags with 16 assets each, and each tag message holds the whole statement. They set no length for a tag or an asset name. So a message may not fit. With 64 bytes for each tag and each asset name, and a bundle of at most 4096 bytes, the largest message is about 113500 bytes. Not measured: the size of a cosign bundle with a key and no log entry.

Proposal of 2026-11-16, for the operator:

1. Step 4 caps each tag and each asset name at 64 bytes.
2. The signer refuses a bundle above 4096 bytes.
3. The signer refuses a request whose tag message would pass 120000 bytes, before it signs. A larger release then fails closed, with no tag.

The other choice is a bundle and a statement that travel as an asset of each Release. That choice adds a download to P1 and is not taken.

### 4.6 What a branch run gets

1. A run of a branch push, of a tag, of a PR or of `workflow_dispatch` matches no policy row of section 4.4, and it fails check 2 or check 3 of the signer. Its VM gets no vsock device, also when the job asks for the label `creche-release`.
2. Its allocator gets no statement and no tag. It releases no component of `Releases.YES`, and the run ends red.
3. Its job token cannot create or delete a release tag, because tag protection refuses it (measured, section 6). It can still create a Release on another tag name. Root refuses that release at P1, because no statement names the tag at its commit.

A rerun of a run that already got a statement gets no second one. The next green run on `main` tags what the rerun left, as today (`release.yml:40-45`).

### 4.7 Rotation

1. The operator makes a new pair by section 4.2, steps 1 to 3, and replaces both files on the host. The signer restarts.
2. An agent opens a creche PR that replaces `handover/release-signing.pub`. The operator merges it.

A release run between step 1 and step 2 fails its self-check and tags nothing. The run of the PR of step 2 signs with the new key and passes. The next green run tags what the red runs left.

### 4.8 Leak and loss

A leak: the operator rotates at once (section 4.7). From step 1, root refuses every statement of the old key. The operator compares the journal of the signer with the runs of Forgejo for the time of the leak. A release that root installed before the rotation stays installed. The operator reads its journal line.

A loss: the operator rotates (section 4.7). Nothing else is needed, because the anchor is a root file outside every release (FR10). So no backup copy of the private key exists. A backup would be a second place for a leak.

### 4.9 Alternatives refused

| Alternative | Why not |
|---|---|
| The key as a Forgejo Actions secret | Measured: a branch push and a PR of the same repository read every repo and org secret. Agents push branches. |
| The key on the seed disk of an eligible VM | A compromised action in the job reads the key, and the VM reaches the public internet. |
| The signer inside the runner | The runner serves every repository of the instance and holds no creche concept (FR16). |
| The runner daemon itself signs | FR16. Also, each CH process runs as `ci-runner`. A CH escape would read the key. |
| The runner daemon signs after the VM exits | FR16. Also, the job is over. The daemon would then need a write token to attach the signature. |
| A private Sigstore with Forgejo as OIDC issuer | More services, and Forgejo's OIDC token is also given to every branch run. |

### 4.10 What stays open

The operator put the fix for items 1 and 2 first on 2026-11-16. The runner design decided it as row D9 and section 10 "Privilege separation". Items 1 and 2 describe the risk before that separation. Item 3 says what D9 closes and what stays.

1. **The runner user is trusted for the relayed bytes.** A CH escape in any VM of the instance gives code as `ci-runner`. The group `ci-runner` may connect to the socket of the signer. That code can read the run key of a real release run from the run folder or the daemon. It sends a `relay` line of version 1 of the exchange on the accepted session before the guest boots (about 17 s, the runner design, section "Measured numbers", fact 1). It then sends its own tags and SHA-256 values. The signer signs them under the real merge commit and creates the tags (section 4.5, step 7). The run passes section 4.4, but the signed digests are not the build of that run. The checks of the signer do not cover this case. The escape cannot read the key, and it cannot get a statement for a run that fails section 4.4. That is no full bound: for a run that passes, the escape picks the content of the statement.
2. **The escape can complete the forged release.** The same escape may read the registration token of the runner, which has the admin scope of the instance (the runner design, section "Security"). With that token it creates the Releases of the signed tags and uploads assets that match its digests. Each release then carries a valid statement, so P1 to P6 pass on its binaries. An instance admin can also change the protection of `main` and of release tags, and merge into `main` past the allowlist of answer 7. That makes a fresh eligible run on demand. This case undercuts FR2, answer 1 and answer 7. It stays open.
3. **What D9 closes and what stays.** Each job VM now runs as a throwaway dynamic user in its own systemd unit, with its own tap, AppArmor profile, Landlock and sandbox (the runner design, section 10 "Privilege separation"). The daemon stays under `ci-runner`, holds the admin token and reads no byte from any VM. An attachment passes the guest's connection to the service as a file descriptor (version 2 of the exchange, section 6.2). Version 2 replaces version 1, so no `relay` line exists any more. D9 closes this part: an escape from a job VM no longer reaches the socket of the signer, another VM or the admin token. So the escape cannot pick the content of a statement (item 1) and cannot finish a forged release (item 2). This part stays open: a compromise of the daemon still gets the admin token. It can also tell the signer that a session belongs to another slot, so it can make the VM of a branch job look like the accepted session of a release run. Root on the host gets everything. The signer uses the service-side check that the runner design offers. It checks that the peer of each passed connection runs in the unit of the slot that it accepted (section 6.3, and section 4.3, item 5 of this note). The runner design keeps that check as default. As of 2026-11-16 it is not yet built, and spikes S3 and S4 of section 10.8 are not yet measured on a host. Items 1 and 2 describe version 1 of the exchange, which never ships: runner PR 8 builds version 2 after the separation (the runner design, section 15). What stays is the daemon and root part above.
4. A malicious change that a PR merges into a release workflow gets signed. That is row 10 of the threat table in creche-release `docs/releases.md` section 6, as today.

## 5. Provenance on Forgejo

Each call goes to `https://<forgejo-host>/api/v1`, with `Authorization: token <release-reader token>`. Each answer is strict JSON with a byte cap, as today. Each check fails closed.

| Check | Call | Fields read | Rule | Measured |
|---|---|---|---|---|
| P1 | `GET /repos/{o}/{r}/releases/tags/{tag}` | `tag_name`, `draft`, `prerelease` | The Release exists, names the tag, is no draft and no prerelease. No author check (FR2) | The fields, on a scratch repository on 2026-11-16 |
| P1 | the tag object of P2 | `message` | The message holds a statement and a bundle. The key of FR10 passes the bundle. The statement names the repository of the catalog row, its workflow of `RELEASE_WORKFLOW`, `event` `push`, `ref` `main`, the resolved SHA, and the tag | No. Measured on a scratch repository on 2026-11-16: `git/tags/{sha}` adds one newline at the end of the message |
| P2 | `GET /repos/{o}/{r}/git/refs/tags/{tag}`, then `GET /repos/{o}/{r}/git/tags/{sha}` | each `ref`, `object.type`, `object.sha` | One entry has `ref` equal to `refs/tags/{tag}`. Its object is an annotated tag. That tag names a commit equal to the resolved SHA. A lightweight tag is refused | Yes, on a scratch repository on 2026-11-16. The refs call is a prefix match and answers a list. A ref to an annotated tag answers `object.type` `tag`. A ref to a lightweight tag answers `object.type` `commit` |
| P3 | `GET /repos/{o}/{r}/compare/main...{sha}` | `total_commits`, `commits` | `total_commits` is 0 and `commits` is empty | Yes, on a scratch repository on 2026-11-16: 0 for an ancestor and for `main`, 1 for a diverged commit |
| P4 | `GET /repos/{o}/{r}/commits/{sha}/pull` | `number`, `merged`, `base.ref`, `merge_commit_sha`, `head.sha` | A 404 refuses. The PR is merged into `main` with `merge_commit_sha` equal to the SHA | Yes, on a scratch repository on 2026-11-16 |
| P5 | `GET /repos/{o}/{r}/commits/{head.sha}/status` | `total_count`, each `context` and its `status` | Each required context of section 6 for the repository is `success`. Each other context is `success` or `skipped`. A count above one page refuses | Yes, on a scratch repository on 2026-11-16: the latest entry of each context, its state in `status` |
| P6 | none: root's copy and the statement of P1 | the SHA-256 of each copy | Each asset of the tag in the statement has a copy with the same SHA-256. The binary and disk-image rules after the signature stay (`provenance.py:483-549`) | No API |

P1 runs its statement check through child 2 of today, with no network (`provenance.py:560-583`). The words are `verify-blob --key /etc/creche/release-signing.pub --bundle <bundle> <statement>`, plus the flags that skip the log, which are not measured. Root writes the statement and its bundle into its own work tree from the tag message. Root parses the statement only after the child passes. The statement has a closed key set and a byte cap. `ReleaseStatement` is the one type that holds a parsed statement, and only the check makes one, as `VerifiedArtifact` today.

Not measured, as of 2026-11-16. The builds of PR 2 and of the reading side of PR 4 (#626) of section 8 found these two facts:

1. cosign is not measured at all. Nobody ran cosign v3 for this path. Not measured: the flags of `verify-blob` with `--key`, `--bundle` and no log entry, whether it reads a trust root from the network, and the size of its bundle. The reader in #626, the reading side of PR 4, caps a bundle at 4096 bytes (section 4.5).
2. The page size of `GET /repos/{o}/{r}/commits/{sha}/status` is not measured. The P5 reader in #626 refuses when `total_count` differs from the count of entries in `statuses`. So a reply with more than one page refuses.

The 2.5 GiB child of a disk file retires (`provenance.py:476`). Root hashes each copy of a disk file already for the approval digest (`artifacts.py:201-210`).

The registry commit of an `mcp-servers` release keeps P3 to P5 only (`provenance.py:355-363`, `steps.py:996-1004`). The comment at `steps.py:999` says that registry `main` is unprotected. That is no longer true after the move.

## 6. Protection of `main` and of release tags

Each of the five repositories gets one protection rule on `main` before its first merge on Forgejo. The four conditions of the verification appendix:

1. The push allowlist is on and empty.
2. "Unprotected file patterns" is empty.
3. "Allow deploy keys" is off on the push allowlist.
4. The server-side git hooks of Forgejo stay installed.

Each rule also sets:

1. The required status contexts of the table below.
2. "Block merge if pull request is outdated" on. The PR head that P5 reads then holds the tree that merges.
3. The merge allowlist on, with the operator's account only. No bot user merges.
4. No force push and no deletion.
5. The rule also binds admins: `apply_to_admins` is true. Measured on a scratch repository on 2026-11-16: `GET /branch_protections/main` has that field, and it makes a rule bind admins.

| Repository | Required contexts |
|---|---|
| creche | `gate / gate (pull_request)` |
| mcp | `gate / gate (pull_request)` |
| chaperone | `ci / gate (pull_request)` |
| registry | `validate / validate (pull_request)`, `validate / reach (pull_request)` |
| creche-release | `gate / gate (pull_request)`, `secrets-guard / guard (pull_request_target)` |

Each name is `<workflow name> / <job name> (<event>)` from the workflow files at the commits of the source line. A change to a workflow name or a job name needs the same change in the rule.

Tag protection (`/repos/{o}/{r}/tag_protections`):

1. Protect the name pattern of release tags, `*-v*`. The allowlist holds the Forgejo bot `ci-signer` only. The signer creates each annotated release tag itself, with the statement in its message (FR8). No job pushes a release tag.
2. Protect the name `main`. Its allowlist holds a user with no write on the repository, so nobody can make the tag. It closes the short ref of section 4.4 as a second layer.

Measured on Forgejo 17.0.1 on 2026-11-15, on a scratch repository:
- The Actions user cannot be on an allowlist. `forgejo-actions` is refused with 422 "user does not exist".
- An empty allowlist is refused with 400 ("both whitelist_usernames and whitelist_teams are empty").
- An allowlist that names a user with no write on the repository is accepted and stored as empty.
- The job token of a branch run is refused (422) on a protected tag name, and creates an unprotected tag (201).

Not measured: that the bot `ci-signer` creates a tag on `*-v*` when the allowlist names it, and that the same rule refuses an admin token and a job token on that name. The rehearsal of section 7 records them.

So tag protection does what the signature cannot do alone: a branch run can neither make nor delete a release tag. The signature stays the guard of what root installs (section 4.6).

## 7. The bridge release and the window

Before the window:

1. **The key** (the operator). Section 4.2. Rollback point: delete the files. Until step 3, nothing reads them.
2. **The bot** (the operator). Make `release-reader` as a read-only collaborator of the five repositories, and two tokens. Rollback point: delete the bot.
3. **The signer and the rehearsal rows** (the operator, with exact commands from an agent). Root installs the `ci-signer` release of PR 2, with the shadow owner in its host config. The bot `ci-signer` gets write on the shadow repositories, and a rehearsal token that only the signer holds. The token and the write of answer 1 still come in the window. The operator adds the kind `creche-release` to the runner's policy file, with the shadow repositories in its `repositories`. The operator adds the `[[attach]]` rows of section 4.4 with the shadow owner, and restarts `ci-runner.service`. Rollback point: remove the rows and the kind, restart the unit, and stop the signer.
4. **The rehearsal.** An agent imports the five repositories into the shadow owner on Forgejo. The ported branches of section 8 run there: a full gate and a full release of each repository, with the signer of step 3. An agent runs P1 to P6 from a dev checkout of the bridge branch against the shadow releases, never on the host. It records each Forgejo reply that section 5 reads, for the tests, and the run facts of section 4.4 for a push to `main` and for a tag `main`. It proves each refusal: a branch push, a PR, a `workflow_dispatch` on `main`, a tag named `main` at a merge commit of `main`, a Release with no statement, a statement of another commit. At the end, the operator moves the policy and the signer to the real owner. The kind then lists the four repositories of section 4.3, each row names the real owner, and the host config of the signer names the real owner. The real owner has no repository on Forgejo yet, so no job matches. The operator deletes the rehearsal token of the bot `ci-signer`. Rollback point: delete the shadow owner.
5. **The site and the secret** (the operator opens the creche-release PR, because `secrets-guard` allows only the operator). Add `AGENT_FORGE_HOST`, `AGENT_FORGE_OWNER` and `RELEASE_FORGE_TOKEN`. The operator taps the `host` release. The current executor ignores the new keys. Rollback point: a revert PR and a tap.
6. **HTTPS on the Forgejo host** (the operator, question 4). Rollback point: none needed. Nothing uses it yet.
7. **Every pending GitHub tag installs or is dropped** (the operator). After the bridge, root refuses each GitHub Release (FR15).

The window:

1. **Freeze** (the operator). No merge on GitHub to the five repositories. Rollback point: lift the freeze.
2. **The bridge** (an agent opens it, the operator merges it on GitHub and taps it). creche PR 4 of section 8 releases `handover` alone from GitHub. The current executor verifies it with GitHub's P1 to P5. From now on, root speaks Forgejo only. Rollback point: the operator restores `/opt/components/handover.prev` by hand, as root. That is the GitHub executor, and GitHub is still whole.
3. **The import** (an agent). Import the final `main`, the tags, the PRs and the Releases of the five repositories with `POST /repos/migrate`. Protect `main` and the tags of each one (section 6) before any merge. Rollback point: delete the Forgejo copies. GitHub is still whole.
4. **The host repoint** (the operator, with exact commands from an agent). `git remote set-url` for each clone in the corpus and the registry checkout, `code-repos.txt`, `known_hosts`, the keys. Rollback point: set each URL back.
5. **creche from Forgejo** (an agent opens PR 5, the operator merges it on Forgejo and taps). Its run on `main` gets a statement and tags `handover` alone first. Proof: root passes P1 to P6 on the host, and `.release-version` of `handover` names the tag. Rollback point: a fix PR on Forgejo, or step 2's restore and the GitHub path.
6. **registry, mcp, chaperone, creche-release from Forgejo, in that order** (an agent opens PRs 6 to 9, the operator merges each and taps). registry goes first: an `mcp-servers` release checks a registry commit with P3 to P5, and a merge imported from GitHub has no status on Forgejo. Rollback point: a revert PR on that repository.
7. **The push mirrors** (the operator). Add a push mirror to GitHub for each repository, and turn off GitHub Actions there. This is the last rollback point: after it, the GitHub path is gone. Before it, step 2's restore still works.
8. **Lift the freeze** (the operator).

## 8. The PRs

Each PR holds one idea. "Before" means the PR merges before the window and works on GitHub and on Forgejo. "Window" means the PR lands in the window.

1. **This note** (creche, before). Proof: the operator merges it.
2. **The signer** (creche, before, after PR 8 "Attachments" of the runner design, section 15 "Build plan"). The program `ci-signer`, in the form that question 8 decides (default: a binary component), its unit, its socket for the version-2 control lines of the runner design (section 6.2), the descriptor check of section 4.3 item 5, the checks of section 4.4, the exchange of section 4.5 and the list test of section 4.3. Proof: unit tests against Forgejo replies recorded in the rehearsal refuse each failed check. Unit tests refuse a `conn` line of a session that the signer did not accept, and a passed descriptor whose peer is not in the unit of the slot of its session. In the rehearsal of section 7, a release run of the shadow owner gets a statement, and each refused run of that step gets none.
3. **The site keys and the secret** (creche-release, before, opened by the operator). Proof: the `host` release installs, and the current executor still passes a release.
4. **The bridge executor** (creche, window step 2, merges on GitHub). The API client of section 5, P1 to P6 on Forgejo, `ReleaseStatement`, the key file of FR10, the site keys of FR11. It removes the GitHub client. Proof: tests against the recorded replies. The dev checkout run of the rehearsal (section 7, before the window, step 4) passes for one tag of each kind and refuses each attack of that step.

   Proposal of 2026-11-16, for the operator. The build of PR 4 covered only its reading side, with no caller. Split PR 4 into two PRs:

   1. **4a, the reading side** (creche, before). The readers of the Forgejo replies of section 5, P1 to P6 and the typed cosign boundary, with no caller. It can merge before the window. Its merge mints one `handover` release that changes no behaviour, because nothing calls the new code.
   2. **4b, the wiring** (creche, window step 2, merges on GitHub). The HTTPS client and the token header (FR11, M9), the site keys (M10), the owner and mode check of the key file of FR10, the wiring into `steps.py` and `drain.py`, and the removal of the GitHub client and of child 1 (FR12). The proof of PR 4 above moves to 4b.

   The cost: one extra `handover` release before the window. The split changes no decision of this note. The window step of the bridge stays step 2.
5. **creche releases from Forgejo** (creche, window step 5). The allocator on `/api/v1` with the statement and the annotated tag, `release.yml` with the label `creche-release` and no job `sign`, the artifact forks, the runner labels, `gate.yml` with no `merge_group`, the removal of `retest.yml` and its test, the tests that pin each workflow and the allocator. It also removes the unread keys of `env/src/creche_env/default.env:32-35` and fixes `steps.py:999`. Proof: its own run on `main` tags `handover`, and the host installs it.
6. **registry on Forgejo** (registry, window). `CRECHE_REF`, `CRECHE_FAMILY_REF` and the runner labels. Proof: its run on `main` tags `registry-data`.
7. **mcp on Forgejo** (mcp, window). `CRECHE_REF`, the label `creche-release`, the runner labels, the rerun line of `README.md` and `CONTRIBUTING.md`. Proof: the host installs the next `mcp-servers` tag, with P3 to P5 on the registry commit of PR 6.
8. **chaperone on Forgejo** (chaperone, window). `CRECHE_REF`, no job `sign`, the label `creche-release`, the artifact forks, `tests/release.rs`, `docs/design.md` section 12 (`:616-618`). Proof: the host installs the next `chaperone` tag, and P6 passes.
9. **creche-release on Forgejo** (creche-release, window). `CRECHE_REF`, the label `creche-release`, `require-pr-merge.sh`, both secrets guards, the git dependencies and `uv.lock`, the old site keys out, and the contract text: contract 06 section 3.4 and section 8.4, and `docs/releases.md` section 2.4 step 3 and section 6 rows 1, 2 and 22. Proof: the host installs the next `host` tag.

The runner's policy rows are no creche PR. By the default of question 9, they are an operator step on the host (section 7, before the window, steps 3 and 4). The runner's own PR 8 "Attachments" (the runner design, section 15) builds the generic interface in the repository `infra/ci-runner`. This note does not change that repository.

## 9. Answers of the operator (2026-11-15)

The operator answered each open question of the first draft on 2026-11-15.

1. **Tag protection and the Actions user.** "Your recommendation." Measured: the Actions user cannot be allowed (section 6). Taken: a Forgejo bot `ci-signer` is the only user allowed on `*-v*`, and the signer creates each release tag. This bot is a third account beside the two of answer 6. It holds write on the five repositories and nothing else. Its token lives with the signer, never in Forgejo. The operator approved this third account on 2026-11-15 ("Yes, add the bot"). The bot exists as a restricted user with no token and no repository yet. It gets write on the five repositories and its token in the window.
2. **FR10 against FR4.** "Your recommendation." Taken: FR10. The bridge reads the root-owned key file `/etc/creche/release-signing.pub`.
3. **FR9: a statement for every kind.** "Your recommendation." Taken: FR9.
4. **HTTPS on the Forgejo host.** The operator sets up HTTPS through a reverse proxy. Requirements:
   1. A DNS name that the host resolves to the proxy.
   2. A certificate from a CA that the host's CA store trusts.
   3. The proxy forwards to the Forgejo HTTP port, with a body limit and timeouts that fit the largest release asset (the disks asset is about 1 GB).
   4. Forgejo's `ROOT_URL` names the HTTPS address, so API replies and clone URLs carry it.
   5. SSH stays on its own port and does not pass the proxy.
   6. The runner fence (the runner design, section "The network fence") allows the proxy address and port in place of the Forgejo HTTP port. The Forgejo client of the runner then speaks HTTPS too (the runner design, open question "HTTPS to Forgejo").
5. **Visibility of creche and chaperone.** "Private." Consequences:
   1. The follower and root read with the token of `release-reader`.
   2. A job token reads only its own repository. Measured on 2026-11-15: a job of one repository got "User permission denied" from git and 404 from the API for another private repository of the organization.
   3. So the `CRECHE_REF` checkouts of registry, mcp, chaperone and creche-release need a read token. A second token of `release-reader`, with read scope only, is an organization Actions secret. Branch runs can read it (section 3), so it must hold read scope only. The token of root is a different token of the same bot and never enters Forgejo.
6. **Two new accounts.** "Yes." The system user `ci-signer` on the host and the bot `release-reader` on Forgejo. Answer 1 adds the Forgejo bot `ci-signer`.
7. **Who merges.** "Yes." The merge allowlist holds the operator's account only, on all five repositories.

## 10. Open questions (2026-11-16)

FR16 and the review of its revision open these questions. Each has a default. Only the operator can answer them.

8. **How the creche catalog ships the signer.** Default: a new binary component `ci-signer` of the catalog, a Rust crate under `rust/crates/ci-signer/`, with its unit `creche-ci-signer.service` in the component. Root installs it as each other binary component, with P1 to P6. Its first release ships from GitHub before the window. Each later release carries a statement that the running signer made. Not taken: a second program in a component that the runner uses.
9. **Who reviews the runner's policy file.** The file is root-owned on the host and is in no repository. Default: only the operator changes it, with exact commands from an agent. Section 4.4 of this note is the reference text for the rows of the signer. An agent proposes each change of a row in a creche PR to this note first.
10. **The kind `creche-release` and its capacity.** The runner gives one tap device to each slot, and the sum of the slots must not pass the tap count (the runner design, section "The policy file"). Default: one slot of the kind `creche-release`, taken from the plain kind, so the plain kind keeps 3 slots and the tap count stays six. The kind has the size of a plain slot. Not taken: a seventh tap, which needs a change of the network unit by root. The policy file of the instance holds the label `creche-release`, so no other kind can take it. A job of another owner that asks for it stays waiting.
11. **Does the release path wait for the runner's question "Who holds the admin token".** Answered. The operator put the fix first on 2026-11-16, and the runner design decided it as row D9 and section 10 "Privilege separation". Section 4.10, items 1 to 3. D9 closes this part: an escape from a job VM no longer reaches the signer socket, another VM or the admin token. This part stays: a compromise of the runner daemon still gets the admin token and can map a session to another slot, and root on the host gets everything. The signer uses the service-side check that the runner design offers. It checks that the peer of the passed connection runs in the unit of the accepted slot. Default: the release path does not wait. It accepts the part that stays after the separation. Items 1 and 2 never apply to the release path, because the signer needs runner PR 8, which comes after the separation (section 4.10, item 3). Not taken: a signer that gets the digests by its own means. The job builds and uploads every artifact, so the code that holds the job token of the run controls those artifacts too.
12. **Gaps of the runner design that this note depends on.** This note does not change the runner repository. It names each gap here, for a change of the runner design:
    1. The guest address of an attached port. Answered in the runner design on 2026-11-16: spike S4 of section 10.8 connects from the guest to CID 2. This note reads CID 2 (section 4.5). Not measured on a host.
    2. The source of an attached connection. Answered in the runner design on 2026-11-16: version 2 passes the descriptor, and the service checks the cgroup of its peer (section 6.3). The service then knows the slot of the bytes, not the job of that slot (section 4.10, item 3). Not built, and not measured on a host.
    3. The facts of a push run on 17.0.1. The runner design's open question "The event and the ref of a run" holds it. Check 3 of section 4.4 needs `event_payload.ref` of a push run. The run reply was measured on a scratch repository on 2026-11-16 (section 4.4). The runner design still says which facts the runner reads.
    Default: an agent opens one PR to the runner design for the three gaps, after the operator merges this note.
