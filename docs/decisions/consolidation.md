# Consolidation decisions

Date: 2026-11-14. Status: this file lists the decisions of the consolidation
that are taken. It adds a status column. The status says what the code and the
platform do now.

Where this document and the code disagree, the code is the fact. The
decisions that were still open, and the plan to clean the host names, paths
and accounts, are not here. The host cleanup is done. The operator's private
notes hold the open items. The CH rows (CH1 and so on) are in
`docs/design/cloud-hypervisor-playpen.md`.

Status words:

- **Holds**: the code or the platform follows the decision today.
- **Done**: the decision was a one-time change, and it happened.
- **Replaced**: a later decision changed it. The row names that decision.

## Phase 0 (2026-10-25)

| # | Decision | Reason | Status |
|---|---|---|---|
| D0.1 | TOML for every file: the family file, the server file, `install.toml`, `policy.toml` and the component manifest. | One markup lets one strict reader serve every file kind. | Holds |
| D0.2 | `install.toml` is the operator's file. The operator writes it by hand from the output of a draft script. | The writer of `policy.toml` must not be able to write the file that names launch commands and secrets. | Holds |
| D0.3 | `handover` stays frozen. The owner allows a change one pull request at a time. | It is root's release path, so a fault in it can stop every later release. | Holds |
| D0.4 | Flue runs in the sandbox, with pi as the model loop. | Flue gives the session store and the tool hooks, and it runs on pi 1.x. | Holds |
| D0.5 | Replaced by CH1 on 2026-11-01. Was: one SQLite store per family, `flue.db` in the family's sessions directory. Now: one session disk per session, and each session has its own `flue.db` on its own disk. D2.1 holds: one facade per VM. | One VM per session (CH1) makes the session disk the boundary, so no store is shared across sessions. | Replaced by CH1 (see the row) |
| D0.6 | A clean cutover outranks system data. Old conversations, memories, artifacts and indexes are dropped. | A migration for a system that is gone costs more than the data is worth. | Done |

## Phase 1: the chaperone to parity

| # | Decision | Reason | Status |
|---|---|---|---|
| D1.1 | The front door serves plain HTTP on the LAN address behind a host firewall. The install file declares it as `guard = "host-firewall"`. There is no TLS. | The guard makes the operator state the limit, and the chaperone logs it at each start. | Holds |
| D1.2 | The app ID rides in `_meta["chaperone.app"]` of each `tools/call`. | An effect plugin gets the caller's identity from the bearer, and the app cannot set the key. | Holds |
| D1.3 | An exposed tool name is `<upstream>__<tool>`, 50 characters or less. | It fits the 64-character function name limit of the model APIs. | Holds. The 50-character limit is an interface limit. |
| D1.4 | An upstream grant can carry `timeout_s` (1 to 120, default 30) and `max_inflight` (1 to 8). | A delegate call takes up to 120 seconds, and one app must not hold every slot of an upstream. | Holds |
| D1.5 | The one-hop delegate rule is a fence hook over the whole policy (`check_sections`). | The rule needs every app's `delegates` list at once, and a fence can only refuse. | Holds |
| D1.6 | A policy file that the chaperone refuses denies every app until a valid file loads. | Fail closed: no app keeps a grant that the operator did not write. | Holds |
| D1.7 | The approval channel is the `ha-approval` plugin. It has its own listener on port 8310 and serves `POST /resume`. | Any callback listener belongs to the plugin, not to the core. | Holds |
| D1.8 | A release is a `chaperone-v<version>` tag with a musl archive and a keyless cosign signature. The versions start above the last Python chaperone. 0.2.0 is the first installable version. 0.3.0 adds `--audit-mode 0640`. | Handover refuses a version below the installed one, and check P6 needs a signature. | Holds |
| D1.9 | The audit file widens from 0600 to 0640 for now. The tightening comes later. | The noticeboard and the quiet check of `door-trigger` must read it. | Holds |

## Phase 2: the agent side on Flue

