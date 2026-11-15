# OpenShell ideas on the CH stack: plan

> Status as of 2026-11-14: Plan accepted on 2026-11-14. Nothing is built.
> Host names, addresses and accounts are placeholders. The operator's private notes hold the real values.

Date: 2026-11-14. Status: plan accepted, nothing built. The operator answered D1 to D3 on 2026-11-14 ("Yes, all three"): each as recommended. Sources, all read-only:

- creche `0ebe0ec8`, chaperone `234d02d`, registry `158b03a` and mcp `0f4910f`, all at origin/main.
- NVIDIA/OpenShell main `2e5589b218` (2026-11-13, last release `v0.1.8`), read with `gh api`.
- `cloud-hypervisor-playpen-design.md`, `phase7-egress-plan.md`, `code-creche-split.md` and `browser-mcp-design.md`.
- private-docs `docs/rework/consolidation-decisions.md`.

File refs: `C` = creche, `CH` = chaperone, `R` = registry, `OS` = OpenShell. Line numbers are on main.

## 0. Answer first

| Idea | Verdict | Order | Size |
|---|---|---|---|
| 4. Policy prover | Build it, as an exact reach diff in registry CI | 1st | 2 PRs, about 1 day |
| 1. Credential placeholders | Build a small form: per-session placeholders on the two planes only. A spike gates it (CH5a). | 2nd | 1 spike, 2 PRs, about 1.5 days |
| 2. Per-program identity | Not worth it here | none | none |
| 3. Seccomp user notification | Not worth it here | none | none |

Next action: start R1 (idea 4) and spike S0 (idea 1). They run in parallel. D1 to D3 are answered (section 7).

## 1. What the stack already does

These facts decide every verdict below.

1. **The guest has no NIC.** Guest init brings up only `lo` (C `rust/crates/vmm-confine/src/command.rs:1-8`, `boot.rs:21-22`). The VM reaches the host only over fixed vsock ports (CH8-1, CH14).
2. **The guest has no DNS.** The vmm resolves names on the host, on each CONNECT (C `rust/crates/vmm/src/egress/rule.rs:115`).
3. **Egress is exact host:port.** The 1104 proxy accepts only `CONNECT host:port` (C `rust/crates/vmm/src/egress/proxy.rs:18-33`). `rule::decide` matches the family list exactly and refuses non-public addresses (C `rule.rs:107-142`). After the 200 it splices bytes and never reads the tunnel.
4. **Two secrets reach the guest.** The family-state reply on port 1100 carries the family LiteLLM key and the chaperone `pep_token` (C `rust/crates/wire/src/lib.rs:2289`, host reader C `rust/crates/vmm-confine/src/family.rs:278-297,547`). The facade wraps them in `Secret` (C `agent/src/family-state.ts:259`). It sends them only to guest 127.0.0.1:4000 and :8300 (C `agent/src/litellm.ts:128,169`, `agent/src/chaperone.ts:42`). Every upstream token, such as `github_token_platform`, stays in the chaperone (CH `docs/design.md:359-368`).
5. **Both planes are plain HTTP** (fact 30). The vmm splices them with no parse and no rewrite (C `rust/crates/vmm/src/planes.rs:205`, CH6-2).
6. **A grant change has no gate.** A merge to registry `main` applies within a minute, with no tap (R `README.md:25-28`). CI checks shape only (R `.github/workflows/validate.yml`). `classify(old, new)` exists, but it runs on the host after the merge (C `family/src/agent_family/classify.py:108`).

## 2. Idea 4: reach diff on every registry PR

### 2.1 How OpenShell builds it

1. The gateway builds a reachability model from the policy, the credential set and a binary-capability registry. It runs Z3 queries on the current and the candidate policy (OS `crates/openshell-prover/src/queries.rs:77,272`).
2. `finding_delta` keeps only new findings. There are 4 kinds: `credential_reach_expansion`, `capability_expansion`, `l7_bypass_credentialed` and `link_local_reach` (OS `crates/openshell-prover/src/finding.rs:12-33`).
3. Auto-approval runs only on the exact string `prover: no new findings` (OS `crates/openshell-server/src/grpc/policy.rs:620-1076`).
4. Size: 7,774 lines of Rust. The solver exists because the rules use globs, paths and HTTP methods.

