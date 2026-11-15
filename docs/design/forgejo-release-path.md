# The release path on Forgejo: design

> Status as of 2026-11-15: The operator decided FR1 to FR4 on 2026-11-15, all as recommended. This note proposes FR5 to FR15. Nothing is built. Next: the open questions of section 9, then PR 2 of section 8.
> Host names, addresses and accounts are placeholders. The operator's private notes hold the real values.

Source: the operator's private migration plan (section 0c, the Actions facts measured on Forgejo 17.0.1 with runner v13.2.1, and the verification appendix), its raw inventory of GitHub dependencies, and the code of the five repositories. File evidence was checked at `origin/main` on 2026-11-15: creche `96fab11c`, chaperone `a3dd242`, mcp `dadfa2b`, registry `7673e0f`, creche-release `9f0ac54`. A "measured" fact comes from section 0c of the plan. Any other fact about Forgejo is marked "not measured".

## 1. Goal and non-goals

The repositories move from GitHub to a self-hosted Forgejo 17.0.1. Wave 4 of the move is the five platform repositories: creche, mcp, chaperone, registry and creche-release. The release path of each one runs through creche code. This note designs that path on Forgejo.

Goal:

1. A merge to `main` of each of the five repositories tags and releases on Forgejo, as it does on GitHub today.
2. Root's executor on the host verifies each release against Forgejo, with checks P1 to P6.
3. The signing key of FR1 lives on the host and never in Forgejo or in a job VM. Only a job of a push to the protected `main`, of a named release workflow, gets a signature.
4. The five repositories move in one frozen window behind a bridge release (FR4).

Non-goals:

1. The repositories of the code family. They moved in wave 2 and have no release path.
2. Panda. It waits for the runner VMs of `ci-runner.md` (wave 3).
3. The agent MCP servers and their fences (the `forgejo-platform` server, `git-platform`, the bot `platform-bot`). They are a separate track.
4. The runner VMs themselves. `ci-runner.md` designs them. This note adds one device and one check to them (section 4).
5. A public transparency log for the signatures (FR12).

## 2. Decisions

The operator decided these rows on 2026-11-15:

| # | Topic | Decision | The operator's words | Date |
| - | ----- | -------- | -------------------- | ---- |
| FR1 | Signing | Cosign key-pair signing replaces keyless Sigstore. The host verifies with the public key. | "as recommended" | 2026-11-15 |
| FR2 | Trust anchor of P1 | The signing key is the only trust anchor of P1. No author check. | "as recommended" | 2026-11-15 |
| FR3 | Runners | CI runs in throwaway Cloud Hypervisor VMs on the host (`ci-runner.md`). The daemon runs as the system user `ci-runner` with a system unit. | "as recommended" | 2026-11-15 |
| FR4 | Cutover | The five repositories move in one frozen window behind a bridge release. The bridge is a release shipped from GitHub whose executor already speaks Forgejo and holds the signing public key. The current executor verifies the bridge. | "as recommended" | 2026-11-15 |

This note proposes these rows. None is decided.