| # | Decision | Reason | Status |
|---|---|---|---|
| D2.1 | One ACP facade per sandbox, and one relay per connection. | Flue needs one live process per conversation. | Replaced: one VM per session (CH1). One facade per VM. |
| D2.2 | Sessions live in Flue's database. Our own keys in the session meta start with `creche/`. | No second store of conversations exists. | Holds |
| D2.3 | The sandbox tools are Flue's six: `read`, `write`, `edit`, `bash`, `grep`, `glob`. The default grant is empty. | A family gets exactly the tools its file names. | Replaced: the tools are in contract 01 section 3.8 (`sandbox_tools`). |
| D2.4 | Codemode is mounted for every family. | Its tools are the family's chaperone tools, so it adds no reach. | Holds |
| D2.5 | No embedding endpoint replaces the `embed` verb. Index search is words only. | The vector half fails closed until a decision names an endpoint (part 2, item 4). | Holds |
| D2.6 | `door-acp` requires Authentik only on the chat UI route. The default is false. The door ships in the `attendance` bundle. | A terminal client cannot pass Authentik, and the door needs no catalog row of its own. | Holds |
| D2.7 | The door beats each relay connection. | A dead host end must not hold a connection until the facade stops. | Holds |
| D2.8 | The Flue image replaces the playpen image. It builds from the facade's lock file. | One lock file holds the pins. A second copy in `toybox/` would drift. | Replaced: the root disks of `disks/` are built from `playpen/`. |

## Phase 3: the cutover (2026-10-26)

| # | Decision | Reason | Status |
|---|---|---|---|
| D3.1 | `/etc/chaperone` is `root:agents 1775`. The caregiver writes only `policy.toml`. | The sticky bit stops the caregiver from replacing root's `install.toml`. | Holds |
| D3.2 | The draft script prints `install.toml` and the eleven manifests. The operator installs them as root. | A hand-written file drifts from the server files, and one typo stops the chaperone. | Holds |
| D3.3 | scrum-lead loses `ha_call`. `ha-call` stays uninstalled. | The Rust fence needs an `entity_id`, and scrum-lead's call has none. | Holds |
| D3.4 | The audit is 0640, and the operator account is in the group `chaperone`. | The noticeboard and the quiet check run as the operator account. | Holds |
| D3.5 | `door-trigger` posts `POST /trigger` into the job queue of `attendance`. | The queue, the limit and the outcome record exist only in `attendance`. | Holds |
| D3.6 | The session page reads the read-only routes of `attendance`. | The noticeboard does not couple to Flue's private tables. | Holds |
| D3.7 | No chaperone rename on the host. The Python unit and tree are gone. A rollback uses the Rust `.prev` tree and the hand steps. | The Python stack was removed in the window, so the margin plan of runbook part E does not apply. | Done |
| D3.8 | The caregiver released before the cutover set. The fleet was down between the two releases. | "A clean cut, not an easy cut." | Done |
| D3.9 | The operator declines the follower's full-set requests at a cutover and files one-component requests by hand. | A set that holds an unready component restores the whole set. | Holds |

## Phase 4: removal

| # | Decision | Reason | Status |
|---|---|---|---|
| D4.1 | Deleted: the Python chaperone, the JSON grant file, the playpen sources and the bridge, `toybox`, `door-owui` and `door-tui`. | Main holds only the reframed stack. | Done |
| D4.2 | The MCP-server intake retires: the roster, the `SIGHUP` reload, the intake unit, the sops-sealed secrets and the caregiver's MCP pass. A new server is a manifest, an `install.toml` entry and a chaperone restart. | The Rust chaperone reads no roster, and a `SIGHUP` ends its process. | Done |
| D4.3 | `attendance` drops the two door principals `door-owui` and `door-tui`. | Their doors are gone. | Done |
| D4.4 | Handover's quiesce step reads `GET /busy` of `attendance`. | The turn records that it read before are gone with the session API. | Holds |
| D4.5 | The keys of the Python chaperone leave the env catalog. | A catalog key with no reader hides a dead setting. | Done |
| D4.6 | `bin/fleet-up` merges as version 1. Its three decisions stay open (part 2, item 2). | The bootstrap is needed now. The decisions do not block it. | Done |