### 2.2 What exists today

- Family schema: C `family/src/agent_family/model.py:133-166`. Server schema: C `family/src/agent_family/server.py:82-91`.
- Field-level directions (add, remove) per family file: C `family/src/agent_family/classify.py:108`, grants at `:191`. It sees one family file at a time. It does not see a server file change.
- Shape validator in registry CI, pinned to a creche commit: R `.github/workflows/validate.yml`, `CRECHE_FAMILY_REF`.
- The chaperone fences are exact scalar matches (CH `crates/core/src/decide.rs:234-241`).

### 2.3 Design

Every rule in our policy is an exact set: tool names, `host:port`, repo names and fence values. No glob exists (contract 01 §3.7). So a set difference is exact, and no solver is needed. Call it a reach diff, not a prover.

New module `reach.py` in the creche `family` package, next to `classify.py`. It runs in registry CI only.

1. Read the base registry and the head registry with the existing strict reader.
2. For each family, build its reach as typed sets:
   - **tools:** (server, tool, write) triples. `"all"` expands against that server's `tools` catalog.
   - **credentials:** every `secret:` name in the `run.env` of each granted server, plus its `shared_secrets`.
   - **ungated tools:** granted tools that `approval` does not list.
   - **fences:** each `arg_denies` and `arg_allows` value set of each granted tool.
   - **sandbox reach:** `egress`, `repos`, `delegates`, each `verbs` scope, `shell` and `sandbox_tools`.
3. Diff head against base. Each gain is one finding. The finding kinds:

   | Kind | Example |
   |---|---|
   | `new_credential` | `creche` gains `github_token_platform` through `git-platform` |
   | `new_write_tool` | a tool with `write = true` joins a grant, also by a catalog add under `"all"` |
   | `new_tool`, `gate_dropped` | a read tool joins, or a tool leaves `approval` |
   | `fence_loosened` | a value leaves `arg_denies`, or joins `arg_allows` |
   | `new_reach` | an egress host, a repo, a delegate, a verb scope, `shell` |

4. Print one Markdown table to `$GITHUB_STEP_SUMMARY`: family, kind, detail. Print narrowings too, under their own heading.
5. Exit 1 if any gain exists. Exit 0 if the PR only narrows or changes nothing.

A server file change counts against every family that grants that server. This is the case `classify` cannot see. The 2026-11-14 review found it by hand: `git-platform` holds the platform token outside the platform fence. The reach diff would print `new_credential` on the grant PR.

`link_local_reach` needs no twin. The egress grammar refuses IP literals (C `family/src/agent_family/grammar.py:288-327`), and the proxy refuses non-public addresses at run time (C `rule.rs:124-210`).

Not covered: the meaning of a fence value. The `./main` bypass (status file, 2026-11-14 ~05:45Z) passes an exact-match fence. A set diff cannot see it, and the Forgejo branch protection is the answer there.

### 2.4 PR sequence

| PR | Repo, base `main` | Content | Size |
|---|---|---|---|
| R1 | creche | `reach.py`, the `agent-family reach --base <dir> <dir>` command, tests on fixture registries | about 400 lines plus 400 lines of tests. Half a day to build and review. |
| R2 | registry | `validate.yml` checks out the PR base into a second folder and runs `agent-family reach`. It bumps `CRECHE_FAMILY_REF` to R1's merge. `AGENTS.md` gains the D1 rule. | about 40 lines. 1 hour. |

**Status 2026-11-14:** R1 is open as la-creche/creche#571 (`6d5af637`), and the operator merges it. The replay flags registry PR 44 and the `git-platform` grant (`new_credential`), and it flags none of the 8 text-only merges. The full table is in the side-session notes, `r1-reach-replay.md`.

R1 changes only the `family` package. If caregiver bundles that package, R1 may mint a caregiver tag. No host path calls `reach.py`, so that install changes no behavior.

### 2.5 Proof in the real environment

