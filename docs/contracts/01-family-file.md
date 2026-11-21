# Contract 01 — the family file

Status: draft 21
Version: 0.19.0

Minor 14 makes `quiet.daily.zone` optional and not read. Minor 15 refuses the
field. Minor 16 adds `repos` (§3.7.1), the proxy rules of §3.7 and the two
reach files (§6.1.1). Minor 17 adds the flavor `coding` to `sandbox.image`
(§3.9). Minor 18 drops `files`, the allowed roots and the mount half of the
platform fence (§3.3, §5.4, §5.5). No VM keeps a folder between jobs, so
`job` names no caller root (§3.12). A shape change reaches a VM at its next
start (§6). Minor 19 drops `sandbox.max_resident_processes`. The vmm admits
each VM by its guest RAM cap and its slot pool (§3.9).

Amended on 2026-11-08 by decisions CH1, CH10-10, CH21-2 and CH82 rules 7
and 8 of the design note named in §6.1.

Amended on 2026-11-08 by decisions CH8-6 and CH58 rule 2 of the same design
note.

Amended on 2026-11-15: §5.5 names the Forgejo platform server and token
beside the GitHub ones.

Amended on 2026-11-15: §5.5 rule 4 names `code-worker`, which took the
non-platform coding reach when the attended `code` family was deleted
(registry#90).

`docs/invariants.md` lists each rule of this contract that the code does
not hold yet.

## 1. Where the file lives

```
registry/
  families/<name>/family.toml       this contract
  families/<name>/instructions.md   prose only, no permission
  mcp/<name>/server.toml            contract 01b
  skills/<name>/SKILL.md
```

Rules:

1. The directory name equals the `name` field. A mismatch is an error.
2. The file never names the instructions path. The layout fixes it. Prose can
   therefore never widen reach, which is what invariant 7 needs.
3. The file is a TOML file. Its limits are those of
   a family file. One document and a table at the top level hold by the
   grammar.

## 2. Top-level fields

| Field | Type | Default | Kinds | Rule |
|---|---|---|---|---|
| `name` | string | required | all | `[a-z][a-z0-9-]{1,30}`, equals the directory name, never `gate-probe` (§3.1) |
| `kind` | enum | required | all | `attended`, `thin` or `autonomous` |
| `description` | string | required | all | one line, 1 to 200 characters after whitespace collapse |
| `model` | table | required | all | §3.2 |
| `tools` | table | `{}` | all | §3.4 |
| `verbs` | table | `{}` | all | §3.5 |
| `delegates` | array | `[]` | all | §3.6 |
| `max_inflight_delegations` | integer | `2` | all | §3.6.1 |
| `egress` | array | `[]` | all | §3.7 |
| `repos` | array | `[]` | all | §3.7.1 |
| `shell` | boolean | `false` | all | §3.8 |
| `sandbox_tools` | array | `["read", "grep", "find", "ls"]` | all | §3.8 |
| `system_prompt` | enum | `append` | all | §3.8 |
| `sandbox` | table | `{ cpus = 2, memory = "2g", image = "base" }` | all | §3.9 |
| `skills` | array | `[]` | all | §3.10 |
| `approval` | array | `[]` | all | §3.11 |
| `job` | table | `{ timeout = "120s" }` | thin only | §3.12 |
| `triggers` | array | required | autonomous only | §3.13 |
| `max_running_turns` | integer | `1` | autonomous only | §3.14 |
| `quiet` | table | none | autonomous only | §3.15 |

Each type is the TOML type. An `enum` and a `duration` are TOML strings, and
a `number` is a TOML integer or a TOML float. A value of another type is an
error.

A field marked "thin only" or "autonomous only" is an error on any other kind.
The reader names the kind in the message, so the fix is one edit.

There is no `limits` block in v1. The chaperone's rate limit and its open-gate limit
are guards against a runaway loop, not permissions, and no family differs
on them. `caregiver` writes its own defaults for those two into the
grant file (contract 04 §1.2). Adding either here is a change to this contract,
not a field to invent in another.

The third guard, the in-flight delegation cap, IS a field of this file
(§3.6.1). It is the one of the three that a family legitimately differs on: a
caller that delegates as its normal way of working needs more of them at once
than a caller that delegates once an hour. It is a bare top-level field and not
a `limits` mapping, so `limits` keeps one meaning — the three numbers the chaperone
enforces, in the grant file (contract 04 §1.2) — rather than naming a one-key
subset here and a three-key mapping there.

## 3. Field detail

### 3.1 `name`, `kind`, `description`

`name` and `kind` are immutable after the first successful apply.

- A `name` change is a new family. The old one is deleted, key and token first
  (invariant 13).
- A `kind` change is refused. The three kinds differ in session lifetime
  (§4), and live sessions were created under the old rule. Delete
  the family and create it again.

`description` rides in the one view and in the catalog. It is not a prompt.

**One name is reserved: `gate-probe`.** The validator refuses a family that
takes it (§7). `bin/rework-gate-1b.sh` (git history only, commit `5d2d85d`)
proved invariant 9 in its preflight by
writing a grant file under that name, calling the chaperone, deleting the file and
calling again. Run under a real family's name, that probe overwrites the
family's grants, then revokes them, and leaves the chaperone holding a
turn-blocking `grants_stale` on a family that was serving a moment earlier.
The grammar accepts the name, so only this
reservation keeps it free. `agent_family.grammar.PROBE_FAMILY` is the one
spelling; a script that probes uses it and no other name.

### 3.2 `model`

| Field | Type | Default | Rule |
|---|---|---|---|
| `router` | string | required | one LiteLLM alias, `[a-z0-9][a-z0-9._/-]*`, no `*` |
| `budget_usd_per_day` | number | required | greater than 0, at most 500 |
| `context_window` | integer | `200000` | from 65536 to 2097152 tokens |

One alias, not a list. The family key is minted with `models: [router]`, so the
sandbox can reach that alias and nothing else. A wildcard alias is ungrantable:
it would hand the family every model LiteLLM serves.

`budget_usd_per_day` becomes the family key's `max_budget` with a daily
`budget_duration`. The ceiling of 500 refuses a typed extra
zero here rather than on a bill.

`context_window` caps the context of the family's agent. The agent uses the
smaller of this value and the window LiteLLM gives for the alias. The agent
compacts its context when the context passes 90% of that window, and at the
first prompt after one hour without a turn. Each model call sends the whole
context, so the cap bounds the cost of one call. The default of 200000 keeps
one call cheap. A family that needs more history in view sets a larger cap.

```toml
model.router = "agent-router"
model.budget_usd_per_day = 15
model.context_window = 400000
```

### 3.3 `files`

Minor 18 drops the field and its six rules (decision CH10-10). A VM mounts
no host folder. Each session has a disk of its own, and a thin job's VM is
dropped at job end (decision CH1). A family reads code only through the
clones of `repos` (§3.7.1), and the vault only through `mcp-memory`
(decision CH4d). `files` is now an unknown field, and an unknown field is an
error.

### 3.4 `tools`

A mapping of MCP server name to the tools granted from it.

| Key | Value | Rule |
|---|---|---|
| MCP server name | array of tool names, or `all` | the server is declared under `mcp/<name>/server.toml` |

Rules:

1. A tool name matches `[A-Za-z][A-Za-z0-9_-]{0,63}` and is declared in that
   server's file. So a tool name has 1 to 64 bytes. An undeclared tool is an
   error, not a silent no-op.
2. An empty list is an error. Write no key instead.
3. Duplicate tool names are an error.
4. `all` means every tool the server file declares **at read time**. Adding a
   tool to a server file therefore widens every family that says `all`.
   Contract 01b §9 makes that widening visible in the review of the server
   file.
5. `all` is refused for an `autonomous` family. Invariant 8 gives autonomous
   families a narrow reach, and `all` is a reach that changes without the
   family file changing.
6. The chaperone call name is `<server>__<tool>` (double underscore). That is why a
   server name carries hyphens and never an underscore.

```toml
tools.kagi = ["kagi_search_fetch", "kagi_extract"]
tools.vikunja-finance = "all"
```

### 3.5 `verbs`

The chaperone's own verbs, which are not MCP tools. A mapping of verb name to its
argument fence.

| Verb | Fence | Meaning |
|---|---|---|
| `embed` | `{}` | text to vector at the local embedding service. The semantic half of `index_search` (contract 03 §7.3 rule 6) |
| `ha_call` | `allow:` list of `{domain, service, entity_id}` | one Home Assistant service call |
| `enqueue` | `targets:` list of family names | start a session in an autonomous family |
| `job_status` | `{}` | read the outcome records of sessions this family enqueued |
| `release` | `components:` list | run a platform component release |

Rules:

1. An unknown verb name is an error. The names above are v1 and this contract
   owns them. A new verb arrives with a new version of this contract and a chaperone
   release. Contract 04 §4.1 holds each verb's argument schema and serves it in
   the manifest, so a schema change is a reviewed contract change too. This
   contract names the verbs and their fences. It never restates a schema.
2. `ha_call.allow` holds at least one triple. `domain` and `service` match
   `[a-z][a-z0-9_]*`. `entity_id` is `domain.object_id` or absent, and absent
   allows only a call that targets no entity, such as `notify.*`.
3. `enqueue.targets` names families that exist and are `autonomous`. A target
   of another kind is an error.
4. `job_status` without `enqueue` is an error. Its scope is "sessions this
   family enqueued", so alone it can never match a record.
5. `release` is fenced to the platform families (§5.5). Its `components` are
   the names contract 06 defines.
6. `invoke_agent` is not a verb here. The chaperone synthesizes it from `delegates`
   (§3.6), so the file has one source for who may be called.

```toml
verbs.embed = {}
verbs.ha_call.allow = [
    { domain = "notify", service = "mobile_app_phone" },
]
verbs.enqueue.targets = ["scrum-lead"]
```

### 3.6 `delegates`

A list of thin family names this family may call through the chaperone's
`invoke_agent` (contract 04 §7).

Rules:

1. Each name exists and its `kind` is `thin`. Anything else is an error.
2. A family may not name itself.
3. Duplicates are an error.
4. There is no depth field. A thin family's `delegates` must be empty, so a
   chain is at most one hop and cannot loop. Invariant 15 wants a thin agent to
   leave nothing but an audit record, and a thin agent that delegates would
   leave a chain to reason about.

### 3.6.1 `max_inflight_delegations`

| Field | Type | Default | Rule |
|---|---|---|---|
| `max_inflight_delegations` | integer | `2` | 1 to 8. How many of this family's delegate calls may run at once |

How many `invoke_agent` calls this family may hold in flight at one time. The
The chaperone counts the calls it is running for the family and refuses the next one
`rate_limited` (contract 04 §5 row 7). It is a guard against a runaway loop,
not a permission: it bounds what is in flight, never how many calls a session
may make over its life.

The cap is the CALLER's, never the target's. A thin family serves every caller
that may reach it, and it has no turn limit and one sandbox (§4), so the number
that decides whether a third call runs is the number in the calling family's
own file.

Rules:

1. It sits beside `delegates` because it means nothing without it: the list
   says who may be called, this says how many at once.
2. It is a top-level field, not a `limits` mapping (§2). The grant file's
   `limits` holds the three numbers the chaperone enforces, and one name with two
   key sets has two meanings.
3. A value other than the default with an empty `delegates` is a WARNING, not
   an error. A family with no delegate has no call to bound, so the number
   changes nothing. A warning never blocks an apply (§7 rule 4), and an error
   would make emptying `delegates` fail until the cap were removed in the same
   edit.
4. The upper bound is 8, the same ceiling as `max_running_turns` (§3.14).
   Each in-flight call is a job of the TARGET family, and each job runs in
   a VM of its own (decision CH1). So the bound caps how many VMs one caller
   starts at once. The vmm admits each VM (§3.9). A call that finds no room
   returns `host_full` to the caller, at the latest when its `job.timeout`
   ends (decision CH21-3). A number past what the host can hold costs
   latency, not correctness.
5. It applies LIVE, with no sandbox replacement (§6). `caregiver` copies it into
   the grant file, and the chaperone re-reads that file per call (contract 04 §1.4).
   Invariant 16 asks that a change to what a family can do never needs a second
   edit elsewhere, and invariant 9 asks that it needs one action and no manual
   apply. Both hold here.

```toml
delegates = ["vault-oracle", "code-sandbox"]
max_inflight_delegations = 3
```

### 3.7 `egress`

Hostnames the sandbox may reach directly, each `hostname` or `hostname:port`.

Rules:

1. No IP literal. An egress list can then never quietly reach a LAN host.
2. No wildcard. `*.github.com` is ungrantable.
3. A port, when present, is 1 to 65535.
4. The two plane endpoints are implicit and are never listed: the host and
   the port of `AGENT_LITELLM_BASE_URL` and of `AGENT_PEP_URL`.
5. An empty list is the normal case for an attended chat family. All its reach
   then goes through the chaperone, where every call is audited (invariant 15).
6. **The vmm bridges both plane endpoints on every VM, outside this list,
   and then proves deny-by-default** (decisions CH6-2, CH6-3, CH6-5). A VM
   has no network card. The vmm splices vsock ports 1101 and 1102 to
   LiteLLM and the chaperone with no allowlist lookup. At each VM start and after
   each egress edit, the vmm checks that the Cloud Hypervisor command line
   has no `--net` and that the VM has exactly the vsock listeners the vmm
   bound. A failed check is a hole in deny-by-default (invariant 11), so the
   vmm raises `egress_assert_failed` and stops the VM (contract 05 §4.3). The
   plane addresses are the vmm's own config, never a registry field: rule 1
   refuses an IP literal, so a family file could not name them anyway.

Rules 7 to 11 hold for a family that runs on the vmm (§6.1). Its egress proxy
enforces them (decisions CH6-4 and CH69). The proxy reads the list from
`egress.json` (§6.1.1), not from this file.

7. **A bare host means port 443 only.** `github.com` grants
   `github.com:443` and no other port.
8. **CONNECT only, to exactly a listed host and port** (CH69 rule 7). Any
   other method gets 405 and a close. The proxy never looks at the tunnel bytes and holds no CA, so a
   listed port other than 443 carries any protocol inside the tunnel.
9. **The proxy refuses an IP literal target**, IPv4, IPv6 or bracketed IPv6.
   Rule 1 keeps literals out of the list. This rule keeps them out of the
   request.
10. **A name with any record that is not public is refused whole** (CH69 rule
    8). The proxy resolves with the host resolver (getaddrinfo). Not public
    means every IPv4 special-purpose range of RFC 6890, plus
    `100.64.0.0/10`, `0.0.0.0/8`, `224.0.0.0/4` and `240.0.0.0/4`. For IPv6
    only `2000::/3` is public, minus `2001:db8::/32` and the `2001::/23`
    special block. An IPv4-mapped address (`::ffff:0:0/96`) is judged as
    IPv4. The proxy connects only to the checked addresses, in order, within
    one 5 s wait. One private record is enough evidence of a rebinding name,
    so a public name with one private record is refused.
11. **A removal refuses new connections at once and leaves open ones to
    finish** (CH6-4, design note fact 43). The proxy reads `egress.json` on
    every CONNECT, so the next CONNECT to a removed host is refused. A tunnel
    that is already open runs until it closes.

### 3.7.1 `repos`

The repos of the host code mirror that the sandbox may clone, each a bare
mirror directory name (decisions CH3, CH5f, CH10-10 and CH69 rule 15).

```toml
repos = ["mcp", "creche"]
```

Rules:

1. An entry matches `[A-Za-z0-9][A-Za-z0-9._-]{0,99}`, holds no `..` and does
   not end in `.git`. It names the mirror directory `/srv/agents/code/<name>`
   (design note fact 13). It is not the `owner/name` form of a server file:
   the mirror keys its repos by name.
2. Duplicate entries are an error.
3. At most 32 entries. An empty or absent list is allowed and grants no
   clone.
4. Every kind may hold `repos`, because CH5f gives clone-only reads to
   families with no push tool. No host fence checks the list (CH10-10).
   the operator's registry review is the guard.
5. **The vmm serves a repo only when it is in `repos` and in the mirror**
   (CH10-10). The validator does not check the mirror. The vmm checks at
   serve, and a repo in `repos` but not in the mirror gets the same refusal
   as a repo outside `repos` (CH10-10, CH69 rule 16).
6. **Clone only.** The vmm serves `git-upload-pack` and refuses every other
   git command (CH46 rule 4), so no push takes this path. A family that pushes uses
   `push_files` (CH3).
7. The vmm reads the list from `repos.json` (§6.1.1), not from this file.
   `repos` replaces `files` and the host key `AGENT_MOUNT_ROOTS` (decision
   CH10-10, §3.3).

### 3.8 `shell`, `sandbox_tools` and `system_prompt`

| Field | Type | Default | Rule |
|---|---|---|---|
| `shell` | boolean | `false` | `true` gives the agent process a shell tool |
| `sandbox_tools` | array | `["read", "grep", "find", "ls"]` | subset of `read`, `write`, `edit`, `grep`, `find`, `ls`, `codemode` |
| `system_prompt` | enum | `append` | `append` or `replace`: how `instructions.md` meets pi's own system prompt |

Rules:

1. `bash` may not appear in `sandbox_tools`. `shell` is the only way to get a
   shell, so one field answers "can it run commands".
2. An empty `sandbox_tools` list is valid. `vault-oracle` could run with none.
3. No field here is part of the perimeter. Invariant 12 names the perimeter:
   mounts, egress, chaperone and the model key. These fields shape what the agent
   process is offered. A `write` tool can still only write the disk of the
   session's own VM (§3.3). This contract states it once, here: a wrong
   value costs behaviour, not reach.
4. All three sit in `runtime.json` of the family config mount (§6.1).
   `caregiver` writes the file. None travels on an ACP line. The facade
   gets `sandbox_tools` and `system_prompt` on the family-state pull before
   each prompt, so a change reaches the next prompt of a running session
   (decision CH5c). The pull carries no `shell` (`FamilyConfig` of
   `creche_wire`), so `shell` reaches no VM. `system_prompt` is written
   only when it is `replace`: the file is compared byte for byte, so a key
   every family carried would rewrite every family's mount at once. A
   missing key reads as `append`.
5. **`sandbox_tools` governs pi's built-in tools and nothing else.** It can grant seven: `read`, `write`, `edit`, `bash`, `grep`,
   `find` and `ls`. pi's eighth, `powershell`, is in no grant, so the
   playpen always denies it. The field says nothing about chaperone tools: those come from the
   family's grant and the chaperone enforces them, outside the sandbox
   (invariant 12). So the two lists never have to agree, and a new granted tool
   never needs an edit here. It says nothing about `index_search` either: an
   index mount offered that tool, and minor 18 drops mounts (§3.3).
6. It is rendered as an ENABLE list and a DENY list, both, because pi needs
   both. The playpen writes the granted built-ins into `defaultTools` in
   the `settings.json` pi reads from `PI_CODING_AGENT_DIR`, and it passes pi
   `--exclude-tools` with the built-ins this field does not name. It never
   passes `--tools`, `--no-tools` or `--no-builtin-tools`: the first two are
   allowlists over built-in AND extension tools alike, all three act before
   the chaperone bridge has read the manifest, so each would silently drop every
   granted tool (contract 03 §7.1).
7. **`codemode` is pi's built-in extension, granted by name.** It runs
   model-written JavaScript that calls the other tools this family holds, in
   parallel if it likes, and only the script's output reaches the model. It
   adds no reach: a script calls only what the model could call, every chaperone
   call is still decided and audited, and a gated tool is out of every
   script (contract 03 §7.3 rule 7). The playpen loads it with
   `--extension builtin:codemode` and lists it in `defaultTools`. No field
   can load pi's built-in `mcp` extension: a sandbox reaches MCP servers only
   through the chaperone.
8. **`system_prompt: replace` drops pi's own prompt.** pi's prompt opens with
   "You are an expert coding assistant", then lists coding rules and pi's own
   docs. `append` keeps it and adds `instructions.md` after it, which suits a
   coding family. `replace` makes `instructions.md` the whole prompt, which
   suits a chat. The playpen passes the file with `--system-prompt` or
   `--append-system-prompt` accordingly.

### 3.9 `sandbox`

| Field | Type | Default | Rule |
|---|---|---|---|
| `cpus` | integer | `2` | 1 to 8 |
| `memory` | string | `2g` | `[1-9][0-9]*[mg]`, 256m to 16g |
| `image` | enum | `base` | `base`, `python` or `coding`. Which flavor the platform builds this sandbox from |

`image` names a FLAVOR, never an image reference. The set is closed and lives
in code beside the validator's other constants, released with `caregiver`:
invariant 10 says only the platform changes the platform, and a registry file
that could name an image would choose what code runs inside the microVM. An
unknown value is an error that names the whole set, so the fix is one edit.

| flavor | `playpen/Dockerfile` target | what it adds |
|---|---|---|
| `base` | `agent-sandbox` | Node, ripgrep and the ACP facade |
| `python` | `agent-sandbox-python` | a venv with pandas, numpy and matplotlib, on top of `base` |
| `coding` | `agent-sandbox-coding` | uv, pnpm through corepack, and cargo with Rust 1.92.0, rustfmt and clippy, on top of `python` |

`coding` is the flavor for `code` and `creche`, for now (decision CH7-1). It
alone carries installers. An agent installs the locked dependencies of a
repo through the egress proxy, then runs the checks of the repo.

Each flavor is a root disk of the `disks` row (contract 06 §1 rule 15).
`digests.json` of that row names one root disk for each flavor, and the vmm
refuses to start on a `digests.json` that lacks one (decision CH58 rule 4).
The vmm starts a VM of the family from the root disk of the flavor in
`shape.json` (§6.1 rule 6). The platform never falls back to `base`: a
family running on a filesystem its file does not ask for is the failure
nobody goes looking for.

Minor 19 drops `max_resident_processes` (decision CH8-6). The family file
holds no admission number. Each session runs in a VM of its own (decision
CH1), and the vmm admits each VM start by host limits only. Two env keys of
the vmm set the limits: `VMM_RAM_CAP_GIB` (default 80) caps the configured
guest RAM of the running VMs (decision CH8-6). `VMM_FREE_FLOOR_GIB`
(default 50) is the free-space floor of the state root (decision CH8-7).
Each running VM also holds one slot of the vmm's slot pool. The vmm
decides each start by these rules, in order:

1. The free bytes of the state root are below the floor. The start gets
   `host_full`, even with idle VMs.
2. The guest RAM of the running VMs plus the RAM of the start stays at or
   below the cap, and a slot is free. The vmm starts the VM.
3. Else the vmm evicts the shortest prefix of the idle VMs, least recently
   used first, that frees enough RAM. A full slot pool counts like a full
   RAM cap, so a full pool evicts one VM at least (decision CH58 rule 2).
   An eviction can stop several VMs.
4. Each eviction snapshot writes the whole guest RAM of its VM. When the
   free bytes minus those writes fall below the floor, the start gets
   `host_full` and no VM stops (decision CH42 rule 3).
5. Else no prefix of the idle VMs frees enough, and the start gets
   `host_full` at once. The vmm never queues (decision CH21-3).

The vmm evicts the VMs of the prefix one at a time. A VM with a turn
running when its eviction comes goes back to the table. Then the start
gets `host_full`, and the VMs left in the prefix go back too. An attended
session gets `host_full`. A dispatch job retries through `attendance`'s
job queue, and a delegate job retries inside its `job.timeout` (decisions
CH8-6 and CH21-3). `max_resident_processes` is now an unknown field, and
an unknown field is an error.

### 3.10 `skills`

A list of skill names. Each exists as `skills/<name>/SKILL.md`. Duplicates are
an error. `caregiver` copies the granted skills into the family config mount
(§6.1), and the agent process reads them at turn start.

### 3.11 `approval`

A list of granted actions that wait for the operator's phone tap before they run
(contract 04 §8).

Entry grammar:

| Form | Means |
|---|---|
| `<verb>` | that verb, for example `release` or `enqueue` |
| `<server>__<tool>` | one MCP tool |
| `<server>__*` | every tool granted from that server |
| `invoke_agent` | every delegate call this family may make |

Rules:

1. Every entry names something this file grants. An entry for an ungranted
   tool is an error, because it reads as protection that does not exist.
2. Duplicates and overlaps are an error. `ha__*` plus
   `ha__ha_call_service` is one of them.
3. `approval` is not restricted to autonomous families: an attended family may
   gate any tool or verb it holds. `release` is the one verb that needs no
   entry here. Invariant 10 requires a phone approval for a platform release,
   and the release executor asks for it itself, bound to what will run. Listing `release` as well costs the operator a second
   tap that binds to nothing, so the locked fleet does not.
4. An approval entry only narrows. Adding one can never widen reach, so it
   applies live like any other grant change (§6).
5. `invoke_agent` is the fourth form and it is not a verb (§3.5 rule 6). It is
   legal exactly when `delegates` is non-empty, and it gates every delegate
   call the family may make. Contract 04 §7.2 reads it this way already:
   "row 9 when `invoke_agent` is in `approval`". There is no per-delegate
   form, because `delegates` is the one source for who may be called (§3.6).

### 3.12 `job` — thin only

| Field | Type | Default | Rule |
|---|---|---|---|
| `timeout` | duration | `120s` | `[1-9][0-9]*[smh]`, 1s to 1h |

The limit is wall-clock from the start of the job's single turn. Past it,
`attendance` stops the turn, returns a timeout to the caller, and deletes the
session and its scratch directory. The audit record stays (invariant 15).

The 120 s default matches the 120 s limit the chaperone applies to a delegate call
(contract 04 §7.5). A `timeout` longer than that limit is a warning: the caller
gives up first.

`timeout` is the only field. A job runs in a VM of its own, dropped at job
end (decisions CH1 and CH21-2), so no folder outlives a job and no field
names a caller root (design note section 4 item 5.7).

### 3.13 `triggers` — autonomous only

A list, at least one entry. Each entry is one of:

| Form | Field | Rule |
|---|---|---|
| cron | `cron` | a five-field cron expression, or `@hourly`, `@daily`, `@weekly` |
| webhook | `webhook` | a name, `[a-z][a-z0-9-]{1,30}` |
| dispatch | `enqueue` | `true`. Another family's `enqueue` verb starts the session |

Rules:

1. An entry names exactly one of the three keys. Two, or none, is an error.
2. A webhook name is unique across all families. Two families claiming one
   name is an error in the registry report, named on both files.
3. Each firing creates one session. `max_running_turns` decides whether its
   turn runs now or waits (§3.14).
4. The list says how sessions of this family begin. That is the whole of what
   it says. It grants nothing: the reach a session has is the rest of this
   file, and who may enqueue it is the CALLER's `verbs.enqueue.targets`
   (§3.5). A reader who wants to know who fires a family reads both.
5. `enqueue: true` is the only legal value of the dispatch form. `false` is an
   error: absence is denial (invariant 11), so a trigger that says "no" is a
   line that reads as a trigger and is not one. Write no entry instead. Two
   dispatch entries on one family are an error for the same reason: the second
   says what the first already said.
6. A dispatch entry that no family names in `verbs.enqueue.targets` is a
   WARNING, not an error. It can never fire, but the caller's grant and the
   target's trigger live in two files, and either may be committed first. An
   error would make the pair impossible to land in one direction. The same
   reasoning as §3.6.1 rule 3, and a warning never blocks an apply (§7 rule 4).
7. The dispatch form opens no route and holds no secret. A cron entry becomes
   a systemd timer and a webhook entry becomes an HTTP route with its own
   bearer token. This one becomes neither: the chaperone is the only caller, it
   reaches `attendance` on the host, and the `enqueue` grant is the whole
   permission (contract 04 §4.1). That is why it is a form and not a webhook
   named by convention.
8. The three forms may be mixed. A family that a timer wakes AND another
   family may dispatch writes both entries.

### 3.14 `max_running_turns` — autonomous only

Integer, default 1, range 1 to 8. The number of turns of this family that run
at the same time. Past it, a turn waits in a queue.
Attended and thin families have no limit and no queue, so the field is an
error on them.

### 3.15 `quiet` — autonomous only

What a cron firing checks, in code, before it wakes the family. When every
check holds, the firing is skipped: no session, no turn, no model call. Most
firings of a lead find nothing changed, and a turn that only confirms that
costs a pi start and a model call for nothing.

| Field | Type | Default | Rule |
|---|---|---|---|
| `board` | string | none | a server this file grants `survey_board` from |
| `daily` | table | none | one call owed once a day, below |
| `floor_hours` | integer | `24` | 1 to 168 |

`daily`:

| Field | Type | Default | Rule |
|---|---|---|---|
| `call` | string | required | a granted verb, or a granted tool as `<server>__<tool>` (§3.11's grammar, no wildcard) |
| `hour` | integer | required | 0 to 23, in the time zone of the host |
| `weekdays_only` | boolean | `false` | Saturday and Sunday owe nothing |

```toml
quiet.board = "board-lead"
quiet.daily.call = "mail__send_standup_email"
quiet.daily.hour = 16
quiet.daily.weekdays_only = true
quiet.floor_hours = 24
```

The time zone of the host is the value of `AGENT_TIME_ZONE` when the site
file sets it. Else it is the zone of
`/etc/localtime`. With no such file it is UTC. The door loads the zone at
start. A name must equal the entries of the time zone directory byte for
byte. A file that holds `zone` is refused as an unknown field (section 7
rule 1).

The check keeps the LAST GOOD WAKE: the last firing it let through whose job
ended `ok`, with the fingerprint and the job list it took just before that
firing. A wake that fails leaves the older one in place, so whatever woke it
wakes the next firing too.

A cron firing wakes the family when any one of these holds:

1. There is no last good wake, or `floor_hours` have passed since it fired.
   The floor bounds a missed signal: a frozen board still gets a full wake a
   day.
2. The family grants `enqueue`, and a job it enqueued is live, or ended after
   the last good wake fired. `job_status` is how the check reads them, so a
   family that holds `enqueue` must hold `job_status` too.
3. `daily` is owed: it is `hour` or later in the time zone of the host, a
   weekday when `weekdays_only`, and the audit holds no allowed `call` by
   this family since local midnight (contract 04 §6).
4. `board`'s fingerprint moved since the last good wake: each ticket's id,
   column, points, refined flag and epic, from `<board>__survey_board`.

A webhook or a dispatch firing is never checked. It carries a payload, and a
payload is work.

Rules:

1. **The check fails open.** A read that fails, a file it cannot parse, a chaperone
   that does not answer: each is a reason to wake. A needless wake costs
   cents. A wrongly skipped one costs a stalled job found a day late.

   A time zone that is not valid is no failed read. It is a refused config
   of `agent-trigger fire`: status 78. No cron
   firing of any family runs until the value is correct. Rule 1 does not
   cover it: a value of the operator is no failed read.
2. **The fingerprint is taken BEFORE the wake it belongs to.** The family's
   own writes during that wake move it, and cost one more wake at the next
   firing. A fingerprint taken after would fold in what another writer did
   while the family ran, which the family never read.
3. **A firing while a wake of the family is still live is skipped.** It
   would only queue behind it. Past `floor_hours`, it fires anyway.
4. **It reads with the family's own reach.** The board and the jobs are the
   family's own two calls through the chaperone, with its own token, so the check
   can never see more than the family can. They are audited under the family
   with no session.
5. **`agent-trigger fire <family> --force` skips the check.** A firing by
   hand is always meant.

## 4. The three kinds

| | `attended` | `thin` | `autonomous` |
|---|---|---|---|
| Who starts a session | a chat client or the TUI | a caller, through the chaperone | a trigger |
| Session life | forever | one job, then deleted | one job, then deleted |
| `job` | error | allowed | error |
| `triggers` | error | error | required |
| `max_running_turns` | error | error | allowed, default 1 |
| `delegates` | allowed | must be empty | allowed |
| `tools: all` | allowed | allowed | error (§3.4.5) |
| Expected reach | broad | one purpose | narrow plus approvals |

The last row is invariant 8. It is guidance for review, not a check.

## 5. Cross-reference checks

These run after the single-file checks pass. They read the rest of the registry
and the host facts.

### 5.1 The model alias exists

`caregiver` asks LiteLLM for its alias list and refuses an alias that is not
there. Without host context, for example in CI, the check downgrades to a
warning rather than being skipped, and the report says which check was
downgraded.

### 5.2 Every MCP server is declared

Each key of `tools` has a `mcp/<name>/server.toml`. Each granted tool is in
that file's `tools` list. Both are errors when they fail. This is the join that
makes invariant 18 true: one new server file, and families can grant it.

### 5.3 Every delegate exists and is thin

§3.6. The check reads the target's `kind` from its own file.

### 5.4 Every mount is under an allowed root

Minor 18 drops this check with `files` (§3.3). The host key
`AGENT_MOUNT_ROOTS` that held the roots goes with it (decision CH10-10).

### 5.5 The platform fence

Two grants are fenced: the `release` verb and each server that holds a
platform token. The platform tokens are `github_token_platform` (GitHub)
and `forgejo_token_platform` (Forgejo). Minor 18 drops the mount of the
platform working copy with `files` (§3.3).

Rules:

1. The platform families are host config. They are the value of the site
   key `AGENT_PLATFORM_FAMILIES`, a comma-separated list of family names.
   An absent or empty key names no family. The key is not a registry file.
   A fence that the fenced thing can edit is not a fence, and invariant 10
   says only the operator and the platform family change the platform.
2. This rule left in minor 18 with the mount grant.
3. A family outside the platform families that declares a fenced grant is
   an error. The report names the fence and the families it allows.
4. `code-worker` therefore has no path to write a platform repository. Its
   GitHub and Forgejo credentials cannot reach those repositories (contract
   01b §8.2), and a clone of `repos` never pushes (§3.7.1 rule 6).
5. This rule left in minor 18 with the mount grant.
6. This rule left in minor 18 with the mount grant.
7. **The operator lands every platform change.** No family is granted
   `merge_pull_request` from `github-platform`, from `forgejo-platform` or
   from another server of a platform token, by name or through `all`. A
   server of a platform token is a server whose `run.env` names
   `secret:github_token_platform` or `secret:forgejo_token_platform`.
   `caregiver` applies whatever reaches the registry `main` with no tap. A
   family that could merge there could widen itself, or write a new family
   with any reach. So the family opens pull requests and the operator
   merges them. The server names, the token keys and the withheld tool are
   constants in the validator, and rule 1 applies to them. A grant of the
   tool is an error, and the last good state stays (§7).

### 5.6 Registry-wide checks

1. A webhook name is claimed once (§3.13.2).
2. `enqueue.targets` names an existing autonomous family (§3.5.3).
3. A family directory with no `family.toml` is a warning, and `caregiver`
   ignores it.

## 6. What applies live, what replaces the sandbox

No change in this table ends a session, because session
state lives on the host (invariant 5).

| Field | How it lands | New sandbox? |
|---|---|---|
| `name`, `kind` | immutable (§3.1) | — |
| `description` | registry only | no |
| `model.router` | `/key/update` on the family key. An added model works at once. A removed model takes up to 10 s to be refused everywhere (§6.2) | no |
| `model.budget_usd_per_day` | `/key/update` on the family key. A budget drop refuses the next request at once (§6.2) | no |
| `model.context_window` | `caregiver` rewrites `runtime.json` in the family config mount (§6.1). The facade reads it on the family-state pull before each prompt | no |
| `tools`, `verbs`, `delegates`, `approval` | `caregiver` rewrites the grant file, the chaperone re-reads it per call | no |
| `max_inflight_delegations` | the same grant file, the same per-call re-read (§3.6.1 rule 5) | no |
| `egress` | `caregiver` rewrites `egress.json` (§6.1.1), and the proxy reads it on every CONNECT (§3.7 rule 11) | no |
| `repos` | `caregiver` rewrites `repos.json` (§6.1.1), and the vmm reads it on every clone link (CH69 rule 16) | no |
| `skills` | `caregiver` rewrites the family config mount (§6.1), read at turn start | no |
| `shell`, `sandbox_tools` | `caregiver` rewrites `runtime.json` in the family config mount (§6.1). The facade reads `sandbox_tools` on the family-state pull before each prompt (decision CH5c). The pull carries no `shell` (§3.8 rule 4) | no |
| `job.timeout` | `attendance` reads it per job | no |
| `triggers` | `caregiver` rewrites the timer set | no |
| `max_running_turns` | `attendance` reads it per turn | no |
| `quiet` | the trigger door reads it at each cron firing | no |
| `sandbox.cpus`, `sandbox.memory` | `caregiver` rewrites `shape.json` in the family config mount (§6.1 rule 6). A running VM keeps its shape until it sleeps or stops. A restore whose snapshot shape differs from `shape.json` drops the snapshot and cold-boots (decision CH82 rule 7) | at its next start |
| `sandbox.image` | same as `sandbox.cpus`. The flavor is the root disk a turn runs on, so a restore onto another flavor cold-boots as well (§3.9) | at its next start |

Removals apply at once (invariant 9). Three cases remain, and each has its
own enforcement point. Case 3 keeps its number:

1. A removed tool, verb or delegate: the chaperone denies the next call, because it
   `stat`s the grant file per call. A GATED call already waiting for a tap is
   cancelled with `approval_revoked`. A non-gated call already executing at
   the chaperone runs to completion.
2. A removed egress host: the vmm proxy refuses the next CONNECT, and an
   open tunnel runs until it closes (§3.7 rule 11).
3. This case left in minor 18 with `files` (§3.3).
4. A removed repo: the vmm reads `repos.json` on every clone link, so it
   refuses the next clone (CH69 rule 16).

`caregiver` never interrupts a call already in flight: contract 04 §0 gives
it no arrow to the chaperone, and it makes no call to `attendance` (contract 05
§5). No case interrupts a running turn. Contract 04 §1.5 states the cost of
the window.


### 6.1 The family config mount

One read-only directory carries every part of a family's definition that the
agent process itself reads. It also carries two things that the agent process
does not read: `model_live_at` in `runtime.json` (rule 5) and `shape.json`
(rule 6). The vmm is `creche-vmm`, the Cloud Hypervisor VM manager. Its
decisions are the rows CHn in section 2 of the design note
`docs/design/cloud-hypervisor-playpen.md`.
`caregiver` is the only writer of the directory. The facade pulls its
content from the vmm on the family-state link (decision CH5a), and one
revision counter versions the whole directory (contract 05 §2.1).

That counter is the field `config_rev` in both the channel and the status
document. One counter covers the whole directory.

| Entry | From | Read by |
|---|---|---|
| `instructions.md` | `families/<name>/instructions.md` | the agent process, at turn start |
| `skills/<name>/SKILL.md` | the granted skills (§3.10) | the agent process, at turn start |
| `runtime.json` | `shell`, `sandbox_tools`, `system_prompt`, `model.router` and `model.context_window` (§3.8, §3.2), and `model_live_at`, which `caregiver` sets (rule 5) | the facade, on the family-state pull before each prompt (decision CH5c). `model_live_at`: `caregiver` and the vmm (rule 5) |
| `shape.json` | `sandbox.cpus`, `sandbox.memory` and `sandbox.image` (§3.9) | the vmm, before it starts a VM of the family (rule 6) |

```json
{
  "shell": false,
  "sandbox_tools": ["read", "grep", "find", "ls"],
  "model_alias": "agent-router",
  "model_live_at": 1789812010000
}
```

`shape.json`, for the default `sandbox` fields:

```json
{
  "cpus": 2,
  "flavor": "base",
  "memory_mib": 2048
}
```

Rules:

1. `caregiver` writes the whole directory, then bumps the revision. A reader
   never sees half a revision. The path of the mount always names one whole
   revision, and the path is never absent. When the host refuses the write,
   the write fails and the old revision stays.
2. `runtime.json` holds no credential and no grant. The perimeter stays where
   invariant 12 puts it: mounts, egress, the chaperone and the model key (§3.8 rule
   3). Its keys are `shell`, `sandbox_tools`, `model_alias` and
   `context_window`, copied from `model.context_window` (§3.2), plus two keys
   that are written only when set: `model_live_at`, once a model change sets
   it (rules 5 and 7), and `system_prompt`, when it is `replace` (§3.8 rule 4).
   A `runtime.json` of a caregiver from before `context_window` has no such
   key, and the vmm reads it as no cap.
3. `model_alias` names the model of the family's prompts. It is a bare
   LiteLLM alias, copied from `model.router` unchanged. The facade gives
   the bare alias to its LiteLLM provider (`agent/src/litellm.ts`
   `bareAlias`). A prefixed form never appears in this file, the grant file
   or the status document.
4. A change to `instructions.md`, `skills/` or `runtime.json` reaches the
   next prompt of a running session, because the facade pulls the family
   state before each prompt (decision CH5c). No change replaces the VM.
   `shape.json` changes only with `sandbox.cpus`, `sandbox.memory` or
   `sandbox.image`, and §6's table says when each reaches a VM: at its next
   start.
5. **`model_live_at` sits next to `model_alias`** (decisions CH23-1 and CH31).
   It is the time from which a `/key/update` may be treated as live on every
   LiteLLM worker (contract 05 §7 rule 6). It is integer milliseconds since
   1970-01-01T00:00:00Z, UTC (CH22-1, and design note item 31), not the RFC
   3339 text that `applied.json` holds today. `caregiver` writes the alias and
   its live time in one directory swap, so a reader never sees a new alias
   with an old live time. `caregiver` carries the old value forward when the
   alias does not change. `caregiver` reads the value from here for its sync
   check, and the copy in caregiver's applied snapshot, `applied.json`, goes,
   so the field has one home. The vmm holds the family-state reply on host
   port 1100 until the host time passes this time, for at most 10 s. No wire
   type carries the field, and the guest needs no clock for it.
6. **`shape.json` is the family VM shape** (decisions CH36 G6, CH43 rule 3 and
   CH8-5, and the `FamilyShape` docs in `creche-wire`). Its keys are exactly
   `cpus`, `flavor` and `memory_mib`. `caregiver` writes it in the same
   directory swap, from `sandbox.cpus`, `sandbox.memory` and `sandbox.image`.
   `memory_mib` counts `1g` as 1024, and `flavor` is the flavor name of §3.9.
   The vmm reads it before it starts a VM of the family. No guest code reads
   it. It is a file of its own, not a part of `runtime.json`. Its type is
   `FamilyShape` in `creche-wire`, and `family/grammar.py` stays the authority
   for the ranges and the flavors. It holds no credential and no grant.
7. **When the two changes start (decision CH45).** `caregiver` writes
   `model_live_at` into `runtime.json` and writes `shape.json` from the
   `caregiver` change of the Cloud Hypervisor cut. `runtime.json` leaves
   out `model_live_at` until a model change sets it, so the first write adds
   no key to every family.

### 6.1.1 The two reach files beside the mount

Two files carry a family's reach to the vmm (decisions CH69 rules 3 to 6).
They sit beside `config/`, not in it:

```
/srv/agents/state/creche/families/<family>/
  config/        the family config mount (§6.1)
  egress.json    the egress allowlist, from `egress` (§3.7)
  repos.json     the clone list, from `repos` (§3.7.1)
```

| File | Wire type in `creche-wire` | Read by |
|---|---|---|
| `egress.json` | `FamilyEgress` | the vmm proxy, on every CONNECT, and the vmm `check` |
| `repos.json` | `FamilyRepos` | the vmm mirror server, on every clone link |

`egress.json` for `egress = ["github.com"]`, and `repos.json` for
`repos = ["lacreche"]`:

```json
{
  "hosts": [
    { "host": "github.com", "port": 443 }
  ]
}
```

```json
{
  "repos": ["lacreche"]
}
```

Rules:

1. **`caregiver` is the only writer of both files** (CH5b, CH6-4). It writes
   each by temp file and `rename()`, mode 0640, and only when the bytes
   change. One rename lands removals and additions at once (invariant 9).
2. **Every family gets both files.** `egress.json` is empty for a family
   with no `egress`, and `repos.json` is empty for a family with no `repos`.
   An empty `egress.json` is `{"hosts": []}`, and an empty `repos.json` is
   `{"repos": []}`.
3. **An absent file reads as empty**, so it fails closed. The vmm binds the
   egress port only for a non-empty `egress.json` and the mirror port only
   for a non-empty `repos.json` (CH49 rule 3 for the egress port, CH69 rule
   13 for the mirror port).
4. **Each file is in one canonical form** (CH69 rule 5). `egress.json` holds
   lower-case hosts, an explicit port (a bare host is written as port 443),
   no duplicates, sorted. `repos.json` holds the entries sorted. The two
   plane endpoints never enter `egress.json` (§3.7 rule 4). The wire types
   refuse an unknown key, and `family/grammar.py` stays the authority for
   the entry forms (CH23 rule 5).
5. **One file per vsock port** (CH69 rule 4). `egress.json` governs host port
   1104 and `repos.json` governs host port 1103. Neither file holds a
   credential.
6. **When the writes start** (CH69 rule 6). `caregiver` writes both files on
   every pass from its first release that holds them.
7. `delete_family` unlinks both files next to `creds.json`, before the
   sandboxes stop (CH69 rule 23). Reach narrows before processes stop.

### 6.2 On the model plane, "at once" means within 10 seconds

Probe 0b ran on the host on 2026-09-18 against the live
`litellm-headroom:0.37.0` image. One key per family works there, and
`/key/update` changes the models on a live key.

| Change | Measured |
|---|---|
| A model added to the key | works at once on some workers |
| A model removed from the key | refused on every worker after up to about 10 s. Measured 7 s across four workers, with answers flipping between workers until then |
| The budget lowered | the next request was refused at once |

The cause is LiteLLM's own key cache, `user_api_key_cache_ttl`, 10 s per
worker. So invariant 9's "a removal applies at once" reads two ways in §6's
table, and both are real:

1. On the chaperone plane, at once means on the next call. The chaperone re-reads the
   grant file per call (contract 04 §1.4), so a removed tool, verb or delegate
   is dead on the next call.
2. On the model plane, at once means within 10 seconds. A removed model may
   still answer on a worker whose cache has not expired.

A router change is not a reach change, so the 10 s window costs which model
answers, not what the family may touch. Contract 05 §5 and §6.3 carry the same
number for a switch and a rotation.

This lag is accepted: invariant 9's
"at once" means "as fast as the enforcement point allows". The chaperone plane and
the egress plane keep their own, faster paths.

## 7. Refusals and the validation report

Invariant 19: a bad file yields a report. It never crashes `caregiver` and
never half-applies.

Rules:

1. Unknown field: error. The message names the field, its location, and the
   closest known field name.
2. One parse reports every violation it can see, not the first.
3. An error blocks the apply for that family only. The last good state stays
   live, and the family's status becomes `invalid`.
4. A warning never blocks an apply.
5. Other families keep reconciling. One bad file is not a registry outage.

Report shape, one object per family:

| Field | Type | Meaning |
|---|---|---|
| `family` | string | the directory name |
| `file` | string | path, relative to the registry root |
| `status` | enum | `in_sync`, `reconciling`, `invalid`, `degraded`, per contract 05 §2.1 |
| `applied` | boolean | whether this parse changed the live state |
| `issues` | list | every issue found, in file order |
| `errors` | integer | count of `severity: error` |
| `warnings` | integer | count of `severity: warning` |

One issue:

| Field | Type | Meaning |
|---|---|---|
| `severity` | enum | `error` or `warning` |
| `loc` | string | field path, for example `tools.kagi[1]` |
| `msg` | string | one sentence, what is wrong and what would be valid |
| `downgraded` | boolean | true when host context was absent (§5.1) |

```json
{
  "family": "chat",
  "file": "families/chat/family.toml",
  "status": "invalid",
  "applied": false,
  "issues": [
    {
      "severity": "error",
      "loc": "tools.kagi[1]",
      "msg": "'kagi_summarize' is not declared in mcp/kagi/server.toml",
      "downgraded": false
    },
    {
      "severity": "error",
      "loc": "delegates[0]",
      "msg": "'code' exists but its kind is 'attended'; a delegate must be thin",
      "downgraded": false
    },
    {
      "severity": "warning",
      "loc": "model.router",
      "msg": "no LiteLLM context: 'agent-router' was not checked",
      "downgraded": true
    }
  ],
  "errors": 2,
  "warnings": 1
}
```

## 8. The five locked families, plus `scrum-lead`

These rows are examples of a locked fleet. Budgets are placeholders.

### 8.1 `families/chat/family.toml` — attended

```toml
name = "chat"
kind = "attended"
description = "General assistant. Knows the operator, searches, delegates, asks for tickets."

model.router = "agent-router"
model.budget_usd_per_day = 15

tools.kagi = ["kagi_search_fetch", "kagi_extract"]
tools.ha-read = ["ha_get_state", "ha_search", "ha_get_overview", "ha_get_history"]
tools.ha = ["ha_call_service"] # named, not all: all widens with mcp/ha's catalog
tools.vikunja-home-assistant = "all"
tools.vikunja-finance = "all"

verbs.enqueue.targets = ["scrum-lead"]

delegates = ["vault-oracle", "code-sandbox"]

egress = [] # every reach goes through the chaperone, so every reach is audited

shell = false
sandbox_tools = ["read", "grep", "find", "ls", "write", "edit"]
sandbox.cpus = 4
sandbox.memory = "8g"

skills = ["grill-me", "handoff", "memory-writing"]
approval = []
```

### 8.2 `families/code/family.toml` — attended

```toml
name = "code"
kind = "attended"
description = "Coding agent for every repository that is not the platform."

model.router = "code-router"
model.budget_usd_per_day = 100

tools.kagi = ["kagi_search_fetch", "kagi_extract"]
# The credential behind this server cannot reach the three platform repos
# (contract 01b §8.2).
tools.github-code = "all"
tools.vikunja-home-assistant = "all"
tools.vikunja-networking = "all"

verbs = {}

delegates = ["vault-oracle"]

egress = [
    "github.com",
    "api.github.com",
    "codeload.github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
    "registry.npmjs.org",
    "pypi.org",
    "files.pythonhosted.org",
]

shell = true
sandbox_tools = ["read", "grep", "find", "ls", "write", "edit"]
sandbox.cpus = 8
sandbox.memory = "16g"

skills = []
approval = []
```

### 8.3 `families/platform/family.toml` — attended

```toml
name = "platform"
kind = "attended"
description = "The only family that edits the platform."

model.router = "code-router"
model.budget_usd_per_day = 100

tools.kagi = ["kagi_search_fetch", "kagi_extract"]
# The mirror of github-code: this credential reaches only the three
# platform repos (contract 01b §8.3).
tools.github-platform = "all"
tools.vikunja-platform = "all"

# The releasable component names of contract 06 §1. `registry-data` is
# absent because it never takes a release (contract 06 §6).
verbs.release.components = [
    "chaperone",
    "attendance",
    "caregiver",
    "noticeboard",
    "playpen",
    "mcp-servers",
    "infra",
    "handover",
]

delegates = ["vault-oracle"]

egress = [
    "github.com",
    "api.github.com",
    "codeload.github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
    "registry.npmjs.org",
    "pypi.org",
    "files.pythonhosted.org",
]

shell = true
sandbox_tools = ["read", "grep", "find", "ls", "write", "edit"]
sandbox.cpus = 8
sandbox.memory = "16g"

skills = []
# Invariant 10: a platform release waits for the operator's phone tap, and that tap is
# the release executor's own. `release` is not listed: one tap per release
# (§3.11.3).
approval = []
```

### 8.4 `families/vault-oracle/family.toml` — thin

```toml
name = "vault-oracle"
kind = "thin"
description = "Retrieval. Searches the indexed corpora and answers with citations."

model.router = "fast"
model.budget_usd_per_day = 5

tools = {}

verbs.embed = {} # the semantic half of index_search

delegates = [] # required empty on a thin family (§3.6.4)

egress = []

shell = false
sandbox_tools = ["read", "grep", "find", "ls"] # grep is the fallback when search misses
sandbox.cpus = 2
sandbox.memory = "4g"

skills = []
approval = []
job.timeout = "120s"
```

### 8.5 `families/code-sandbox/family.toml` — thin

```toml
name = "code-sandbox"
kind = "thin"
description = "Runs code for one chat, in a VM that lives for one job."

# This model is open. `fast` is the placeholder
# that exists today; no `cheap` alias does.
model.router = "fast"
model.budget_usd_per_day = 5

tools = {}
verbs = {}
delegates = []

# These two lines together are "shell, no network".
egress = []

shell = true
sandbox_tools = ["read", "grep", "find", "ls", "write", "edit"]
# `egress: []` means this family can install nothing, so whatever it needs to
# run code has to be in the image. `python` is the flavor that carries pandas,
# numpy and matplotlib (§3.9).
sandbox.cpus = 4
sandbox.memory = "8g"
sandbox.image = "python"

skills = []
approval = []
job.timeout = "120s"
```

### 8.6 `families/scrum-lead/family.toml` — autonomous


```toml
name = "scrum-lead"
kind = "autonomous"
description = "Refines, points and splits board tickets, dispatches workers, emails the standup."

model.router = "agent-router"
model.budget_usd_per_day = 10

# Named tools, not `all`: an autonomous family may not say `all` (§3.4.5).
tools.board-lead = [
    "survey_board",
    "get_ticket",
    "append_dev_notes",
    "set_points",
    "mark_refined",
    "label_epic",
    "mark_blocked",
    "file_child_ticket",
    "comment_on_ticket",
    "file_ticket",
]
tools.mail = ["send_standup_email"]

verbs.embed = {}
verbs.ha_call.allow = [
    { domain = "notify", service = "mobile_app_phone" },
]
verbs.enqueue.targets = [
    "platform-worker",
    "finance-worker",
    "home-assistant-worker",
    "networking-worker",
]
verbs.job_status = {} # reads back the jobs this family enqueued (§3.5.4)

delegates = []

egress = []

shell = false
sandbox_tools = ["read", "grep", "find", "ls", "write", "edit"] # the ledger write lane
sandbox.cpus = 2
sandbox.memory = "4g"

skills = []

triggers = [
    { cron = "@hourly" },
    { enqueue = true }, # `chat` enqueues this family (§8.1, §3.13 rule 8)
]

# One wake at a time. A second firing queues (§3.14).
max_running_turns = 1

# Most hourly firings find nothing changed. Code checks that first (§3.15).
quiet.board = "board-lead"
quiet.daily.call = "mail__send_standup_email"
quiet.daily.hour = 16
quiet.daily.weekdays_only = true
quiet.floor_hours = 24

# Every dispatch is one phone tap.
approval = ["enqueue"]
```