## Working rules

| # | Rule | Reason | Status |
|---|---|---|---|
| W1 | A pull request holds one idea. A review finding gets its own follow-up pull request. | A small change is easy to review and easy to revert. | Holds |
| W2 | The owner merges. The owner grants each exception in words. | One person decides what reaches `main`. | Holds |
| W3 | When the owner asks, a review finding is fixed in the same pull request. | The owner decides when W1 costs more than it gives. | Holds |
| W4 | Every document is in STE. | One reading per sentence, for a person and for a parser. | Holds |
| W5 | A comment names no pull request, no review and no date. | The code outlives the history, and the history is in git. | Holds |

## A platform family edits the platform (2026-10-28)

A platform family (named in `AGENT_PLATFORM_FAMILIES`) edits, pushes and releases the platform. It
does not land its own changes.

| # | Decision | Reason | Status |
|---|---|---|---|
| D5.1 | A platform family does not widen itself. No family gets `merge_pull_request` from `github-platform`. The operator merges each pull request on a platform repository. | `registry-sync` applies registry `main` with no tap. A merge there is a grant. | Holds. The validator refuses the grant. |
| D5.2 | The platform is four repositories: `creche`, `registry`, `mcp` and `chaperone`. The family reads each one from a host clone and edits each one in a working copy. | The chaperone is part of the product, and the family edits the product. | Holds |
| D5.3 | A deploy by a platform family is a release request through `release__request`. The phone tap is the only approval. The family runs no root step. | An agent never releases alone, and the operator approves from the phone. | Holds |

The code already holds D5.1. `family/src/agent_family/grammar.py` defines
`PLATFORM_WITHHELD = {"merge_pull_request"}`, and the validator refuses a
family that holds it. The constant ships in code, so a registry change
cannot remove it.

## The host cleanup, step 4 (2026-10-29)

| # | Decision | Reason | Status |
|---|---|---|---|
| D6.1 | Step 4 is a clean cut. Each package reads the new names only, and no reader falls back to an old name. The host moves its files in one window and releases `handover`, `attendance`, `caregiver` and `noticeboard` as one set while the fleet is down. | Fewer pull requests and fewer taps, and no second release to remove the old names. "A clean cut, not an easy cut" (D3.8). | Done |
| D6.2 | The rule "another value needs another key" of `bin/tests/test_envfile_catalog.py` stays. Each key whose default text changes takes a new name: `AGENT_PRODUCT_STATE_ROOT`, `AGENT_PRODUCT_TOKENS_DIR`, `DOOR_ACP_ACCESS_KEY_FILE`, `DOOR_ACP_BEARER_FILE`, `DOOR_ACP_SOCKET_FILE`, `DOOR_TRIGGER_FAMILY_STATUS_DIR`, `DOOR_TRIGGER_WEBHOOK_DIR`, `DOOR_TRIGGER_PRODUCT_STATE_ROOT`. | A rule with a way around it is an exception table. A site file that sets an old name is refused, so a stale override shows at once. | Done |
| D6.3 | `AGENT_MCP_STATE_ROOT` leaves the catalog. `handover` and the install draft name `/var/lib/mcp` in code. | No program read the key, and D6.2 forbids a new value under the old name. | Done |

The pull requests are la-creche/creche#277 to #282. The host steps ran in one window and the runbook is archived.

## The compose stack takes the `creche-` prefix (2026-11-03)

| # | Decision | Reason | Status |
|---|---|---|---|
| D7.1 | `ai-stack.service` becomes `creche-stack.service`. The compose project `ai-stack` becomes `creche-stack`, and each `ai-<service>` container becomes `creche-<service>`. One root window stops the old project, deploys and starts the new unit. The Postgres role `aistack` and the data under `/srv/agents/appdata` keep their names. | The operator chose the clean cut over the unit-only rename. The window recreates all seven containers, and LiteLLM is down for about 30 seconds. The data sits on bind mounts, so nothing moves. | Done |