1. **Replay.** Run `agent-family reach` on each pair of adjacent merge commits of registry `main`, from real history. It must flag the known widenings, for example registry#44 (the code/creche split). It must flag nothing on a merge that only changed text.
2. **Live.** The first real grant PR is the proof: the principal browser grant, branch `principal/browser-grant` `0c2e562`, which stays unpushed until its host steps run. Its check must turn red and list the browser tools. A narrowing PR must stay green.

No fixture or flag in production code. The replay reads git history, and the live proof is a real PR.

## 3. Idea 1: per-session placeholders on the planes

### 3.1 How OpenShell builds it

1. The agent's env holds opaque strings such as `openshell:resolve:env:KEY` (OS `crates/openshell-core/src/secrets.rs:13,1002-1020`).
2. The proxy terminates TLS with its own CA (OS `crates/openshell-supervisor-network/src/l7/tls.rs:358`). It swaps the placeholder in the HTTP/1.1 head, by default only in `Authorization` (OS `secrets.rs:674,1411`).
3. A placeholder sent outside its binding gets 403 `credential_endpoint_mismatch` (OS `secrets.rs:642`). Bodies are scanned for leftover markers (OS `crates/openshell-core/src/secrets_body.rs:18`).

### 3.2 What fits our stack, and what does not

- **No TLS termination.** No third-party credential is in the guest (section 1, fact 4). CH3 already rejected a vmm proxy that adds the GitHub token, because "chaperone holds every credential". The 1104 tunnel stays blind.
- **The two planes fit.** They are plain HTTP (fact 30), so the vmm can swap a header with no CA.
- **CH5a holds this today.** Its row says the key and token sit in guest memory and snapshots, and "a move to proxy-added creds needs evidence first". So this idea is gated by a spike (D2).

What it buys:

1. A leaked value is worthless. Today a key read out of the guest works from anywhere on the LAN. A placeholder works only on that one VM's vsock plane.
2. Snapshots hold no real secret. A snapshot stays on disk up to 7 days (CH2). Whether the host's restic backup covers it is not checked.
3. A rotation lands at the next request. The vmm reads `creds.json` per request, so a running turn picks up a new key without a pull.

### 3.3 Design

1. **Placeholders per session.** At session create, the vmm makes two random values in the real formats: `sk-` plus 48 hex digits for LiteLLM, and 64 lowercase hex digits for the chaperone. It reads `/dev/urandom`, so no crate is added. It stores them in the session record, so a restore finds the values that the guest memory holds.
2. **The family-state reply sends the placeholders.** `FamilyCreds` keeps its shape. So the wire type, the facade, the guest init protocol and the contracts do not change.
3. **The planes become HTTP/1.1 proxies.** For each request on 1101 and 1102, the vmm reads `creds.json` and checks the request's `Authorization` value:
   - the exact placeholder of this VM: swap in the real value and forward.
   - any other value, or none: answer 403 and write one log line, with no secret in it.
4. **Bodies stream through unbuffered.** The vmm does not touch bodies or responses, so LiteLLM streams and chaperone MCP streams pass as they do now.
5. **The facade change is none.** R1 of this idea must confirm one thing first: that pi's LiteLLM provider and Flue's MCP connection send the secret in `Authorization` (C `agent/src/litellm.ts:169`, `agent/src/chaperone.ts:42`). If one uses another header, that header is swapped instead.

Inside the guest, nothing changes: a tool that reads the facade's memory gets a placeholder where it got a key before. Ideas 2 and 3 are not needed for that reason.

### 3.4 Fit with the egress model

- No new port, no NIC, no new egress. 1104 and 1103 are untouched.
- CH6-2 ("no rewrite, so the Host header stays valid") is amended: the vmm rewrites one header and keeps `Host`.
- Phone approval is untouched. The chaperone sees the same token as today.

### 3.5 Spike and PR sequence