| # | Topic | Proposal | Why |
| - | ----- | -------- | --- |
| FR5 | The signer | A separate program `ci-signer` holds the private key. It runs as its own system user `ci-signer`, in its own unit `creche-ci-signer.service`. It ships in the `ci-runner` component. | Each CH process runs as `ci-runner` (`ci-runner.md` section 4.7). A CH escape must not reach the key. |
| FR6 | The channel | Only a VM of a job with the label `release`, in a run that passes the checks of section 4.4, gets a vsock device. The signer serves the host end. No other VM has a path to the signer. | A vsock device is no network card. The nftables fence of `ci-runner.md` section 4.5 does not change. |
| FR7 | What is signed | One release statement per run. The signer writes the repository, the workflow file, the event, the ref, the commit and the run id from its own read of Forgejo. The job gives only the tags and the SHA-256 of each asset. | `verify-blob --key` alone binds no workflow, ref or commit. Keyless signing bound all three (`artifacts.py:332-345`). The statement puts that binding back. |
| FR8 | Where the statement travels | In the message of the annotated tag, with its bundle. The per-asset bundles `*.sigstore.json` retire. | A tag never moves. Root already reads the tag object for P2 (`provenance.py:248-257`). The follower fetches nothing new. |
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
| M2 | P1 | `provenance.py:218-233`, `BOT_LOGIN` at `:101-103` | `GET /repos/{o}/{r}/releases/tags/{tag}`. No author check. Plus the statement of the tag (section 5) | Measured: the read-back author is the user who started the run. Reply shape: not measured |
| M3 | P2 | `provenance.py:236-270` (`git/ref/tags/{tag}`, `git/tags/{sha}`) | `GET /repos/{o}/{r}/git/refs/tags/{tag}` and `GET /repos/{o}/{r}/git/tags/{sha}` | Not measured. The plan's inventory says that the refs call answers a list |
| M4 | P3 | `provenance.py:273-280`, `ON_MAIN` at `:105` | `GET /repos/{o}/{r}/compare/main...{sha}`. Pass when `total_commits` is 0 | Measured: the reply has `commits`, `files` and `total_commits`, and no `status`. The reading for an ancestor: not measured |
| M5 | P4 | `provenance.py:294-314` (`commits/{sha}/pulls`, a list) | `GET /repos/{o}/{r}/commits/{sha}/pull`, one object | Measured: 404 for a commit that is no PR merge. The 200 shape: not measured |
| M6 | P5 | `provenance.py:317-341` (`actions/runs?head_sha=`) | `GET /repos/{o}/{r}/commits/{head}/status` | Measured: the context form `<workflow> / <job> (<event>)`, and a skipped job posts `skipped`. The reply shape: not measured |
| M7 | P6 | `artifacts.py:71` (`TOKEN_ISSUER`), `:76` (`CODE_HOST`), `:82` (`WORKFLOW_DIR`), `:317-348` (`verify_words`), `provenance.py:369` (trust root), `:476` (2.5 GiB child) | `cosign verify-blob --key` on the statement. Root hashes each copy itself and compares | FR1. The exact cosign v3 flags for a key bundle with no log entry: not measured |
| M8 | Newest tag | `provenance.py:606-628` (`git/matching-refs/tags/{prefix}`) | `GET /repos/{o}/{r}/git/refs/tags/{prefix}`, then an exact match on each `ref` | Not measured |
| M9 | The API client | `provenance.py:90-94` (`api.github.com`, the API version header), `:146-176` (`Bearer`) | `https://<forgejo-host>/api/v1`, the header `Authorization: token <t>` | The API base is measured. HTTPS on the Forgejo host: not available today |
| M10 | The release token | `drain.py:76` (`RELEASE_GITHUB_TOKEN`), `:386`. `steps.py:981` | `RELEASE_FORGE_TOKEN`: a token of the bot `release-reader`, read-only collaborator on the five repositories | The plan's inventory: a Forgejo token has scopes and no list of repositories |
| M11 | The allocator | `allocate-tags.sh:183-187` (the PR read), `:200-210` (the bump labels), `:442-448` (the signature check), `:501-507` (tag push, `gh release create`) | curl to `/api/v1`: `commits/{sha}/pull`, the merged PRs with a `bump:` label, `POST releases` and one asset upload each. An annotated tag with the statement | Measured: the job token creates a Release (201) and pushes a tag. Basic auth with `x-access-token` works. Asset upload and the label filter: not measured |
| M12 | Artifact upload and download | creche `release.yml:752`, `:821`, `:953`, `:866`, `:871`, `:1002-1012`. chaperone `release.yml:254-327` | The forks of `data.forgejo.org/forgejo`, by full URL, pinned by SHA | Verification appendix: v4 and later of the upstream actions refuse Forgejo. The artifact limit for the disks (about 1 GB): not measured |
| M13 | Merge queue | creche `gate.yml:63-66` (`merge_group`), `:409` and `release.yml:486` (`GROUP_BASE`), `test_gate_workflow.py:154`, `:910-927` | FR13 | Verification appendix |
| M14 | `!retest` | `retest.yml:27` (`issue_comment`), `:40` (`author_association`), `:62-96` (`gh`). `bin/tests/test_retest_workflow.py` | FR14 | Measured: the rerun API |
| M15 | The scope read | creche `release.yml:156-168` (`gh run list` of the previous commit) | `GET /repos/{o}/{r}/commits/{before}/status`. Pass when each context `release / * (push)` is `success`. A failed read stays `code` | Not measured |
| M16 | Token scope and order | `permissions:` in each workflow. `concurrency: release-allocate` in creche `release.yml:977-979`, registry `tag.yml:38-40`, mcp `rework-component-tags.yml:47-49`, creche-release `tags.yml:41-43`. chaperone `release.yml:37-39` (group `release`) | `permissions:` is ignored, so each job has a write token. `concurrency:` stays. A race still moves no tag: the push of an existing tag fails | Measured: `permissions:` is ignored. The plan's research: `concurrency.group` is best-effort |
| M17 | The tag guard of creche-release | `.github/require-pr-merge.sh` (`gh api .../commits/$SHA/pulls`) | curl to `commits/{sha}/pull`. Check `merged`, `base.ref` is `main`, `merge_commit_sha` is the SHA | Measured: the 404 of a commit that is no merge |
| M18 | The secrets guards of creche-release | `secrets-guard.yml:40-43` (`gh api --paginate .../files`, `previous_filename`, the operator's GitHub login). `gate.yml:29-35` (the same login) | curl to `pulls/{n}/files` with `page` and `limit` until an empty page. The operator's Forgejo login | Not measured: `previous_filename`, `pull_request_target` on 17.0.1 |
| M19 | The checkout of creche at `CRECHE_REF` | chaperone `release.yml:48`, `:88-89`, `:314-315`. registry `tag.yml:54`, `:68-69` (`CRECHE_FAMILY_REF`), `:90-91`. mcp `rework-component-tags.yml:65`, `:98-99`. creche-release `tags.yml:56`, `:94-95` | `repository: <owner>/creche` resolves on the same Forgejo. A public creche needs no token | Measured: `github.api_url` names Forgejo. The visibility of creche on Forgejo: not checked |
| M20 | The git dependencies of creche-release | `pyproject.toml:41-44`: four packages from `https://github.com/<owner>/creche` | The Forgejo clone URL, then `uv lock` | The runner fence allows the Forgejo HTTP port (`ci-runner.md` section 4.5) |
| M21 | The asset download | `artifacts.py:213-228`, `corpus/artifacts.py:89` (HTTPS only), `:293` | The same path shape on `<forgejo-host>` | The plan: Forgejo uses the same path shape. HTTPS: see FR11 |
| M22 | The asset size | `catalog.py:350` (`DISK_FILE_MAX_BYTES`) | `[repository.release] FILE_MAX_SIZE` and `ALLOWED_TYPES` of Forgejo | Not measured |

## 4. Key management and the signer

### 4.1 The shape

```
Forgejo host                     the host
+---------------+   HTTPS   +--------------------------------------------+
| Forgejo 17    |<----------| ci-runner daemon (user ci-runner)          |
|  runs, jobs   |           |   reads each waiting job and its run       |
|  PRs, tags    |<--+       |   starts CH, adds --vsock for an eligible  |
+---------------+   |       |   job with the label `release`             |
                    |       +----------------------+---------------------+
                    | HTTPS                        | asks for a session
                    |       +----------------------v---------------------+
                    +-------| ci-signer (user ci-signer)                 |
                            |   checks the run again, on its own         |
                            |   holds the private key (a credential)     |
                            +----------------------^---------------------+
                                                   | vsock, one VM only
                            +----------------------+---------------------+
                            | VM of the `release` job                    |
                            |   sends tags and digests, gets statement   |
                            +--------------------------------------------+
```

The key never enters a VM. The job sends digests and gets back a signed statement.

### 4.2 Who makes the key pair, and where

The operator makes the pair once, on the host, as root, with no network step:

1. Run `cosign generate-key-pair` in a new directory with mode 0700, with `COSIGN_PASSWORD` empty. Not measured: that cosign v3 signs with a key of an empty password and no prompt.
2. Move the private key to `/etc/creche/ci-signer/release-signing.key`, owner root, mode 0400.
3. Move the public key to `/etc/creche/release-signing.pub`, owner root, mode 0644.
4. Give the public key to the agent that writes PR 5 of section 8. That PR adds it as `handover/release-signing.pub`, for the self-check of the allocator.

A password beside the key would add no protection, so the key has none. The file mode is the protection. No second copy of the private key exists. Section 4.8 says why.

### 4.3 The signer

1. `ci-signer` is a second program of the `ci-runner` component. It runs as the system user `ci-signer`, which the operator makes with one `sudo useradd`, as for `ci-runner`.
2. The unit `creche-ci-signer.service` gives it the key with `LoadCredential=`. Only the unit reads the credential. The user `ci-runner` cannot read it.
3. The signer holds its own token of the bot `release-reader`, also as a credential. It never uses the token of `runner-bot`.
4. The list of signing pairs is a file in the `ci-runner` tree: each repository name with its release workflow file. A creche test holds the list equal to `catalog.RELEASE_WORKFLOW`. The owner comes from the host config of the signer, so a rehearsal names a shadow owner with no release.
5. The signer writes one journal line for each statement: the run id, the commit and the SHA-256 of the statement.

| Repository | Workflow file |
|---|---|
| creche | `release.yml` |
| chaperone | `release.yml` |
| mcp | `rework-component-tags.yml` |
| creche-release | `tags.yml` |

registry has no row. Its component `registry-data` is `Releases.NO` (`catalog.py:173`), so root never verifies one of its tags.

### 4.4 Which run gets a signature

The label `release` takes a plain slot of R6 (`ci-runner.md` section 3). The daemon reads the run of each waiting job with that label: `GET /repos/{o}/{r}/actions/runs/{id}`. Not measured: that the entry of a waiting job names its repository and its run id. The run reply has `event`, `prettyref` and `commit_sha`. A run is eligible only when each check passes:

1. The repository and the workflow file are a pair of the list of section 4.3. The field of the run reply that names the workflow file is not measured.
2. `event` is `push`.
3. `prettyref` is `main`.
4. `GET /repos/{o}/{r}/commits/{commit_sha}/pull` answers one PR with `merged` true, `base.ref` `main` and `merge_commit_sha` equal to `commit_sha`.
5. The run is not finished.

Check 4 is not redundant with check 3. `prettyref` is a short ref name. A tag named `main` pushed from a branch could show the same `prettyref`. This reading comes from the code of Forgejo and is not measured. Check 4 refuses that run, because its commit is no merge into `main`. Tag protection of the name `main` is a second layer (section 6).

The daemon then asks the signer to open a session for that run. The signer does not trust the daemon. It reads the run and the PR itself with its own token and runs the same five checks. Only then does it serve the host end of the vsock device of that one VM.

### 4.5 The exchange

1. The allocator in the `release` job plans its tags. It knows each tag and each asset before it pushes a tag.
2. It sends one request over vsock: a list of tags, each with its assets as a name and a SHA-256. A `venv` or `units` tag has no asset.
3. The signer refuses a request that breaks a limit: at most 32 tags, at most 16 assets for each tag, a tag of the form `<component>-vX.Y.Z`, a plain asset name, 64 lower-case hex digits.
4. The signer builds the statement. It writes its own fields from section 4.4 and the tags of the request. It signs the bytes with `cosign sign-blob --key`, with no log entry.
5. The signer answers the statement and its bundle. It serves one statement for each run id. A second request of the same run is refused.
6. The allocator checks the bundle with `handover/release-signing.pub` before it pushes a tag. A check that fails tags nothing.

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

The allocator writes each tag of the run as an annotated tag. Its message has three lines: the word `release-statement-1`, the statement in base64, and the bundle in base64. Base64 has no character that `git tag --cleanup=verbatim` changes.

### 4.6 What a branch run gets

1. A run of a branch push, of a PR or of `workflow_dispatch` fails check 2 or check 3. Its VM gets no vsock device, also when the job asks for the label `release`.
2. Its allocator gets no statement. It tags no component of `Releases.YES`, and the run ends red.
3. Its job token can still create a tag and a Release (measured). Root refuses that release at P1, because no statement names the tag at its commit.

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
| The daemon itself signs | Each CH process runs as `ci-runner`. A CH escape would read the key. |
| The daemon signs after the VM exits | The job is over. The daemon would then need a write token to attach the signature. |
| A private Sigstore with Forgejo as OIDC issuer | More services, and Forgejo's OIDC token is also given to every branch run. |

### 4.10 What stays open

1. A CH escape gives code as `ci-runner`. That code can talk to an open session of a real release run and ask for its statement first. The real run then gets none and ends red, which the operator sees. The escape cannot read the key or open a session for another run.
2. A malicious change that a PR merges into a release workflow gets signed. That is row 10 of the threat table in creche-release `docs/releases.md` section 6, as today.

## 5. Provenance on Forgejo

Each call goes to `https://<forgejo-host>/api/v1`, with `Authorization: token <release-reader token>`. Each answer is strict JSON with a byte cap, as today. Each check fails closed.

| Check | Call | Fields read | Rule | Measured |
|---|---|---|---|---|
| P1 | `GET /repos/{o}/{r}/releases/tags/{tag}` | `tag_name`, `draft`, `prerelease` | The Release exists, names the tag, is no draft and no prerelease. No author check (FR2) | The author read-back only |
| P1 | the tag object of P2 | `message` | The message holds a statement and a bundle. The key of FR10 passes the bundle. The statement names the repository of the catalog row, its workflow of `RELEASE_WORKFLOW`, `event` `push`, `ref` `main`, the resolved SHA, and the tag | No |
| P2 | `GET /repos/{o}/{r}/git/refs/tags/{tag}`, then `GET /repos/{o}/{r}/git/tags/{sha}` | each `ref`, `object.type`, `object.sha` | One entry has `ref` equal to `refs/tags/{tag}`. Its object is an annotated tag. That tag names a commit equal to the resolved SHA. A lightweight tag is refused | No |
| P3 | `GET /repos/{o}/{r}/compare/main...{sha}` | `total_commits`, `commits` | `total_commits` is 0 and `commits` is empty | The shape. Not the reading for an ancestor |
| P4 | `GET /repos/{o}/{r}/commits/{sha}/pull` | `number`, `merged`, `base.ref`, `merge_commit_sha`, `head.sha` | A 404 refuses. The PR is merged into `main` with `merge_commit_sha` equal to the SHA | The 404 only |
| P5 | `GET /repos/{o}/{r}/commits/{head.sha}/status` | `total_count`, each `context` and its state | Each required context of section 6 for the repository is `success`. Each other context is `success` or `skipped`. A count above one page refuses | The context form, and `skipped` |
| P6 | none: root's copy and the statement of P1 | the SHA-256 of each copy | Each asset of the tag in the statement has a copy with the same SHA-256. The binary and disk-image rules after the signature stay (`provenance.py:483-549`) | No API |

P1 runs its statement check through child 2 of today, with no network (`provenance.py:560-583`). The words are `verify-blob --key /etc/creche/release-signing.pub --bundle <bundle> <statement>`, plus the flags that skip the log, which are not measured. Root writes the statement and its bundle into its own work tree from the tag message. Root parses the statement only after the child passes. The statement has a closed key set and a byte cap. `ReleaseStatement` is the one type that holds a parsed statement, and only the check makes one, as `VerifiedArtifact` today.

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
5. The rule also binds admins. The name of that field in the API of 17.0.1 is not measured.

| Repository | Required contexts |
|---|---|
| creche | `gate / gate (pull_request)` |
| mcp | `gate / gate (pull_request)` |
| chaperone | `ci / gate (pull_request)` |
| registry | `validate / validate (pull_request)`, `validate / reach (pull_request)` |
| creche-release | `gate / gate (pull_request)`, `secrets-guard / guard (pull_request_target)` |

Each name is `<workflow name> / <job name> (<event>)` from the workflow files at the commits of the source line. A change to a workflow name or a job name needs the same change in the rule.

Tag protection (`/repos/{o}/{r}/tag_protections`):

1. Protect the name pattern of release tags, `*-v*`. The allowlist holds the Actions user only, if Forgejo allows it. That is not measured.
2. Protect the name `main` with an empty allowlist. It closes the short ref of section 4.4 as a second layer.

Tag protection cannot tell a run of `main` from a branch run. Each job token is the same user `forgejo-actions` (measured). So a branch run can still make a release tag. The signature is the guard (section 4.6). Tag protection only keeps the bot users and a hand push away from release tags.

## 7. The bridge release and the window

Before the window:

1. **The rehearsal.** An agent imports the five repositories into a shadow owner on Forgejo. The ported branches of section 8 run there: a full gate and a full release of each repository, with the signer on the shadow owner. An agent runs P1 to P6 from a dev checkout of the bridge branch against the shadow releases, never on the host. It records each Forgejo reply that section 5 reads, for the tests. It proves each refusal: a branch push, a PR, a `workflow_dispatch` on `main`, a tag named `main`, a Release with no statement, a statement of another commit. Rollback point: delete the shadow owner.
2. **The key** (the operator). Section 4.2. Rollback point: delete the files. Nothing reads them yet.
3. **The bot** (the operator). Make `release-reader` as a read-only collaborator of the five repositories, and two tokens. Rollback point: delete the bot.
4. **The site and the secret** (the operator opens the creche-release PR, because `secrets-guard` allows only the operator). Add `AGENT_FORGE_HOST`, `AGENT_FORGE_OWNER` and `RELEASE_FORGE_TOKEN`. The operator taps the `host` release. The current executor ignores the new keys. Rollback point: a revert PR and a tap.
5. **HTTPS on the Forgejo host** (the operator, question 4). Rollback point: none needed. Nothing uses it yet.
6. **Every pending GitHub tag installs or is dropped** (the operator). After the bridge, root refuses each GitHub Release (FR15).

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
2. **The signer** (creche, before, after PR 6 of `ci-runner.md` section 8). `ci-signer`, its unit, the vsock device on an eligible `release` job, the checks of section 4.4 in the daemon and in the signer, and the list test of section 4.3. Proof: unit tests against Forgejo replies recorded in the rehearsal refuse each failed check. The `kvm` CI job boots one VM with the device and one without, and only the first gets a statement.
3. **The site keys and the secret** (creche-release, before, opened by the operator). Proof: the `host` release installs, and the current executor still passes a release.
4. **The bridge executor** (creche, window step 2, merges on GitHub). The API client of section 5, P1 to P6 on Forgejo, `ReleaseStatement`, the key file of FR10, the site keys of FR11. It removes the GitHub client. Proof: tests against the recorded replies. The dev checkout run of rehearsal step 1 passes for one tag of each kind and refuses each attack of that step.
5. **creche releases from Forgejo** (creche, window step 5). The allocator on `/api/v1` with the statement and the annotated tag, `release.yml` with the label `release` and no job `sign`, the artifact forks, the runner labels, `gate.yml` with no `merge_group`, the removal of `retest.yml` and its test, the tests that pin each workflow and the allocator. It also removes the unread keys of `env/src/creche_env/default.env:32-35` and fixes `steps.py:999`. Proof: its own run on `main` tags `handover`, and the host installs it.
6. **registry on Forgejo** (registry, window). `CRECHE_REF`, `CRECHE_FAMILY_REF` and the runner labels. Proof: its run on `main` tags `registry-data`.
7. **mcp on Forgejo** (mcp, window). `CRECHE_REF`, the label `release`, the runner labels, the rerun line of `README.md` and `CONTRIBUTING.md`. Proof: the host installs the next `mcp-servers` tag, with P3 to P5 on the registry commit of PR 6.
8. **chaperone on Forgejo** (chaperone, window). `CRECHE_REF`, no job `sign`, the label `release`, the artifact forks, `tests/release.rs`, `docs/design.md` section 12 (`:616-618`). Proof: the host installs the next `chaperone` tag, and P6 passes.
9. **creche-release on Forgejo** (creche-release, window). `CRECHE_REF`, the label `release`, `require-pr-merge.sh`, both secrets guards, the git dependencies and `uv.lock`, the old site keys out, and the contract text: contract 06 section 3.4 and section 8.4, and `docs/releases.md` section 2.4 step 3 and section 6 rows 1, 2 and 22. Proof: the host installs the next `host` tag.

## 9. Open questions for the operator

1. **Tag protection and the Actions user.** Not measured: whether `forgejo-actions` (id -2) can be on the allowlist of a tag protection. Default: measure it in the rehearsal. If it cannot, protect only the name `main`, and let the signature guard release tags (section 6).
2. **FR10 against FR4.** FR4 says that the bridge executor "holds" the public key. FR10 reads that as: the bridge reads a root-owned key file outside every release. Default: FR10. The other way puts the key in the `handover` tree: a rotation is then a release, and a loss needs a by-hand root step.
3. **FR9: a statement for every kind.** FR2 makes the key the only anchor of P1. A `venv` or `units` component has no signed asset, so without FR9 its P1 has no anchor. A tag on an old `main` commit could then name a new version. Default: FR9, so mcp and creche-release also use the signer.
4. **HTTPS on the Forgejo host.** Root and the follower refuse plain HTTP. The API of the Forgejo host is plain HTTP today. Who gives it a certificate that the host's CA store trusts?
5. **Visibility of creche and chaperone on Forgejo.** The follower downloads assets with no token, and `CRECHE_REF` checkouts use no token. A private repository needs a token for each. Default: both public, as today on GitHub.
6. **Two new accounts.** The system user `ci-signer` on the host and the bot `release-reader` on Forgejo. Default: yes.
7. **Who merges.** The merge allowlist holds the operator's account only, on all five repositories. An agent that merges its own creche PR does it as that account today. Default: keep it so.