| Step | Repo, base `main` | Content | Size |
|---|---|---|---|
| S0 spike | none, the host, as <user>, scratch dir, no sudo | (1) Search one real snapshot memory file for its family's key bytes. This proves or refutes fact 33. (2) Time a hyper rewrite proxy against today's splice: 1,000 chaperone `tools/list` calls and 100 streamed LiteLLM calls, p50 and p99. (3) Read whether restic backs up the vmm state dir. | 2 hours |
| P1 | creche | The `client` feature on the workspace `hyper` dependency (CH9-1: a feature add is its own PR) | 15 minutes |
| P2 | creche | Placeholders, the store field, the reply change, the plane proxy, the 403 line, `rust/crates/vmm/AGENTS.md` rules, a kvm test | about 600 lines plus 400 lines of tests. 1 day. kvm job about 13 minutes a run. |

Build gate from S0: build P1 and P2 only if the added p50 latency is 1 ms or less. That value is the plan's first value, and D2 can change it. The placeholder and the swap ship in one PR, because either one alone breaks every model call.

After P2 merges and the operator taps the vmm release:

1. Old snapshots still hold real values. Rotate both credentials of each family once, with `caregiver rotate`. The exact command goes to the operator at that step.
2. Read one new snapshot file for the real key bytes, read-only. The key must be absent.

#### S0 results (2026-11-14, 23:30-23:55 UTC)

Verdict: **gate passes. Build P1 and P2**, with the three P2 notes below. Run as <user> on the host, no sudo. Scratch dir `~/s0-spike` (707 MB: a Rust 1.92.0 toolchain, the bench and its build) is removed. `vmm serve` (pid 221800) and every service ran untouched. No command needs the operator.

**Check 1. Secrets sit in snapshots: fact 33 is proven.** A read-only script searched the data extents of two `memory-ranges` files under `/home/<user>/.local/state/creche-vmm/sessions/<id>/snapshots/<n>/` for the bytes of the family's own `creds.json`. It printed counts only. Both credential files predate both snapshots (written 2026-11-09 and 2026-11-10).

| Snapshot | Family | Taken | Data scanned | LiteLLM key | `pep_token` | `Bearer <key>` | `Bearer <pep_token>` |
|---|---|---|---|---|---|---|---|
| `58eebc40` | creche | 2026-11-14 18:17Z | 560 MiB | 8 found | 5 found | 2 found | 0 |
| `8e9bbd0b` | principal | 2026-11-14 18:17Z | 399 MiB | 7 found, plus 1 as UTF-16 | 5 found, plus 1 as UTF-16 | 2 found | 0 |

The state root holds 8 snapshots (6 creche, 1 principal), 27 GB in all, mode 0600, owner <user>.

**Check 2. Added latency: pass, except a back-to-back burst.** A hyper 1.12 HTTP/1.1 rewrite proxy (check the placeholder, read `creds.json` per request, swap `Authorization`, stream both bodies) against a `copy_bidirectional` splice. Stand-in upstreams on 127.0.0.1 in the scratch dir. Host load 13 to 15. Values in µs. "Added" is rewrite minus splice. The chaperone and burst rows give the range over 3 runs. The paced rows are 1 run.

| Shape | Connection | Splice p50 / p99 | Rewrite p50 / p99 | Added p50 | Added p99 | Gate (p50 ≤ 1 ms) |
|---|---|---|---|---|---|---|
| 1,000 chaperone `tools/list`, 8 KB reply | keep-alive | 129-172 / 273-365 | 172-240 / 423-493 | +43 to +68 | +128 to +178 | pass |
| 1,000 chaperone `tools/list` | new per call | 322-378 / 641-731 | 396-462 / 687-807 | +70 to +84 | -45 to +166 | pass |
| 100 LiteLLM streams, 16 KB request, 100 chunks 2 ms apart, first byte | keep-alive / new | 3,744 / 4,002 | 3,815 / 4,072 | +70 to +72 | +171 to +357 | pass |
| the same, end of stream (about 313 ms) | keep-alive / new | 313,307 / 313,557 | 313,363 / 312,861 | -696 to +56 (timer noise) | noise | pass |
| 100 LiteLLM streams, 301 chunks back to back, first byte | both | 281-411 | 422-516 | +84 to +166 | -150 to +469 | pass |
| the same, end of stream | both | 836-1,069 | 5,766-7,800 | **+4,930 to +6,776** | +6,897 to +8,039 | **fail** |

The burst row costs about 20 µs per chunk: the proxy forwards one hyper frame per chunk, and the splice moves whole buffers. A real LiteLLM stream sends tokens milliseconds apart, as in the paced rows, so a turn sees about +70 µs. All three refusal checks answered 403, each with one log line. Not covered: the guest side is TCP here, not vsock.

**Check 3. Restic does not back up the snapshots.** `restic-backup.service` (root, nightly 03:30 UTC) runs `/opt/private-docs/bin/restic-backup.sh`. It backs up `/srv/agents`, `/etc/chaperone` and `/var/lib/chaperone` only. The vmm state root `/home/<user>/.local/state/creche-vmm` is on `/`, outside those paths, with no link into them. The last run (2026-11-14 03:33) processed 3.2 GiB, less than the 27 GB of snapshots. Each family's `creds.json` is under `/srv/agents/state/creche/families/`, so the real values are in restic either way.

**What this means for the gain (section 3.2).**

1. Point 1 (a leaked value is worthless off the VM) holds in full. It is now the main gain.
2. Point 2 (snapshots hold no real secret) is true today, but small. The snapshots stay on the host's disk, mode 0600 <user>. The same uid reads `creds.json`.
3. Point 3 (rotation at the next request) is unchanged.

**P2 notes from S0.**

1. **The LiteLLM placeholder format in section 3.3 step 1 is wrong.** The real `litellm_key` is 25 characters, `sk-` plus 22. The placeholder must be `sk-` plus 22 characters, not `sk-` plus 48 hex digits. The `pep_token` is 64 characters, as planned.
2. **P2 must not forward a burst one frame at a time.** Either coalesce response frames, or splice the response bytes and parse only the request head. The kvm test, or a bench in the PR, times a 301-chunk burst.
3. **The chaperone header is still not confirmed** (section 8 risk 1). The facade source says `Authorization: Bearer` for both planes (C `agent/src/litellm.ts:8,128`, `agent/src/chaperone.ts:8`). Flue's own code was not read, because the host checkout has no `node_modules`. The snapshots hold `Bearer <key>` but no `Bearer <pep_token>`, so the memory does not confirm it either.

### 3.6 Proof in the real environment

1. **kvm test in P2.** A real `vmm serve`, a real CH guest, and a plane upstream that the test binds on its own port through the existing plane config keys (CH67 rule 2). That is configuration, not a seam. The test asserts three things:
   - the upstream sees the real value
   - a wrong value gets 403
   - the snapshot file holds no real value
2. **On the host.** The snapshot read of step 2 above, plus one real turn of `code` and one of `principal`.

## 4. Idea 2: per-program identity. Not worth it here.

### 4.1 How OpenShell builds it

At each trapped `connect`, it opens `/proc/<pid>/exe`, hashes the opened file, and checks every ancestor (OS `crates/openshell-binary-identity/src/lib.rs:256-432`). Policy names programs by path. Its own docs list the limits: an interpreter is one identity for every script it runs, and anything an allowed launcher spawns inherits the grant.

### 4.2 Why not here

1. The VM is the boundary. One session runs per VM (CH1), and the agent drives every program in it through `bash` (`shell = true` for `code` and `creche`).
2. The allowed programs carry anything. `git`, `node` and `python` each open a tunnel to any allowed host. A rule "only git may reach github.com" stops nothing that git itself can send.
3. It needs idea 3 to exist, because the host sees no guest pid today (C `rust/crates/guest-init/src/bridge.rs:21-27`).
4. Idea 1 does not need it. Per-session placeholders keep today's in-guest protection, so no "only the facade may use the planes" rule is required.

Exists today, and stays: the family-state socket accepts only the facade pid by `SO_PEERCRED` (C `bridge.rs:23-25`).

## 5. Idea 3: seccomp user notification. Not worth it here.

### 5.1 How OpenShell builds it

1. A launcher thread installs a notification filter on `socket`, `connect`, `sendto`, `sendmsg` and signal syscalls (OS `crates/openshell-sandbox/src/linux/seccomp_notify.rs:336-354`).
2. The broker makes each INET socket itself, bound to `lo`, and injects it with `SECCOMP_IOCTL_NOTIF_ADDFD`. A `connect` waits up to 30 s for the supervisor (OS `crates/openshell-sandbox/src/network_broker.rs:689-1492`).
3. DNS goes to a relay on 127.0.0.53 that answers only allowed names, with synthetic addresses.
4. On a link drop it sends SIGSTOP to the workload, then SIGCONT or SIGKILL (OS `crates/openshell-sandbox/src/linux/process_signal.rs:138`).
5. It needs Linux 6.2 or newer and Landlock ABI v3.

### 5.2 Why not here

1. **The mediation exists by construction.** With no NIC, a `connect` reaches only the guest-init bridges on 127.0.0.1. The vmm decides each CONNECT on the host by exact host:port (section 1, facts 1 to 3).
2. **The DNS relay has nothing to guard.** The guest has no resolver (fact 2).
3. **Freeze on link drop is already stronger.** A vmm stop or crash ends every CH scope through `BindsTo` and `ExecStopPost` (C `rust/crates/vmm-confine/AGENTS.md:44-54`). The guest dies. It does not pause.
4. **"Pause until the host decides" fits better in the proxy.** If a phone ask for a new egress host is ever wanted, the 1104 proxy can hold the CONNECT itself. That needs no guest change.
5. **The cost is large.** A notify supervisor in guest init, a filter on every tool process, and a check that pnpm, cargo, uv and node still work. The guest kernel config (`ch-release-v7.2.8-20261009`) is not checked for the needed options.

## 6. Order

1. **R1, then R2** (idea 4). R1 starts now. R2 waits for D1.
2. **S0** (idea 1), after D2. It needs the operator's OK for the the host read and the timing run.
3. **P1, then P2** (idea 1), only if S0 passes its gate.
4. **The vmm tap, the rotation and the snapshot read**, after P2.

R1 and S0 can run in parallel. They touch different repos and no shared file.

## 7. Decisions for the operator

**Answered 2026-11-14.** The operator: "Yes, all three". D1, D2 and D3 each take the recommended option below. The other options are rejected.

**D1. What a red reach check blocks.**
Recommended: the check goes red on any gain, and your merge is the approval. An agent never merges, and never turns on auto-merge for, a registry PR with a red `reach` check. R2 writes that rule into registry `AGENTS.md`. It becomes a required check once registry moves to Forgejo with branch protection.
Other option: caregiver also holds each widening on the host until a phone tap. Cost: about 3 more PRs and a new approval path in caregiver.

**D2. Reopen CH5a for per-session placeholders on the planes, gated on spike S0.**
Recommended: yes. Run S0 on the host as <user>, in a scratch dir, no sudo, removed after. Build P1 and P2 only if the added p50 is 1 ms or less. If you say no, CH5a stays as it is, and idea 1 closes.

**D3. Close ideas 2 and 3 for this stack.**
Recommended: yes, for the reasons in sections 4.2 and 5.2. Reopen them only if a family ever gets open-web egress inside its VM (browser note, fact 4).

Choices made without you, each the option with less work:

1. The reach diff is Python, in the creche `family` package next to `classify.py`, because the registry validator already runs that package.
2. It uses a set difference, not an SMT solver, because every rule is an exact set.
3. The placeholders keep the `FamilyCreds` shape, so the wire, the facade and the contracts do not change.
4. The placeholder and the swap ship as one PR.

## 8. Risks and open checks

1. **The `Authorization` header is not confirmed** for the pi provider and the Flue MCP connection (section 3.3, step 5). P2 checks it first.
2. **Fact 33 is proven** by S0 (section 3.5, S0 results, check 1).
3. **Restic does not back up vmm snapshots** (S0 check 3). The real values are in restic through `creds.json` either way.
4. **The blind 1104 tunnel stays.** A guest can send data to an allowed host, such as github.com, with a token of its own. OpenShell answers this with TLS termination and HTTP method rules. This plan does not.
5. **R2's check is advisory on GitHub Free.** Registry is private, so required checks answer 403 until the Forgejo move (status file, 2026-11-14 ~18:20Z).
