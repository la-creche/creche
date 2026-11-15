# Design: family isolation

> Status as of 2026-11-14: Done on 2026-10-31. All 16 pull requests merged and the host runs them.
> Host names, addresses and accounts are placeholders. The operator's private notes hold the real values.

Date: 2026-10-29. Status: done 2026-10-31. All 16 PRs are merged and live on the host. The two changes from this note are marked where they apply.

Base: creche `83e39edc` (after creche#287), registry `773a661` (after registry#39), chaperone `3042d6a`.

## 1. What to do now

1. Answer the five questions of section 2. Each question has a recommendation.
2. Read sections 4 to 6. They say what changes and what stays.
3. Say "go" with your answers. I then open the PRs of section 7, one at a time, in the order of that section.

## 2. Questions for you

| # | Question | Recommendation | The other option |
|---|---|---|---|
| Q1 | Where do the two fences live? | Two keys of the site file `/etc/creche/site.env`. The file is root-owned and comes from `private-docs` by `sudo creche-deploy`. The caregiver unit reads it already. | A new root file `/etc/creche/fences.toml`. It breaks rule 3 of `env/AGENTS.md` ("the host gives one `.env`"). |
| Q2 | What does the registry CI check, when the fences are host facts? | The two fence checks become warnings marked `downgraded` in CI, as the model and symlink checks are today. The caregiver on the host refuses the family. | A copy of the fences in the registry for CI only. The registry could then edit what CI checks, and the copy can drift. |
| Q3 | What does a release request record as `requested_by`? | The app ID of the call. The chaperone sends it on each call as `_meta["chaperone.app"]`. The attendance plugin reads it the same way. | The fixed word `chaperone`. This needs no chaperone change, but the phone summary no longer says which family asked. |
| Q4 | What does the chaperone do when `policy.toml` is absent or is not TOML at start? | Start, and deny every call. `/healthz` answers 503. This is what a bad reload does today. | Refuse start, as today. Then a restart is worse than a reload, and a caregiver fault stops the chaperone. |
| Q5 | What does the chaperone do when an upstream ends after start? | Mark it down, and refuse each family granted it, as at start. No restart (v1, `docs/design.md` §7.7). You restart the chaperone. | Keep today's behaviour: each call to that upstream fails with `not_connected`, and the other tools of the family keep working. This conflicts with decision 3. |

One more choice, with no recommendation needed from you unless you object: The host value of the mount roots drops `/srv/agents/work/projects`. No family mounts a path under it today. A narrower fence refuses less by accident.

## 3. What the code does today

### 3.1 The caregiver and a refused family

| Case | What the caregiver does today | Gap |
|---|---|---|
| The validator refuses the family (an `error` in its report). | It keeps the last good state. The sandbox keeps running. The last fragment `grants/<family>.toml` stays, so the old `[apps.<family>]` table stays in `policy.toml`. The status reads `invalid`. (`caregiver/src/caregiver/reconcile.py:255-266`, `_keep_last_good` at `:722-766`) | None. This is "refused as a whole": the family runs its last checked definition, never a part of the new one. |
| The family was never valid. | No sandbox, no fragment. The chaperone does not know the app. | None. |
| The validator passes, but `chaperone check` refuses the new fragment. | `land_app` writes the new fragment first (`policy_fleet.py:254-256`). The assembly then leaves the family out (`:422-427`). The family loses its last good grants. Its sandbox keeps running with no tool. | **Gap A.** Two refusal paths give two different results. |
| Each fragment passes alone, but the joined file fails (for example, two apps share a digest). | Nothing is written for any family. The old file stays. (`policy_fleet.py:430-432`, a known gap in `caregiver/AGENTS.md`) | **Gap B.** One family's problem stops the policy writes of all families. |
| `chaperone check` cannot run (binary absent, timeout). | Nothing is written. | None. This is a fault of the platform, not of a family. |

### 3.2 A family with a missing tool

| Case | Today | Gap |
|---|---|---|
| The family grants a server with no `mcp/<server>/server.toml`, or one that does not parse. | Validation error. The family is refused as a whole. (`family/src/agent_family/validate.py:314-316`) | None. |
| The server file parses but has rule errors (`serverrules.py`). | The errors go only to the server's own report. The caregiver never reads server reports. The family is not refused. (`registry.py:224-225`) | **Gap C.** |
| The server is in the registry but not in the host's `install.toml`. | `chaperone check` refuses the fragment with `UnknownUpstream` (chaperone `crates/core/src/policy.rs:580-585`). Then Gap A applies. | Gap A. |
| The server is installed but fails to start. | The chaperone refuses to start. Every family stops. (chaperone `crates/chaperone/src/effect.rs:283-287`, `:469-482`) | **Gap D** (decision 4). |
| The server ends after start. | Each call to it fails with `not_connected`. The other tools of the family keep working. `tools/list` still shows the dead tools. (`effect.rs:239-241`, `gateway.rs:245-248`) | **Gap E** (decision 3, Q5). |
| An unknown Flue built-in in `sandbox_tools`. | Validation error. The family is refused as a whole. (`validate.py:545-552`) | None. |

### 3.3 The chaperone and one family's policy

1. `policy.toml` holds one table for each app. `Policy::load` stops at the first error of any app. One bad app rejects the whole file (chaperone `crates/core/src/policy.rs:521-539`). **Gap F.**
2. At start, a policy that does not load stops the process (`startup.rs:174-177`).
3. At runtime, the chaperone polls the file on each call. A bad new file sets the policy to none, and every call is denied (`reload.rs:96-104`). It keeps no last good policy.
4. Every refusal of start exits with status 1. No exit status 78 exists in the chaperone.

### 3.4 Family names in code

| Where | Name | Plan |
|---|---|---|
| `family/src/agent_family/grammar.py:244` | `PLATFORM_ALLOWLIST = {"code"}` | Host config (section 4.1). |
| `family/src/agent_family/grammar.py:230-236` | `ALLOWED_ROOTS`, with `/srv/agents/work/coding-sandbox` | Host config (section 4.1). |
| `attendance/src/attendance/workspace.py:26,29`, `paths.py:26` | `coding-sandbox`, as a family and as a path | A field of the family file (section 4.2). |
| `bin/chaperone-install-draft.py:69` | `REQUESTED_BY = "code"` | Removed (section 4.3). The host file still says `agent-control`. |
| caregiver, chaperone | none outside tests | No change. |

## 4. Design: the fences and the family names

### 4.1 The fences become host config (decision 1)

1. Two new keys go into the env catalog of creche:
   - `AGENT_MOUNT_ROOTS`: optional in the catalog, with no default. The value is a list of absolute paths, separated by commas. (Changed in creche#292: a required key needs a typed reader in `handover`, which is frozen. The caregiver refuses to start without the key.)
   - `AGENT_PLATFORM_FAMILIES`: optional, with an empty default in `default.env`. The value is a list of family names, separated by commas. No value means that no family may hold the platform server or `release`.
2. The the host value goes into `private-docs/site.env`:

   ```
   AGENT_MOUNT_ROOTS=/srv/agents/vault,/srv/agents/code,/srv/agents/state/index,/srv/agents/work/coding-sandbox
   AGENT_PLATFORM_FAMILIES=code
   ```

3. The file reaches the host only by `sudo creche-deploy`. Root owns `/opt/private-docs/site.env` and the link `/etc/creche/site.env`. No timer pulls `private-docs`. The registry has no path to it. No sandbox mounts `/etc` or `/opt`, because neither is a mount root.
4. `family` gets one new value, `Fences`, with two fields: `mount_roots` and `platform_families`. `load_registry` takes it as an argument. The two constants leave `grammar.py`.
5. With no `Fences` (registry CI, the `agent-family` command, noticeboard, door-trigger), the two fence checks give one warning each, marked `downgraded`. This is the rule that the model and symlink checks follow today.
6. The caregiver reads the two keys from its environment at start. Its unit already has `EnvironmentFile=/etc/creche/site.env`. These are the faults of the caregiver's own config, and each one stops it with exit status 78:
   1. `AGENT_MOUNT_ROOTS` is absent.
   2. A root is not absolute, or holds `.`, `..` or `//`.
   3. A platform family name does not match `[a-z][a-z0-9-]{1,30}`.
7. The caregiver reads the keys at start only. A change of the fences needs a restart of the caregiver.
8. The platform server's name (`PLATFORM_SERVER = "github-platform"`) and `PLATFORM_WITHHELD` stay constants. They name a server and a tool, not a family. The catalog key `AGENT_PLATFORM_SERVER` exists, and nothing reads it. Section 9 lists it as a follow-up.

A family that the new fences refuse keeps its last good state (section 3.1). So a wrong host value cannot take a running family's sandbox away. It stops the next change of that family.

### 4.2 The per-chat directory becomes a field of the family file (decision 2)

1. The family file gets one new optional field: `job.caller_root`. It is an absolute path.
2. The validator accepts it only when all of these are true:
   1. The family is `thin`.
   2. The path equals the path of one `rw` entry of `files`.
3. The caregiver copies it into the `limits` block of the family's status document, next to `job_timeout_s`. Attendance reads `limits` already (`attendance/src/attendance/family_status.py:326`).
4. Attendance makes `<caller_root>/<owner_session>` at the first delegate call of a chat, with mode 0700. The job runs there. This is today's behaviour, with the path from the status document.
5. A family with no `caller_root` runs each job in its sessions mount, as every other family does today.
6. Attendance loses `CODING_SANDBOX_FAMILY`, `WORKSPACE_KIND`, `_CODING_SANDBOX` and the key `ATTENDANCE_WORK_ROOT`. Nothing else reads that key.
7. The registry's `coding-sandbox` file sets `job.caller_root = "/srv/agents/work/coding-sandbox"`.

### 4.3 The release requester comes from the app ID (decision 2, Q3)

1. The chaperone's `release` plugin reads `_meta["chaperone.app"]` of the call. It writes that value as `requested_by`. A call with no app ID, or with a value outside `[a-z][a-z0-9-]{1,30}`, is refused. The request file is not written.
2. The flag `--requested-by` leaves the plugin. The plugin refuses an unknown flag, so an old `install.toml` makes the `release` upstream fail to start. Section 5 says what that does.
3. `bin/chaperone-install-draft.py` drops `REQUESTED_BY`.
4. `handover` does not change. Its `REQUESTED_BY_RE` accepts a family name already, and `requested_by` is "recorded, never trusted".

## 5. Design: what stops the chaperone, and what stops one family (decisions 3 and 4)

### 5.1 The rule

1. A fault of the chaperone itself refuses start. A fault of one family, or of one tool, refuses that family only.
2. A refused family is refused as a whole. Each call of that app is denied, and its `tools/list` is empty. The chaperone never serves a part of an app's grants.
3. No error becomes an allow. Each change below changes which calls are denied. None of them allows a call that today's code denies, except the calls of the families that today's code stops with no fault of their own.

### 5.2 What refuses start (the chaperone's own config)

1. `install.toml` is absent, too large, or fails the strict reader.
2. The plugin tree does not read, or a plugin named in `install.toml` is absent.
3. A slot is empty, or its plugin fails: identity, audit, clock, secret source and approval channel. These are parts of the chaperone. They are not tools of a family.
4. The secrets directory does not read, or a secret of a slot plugin does not read.
5. The listen address fails `check_listen`, or the bind fails.
6. The run ID cannot be made.

The exit status stays 1. Your decision 86 (exit status 78 for a refused start) is about the Rust port of creche. Section 9 lists it for the chaperone as a follow-up.

### 5.3 What no longer refuses start

1. **An effect plugin (an upstream) fails to start.** This covers a spawn failure, a missing secret of that plugin, a failed `initialize` and a failed `tools/list`. The chaperone logs the cause, marks the upstream `down`, and continues. Each app with a grant on a `down` upstream is refused with the fault `upstream_down`.
2. **An upstream ends after start (Q5).** The chaperone sees the transport close, logs it, and marks the upstream `down`. The same refusal follows. No restart. You restart the chaperone.
3. **`policy.toml` is absent or is not TOML at start (Q4).** The chaperone starts with no policy. It denies every call, as a bad reload does today. `/healthz` answers 503. The caregiver's chaperone watch then raises `pep_unreachable` after 90 s.

### 5.4 One app's policy no longer stops the others (Gap F)

1. The chaperone reads the file in two levels:
   1. The envelope: the TOML syntax, the size, `version` and the top-level keys. A fault here is a fault of the whole file. No policy loads, and every call is denied.
   2. Each `[apps.<id>]` table. A fault here refuses that app only. The other apps load.
2. Two apps with one shared digest are both refused. A fence's `check_sections` refusal refuses its caller app only, and the check runs again without it. (Changed in chaperone#70: the target's own table is valid alone.)
3. A grant to an upstream that `install.toml` does not name refuses that app (`unknown_upstream`).
4. A reload applies the same two levels. The chaperone keeps no last good app table. The caregiver keeps the last good fragment (section 6).
5. `chaperone check` stays strict. Any app fault gives a non-zero exit. Its output names each refused app and its fault, so the caregiver can tell which fragment failed.

### 5.5 What `/healthz` says

`/healthz` answers 200 when a policy is loaded. The body adds two lists: the `down` upstreams and the refused apps, each with its fault. The caregiver's chaperone watch only needs the status code today, so this adds no new fault to the watch. Section 9 lists a per-family fault as a follow-up.

### 5.6 What stays fail-closed

1. An unknown credential is denied.
2. A tool that the app's table does not grant is denied.
3. A refused app is denied in full.
4. A policy envelope that does not load denies every call.
5. A slot fault refuses start.
6. A plugin fault, a hook with no answer and an approval channel with no answer each deny the call (G2, unchanged).

`docs/design.md` G2 and §7.7 step 3 change with the PRs of section 7.

## 6. Design: the caregiver with a refused family

The rule: a refused family keeps its last good state. It never runs a part of a new definition. Each other family keeps working.

1. **Validation refusal.** No change. The sandbox runs, and the last fragment stays.
2. **`chaperone check` refuses a new fragment (Gap A).** `land_app` checks the new fragment before it replaces the old one. A refusal keeps the old fragment, and the status reads `invalid` with the chaperone's fault text. Today the family loses its grants.
3. **The joined file fails, but each fragment passes alone (Gap B).** After 5.4, `chaperone check` names the apps it refuses. The caregiver keeps the last good fragment of each app named, and writes the file for the others. Today it writes nothing for anyone.
4. **A family grants a server whose file has rule errors (Gap C).** The validator refuses the family: "`<server>`'s server file has errors". The family keeps its last good state.
5. **An upstream is `down` at the chaperone.** The chaperone refuses the family (section 5.3). The caregiver does not see this yet. Section 9 lists it as a follow-up.

## 7. The PRs, in order

Each row is one PR, one idea. "After" names what must be merged, released or applied first.

| # | Repo | The idea | After | Size (estimate) |
|---|---|---|---|---|
| 1 | creche | Add `AGENT_MOUNT_ROOTS` and `AGENT_PLATFORM_FAMILIES` to the env catalog | none | 30 lines |
| 2 | private-docs | Set the two keys in `site.env` | 1 merged | 5 lines |
| — | host | You deploy 1 and 2 together (runbook step A) | 1, 2 | — |
| 3 | creche | `family` takes `Fences`. The caregiver reads them from the site keys. The two constants leave. | step A | 250 lines |
| 4 | registry | Bump `CRECHE_FAMILY_REF` to 3. The fence checks become CI warnings. | 3 merged | 10 lines |
| 5 | creche | Refuse a family whose granted server file has errors (Gap C) | 3 | 80 lines |
| 6 | creche | Keep the last good fragment when `chaperone check` refuses a new one (Gap A) | none | 120 lines |
| 7 | chaperone | Load each app alone. One bad app is refused, the others load. `check` names each app. (Gap F) | none | 250 lines |
| 8 | chaperone | An upstream that fails to start is `down`, and its apps are refused (Gap D) | 7 | 200 lines |
| 9 | chaperone | An upstream that ends after start is `down` (Gap E, Q5) | 8 | 120 lines |
| 10 | chaperone | Start with no policy when `policy.toml` does not load (Q4) | 7 | 60 lines |
| 11 | creche | Keep the last good fragments of the apps that the joined check names (Gap B) | 7 released | 120 lines |
| 12 | creche | The family field `job.caller_root`: validate it, and carry it in the status `limits` | none | 150 lines |
| 13 | registry | `coding-sandbox` sets `job.caller_root`, and the pin moves to 12 | 12 released | 10 lines |
| 14 | creche | Attendance takes the per-chat root from the status. The family name and `ATTENDANCE_WORK_ROOT` leave. | 13 applied | 120 lines |
| 15 | chaperone | The release plugin takes the requester from the app ID. `--requested-by` leaves. | 8 released | 100 lines |
| 16 | creche | The install draft drops `--requested-by` | 15 merged | 15 lines |

Each chaperone PR updates `docs/design.md` for its own part. Each creche PR updates the `AGENTS.md` of its package (for 3: `family/AGENTS.md` line 45 and the table at line 178).

Three chains can run side by side: the fences (1 to 5), the chaperone (6 to 11, 15, 16) and the per-chat root (12 to 14). Each chain opens its next PR after the previous one merges.

## 8. Runbook for you (the host)

Each command is a write or a tap. Agents run none of them.

### Step A: deploy the catalog and the site keys (after PRs 1 and 2 merge)

1. Pull the two deploy origins:

   ```bash
   ssh <host> 'git -C /home/<user>/code/creche pull --ff-only'
   ```

   ```bash
   ssh <host> 'git -C /home/<user>/code/private-docs pull --ff-only'
   ```

2. Deploy. This brings `/opt/creche` and `/opt/private-docs` to their origins together:

   ```bash
   ssh -t the host 'sudo creche-deploy'
   ```

3. Check. Expect no new `FAIL` line, and the count `2`:

   ```bash
   ssh <host> '/opt/creche/bin/fleet-up check'
   ```

   ```bash
   ssh <host> "grep -c -E '^(AGENT_MOUNT_ROOTS|AGENT_PLATFORM_FAMILIES)=' /etc/creche/site.env"
   ```

The order matters. A site file with a key that the old catalog does not know fails `fleet-up check`. A catalog with a required key that the site file does not set also fails it. One deploy moves both.

### Step B: the caregiver release with the fences (after PR 3 merges)

1. Tap the `caregiver` release on the phone.
2. Check that the caregiver runs, and that each family reads `in_sync`:

   ```bash
   ssh <host> 'journalctl --user -u creche-caregiver.service -n 40 --no-pager'
   ```

3. If the caregiver exits with status 78, the journal names the key. Fix `site.env` in `private-docs`, then repeat step A.

### Step C: the chaperone releases (after PRs 7 to 10 merge)

1. Tap each `chaperone` release on the phone.
2. Check the health and the two new lists:

   ```bash
   ssh <host> 'curl -s http://192.0.2.10:8300/healthz'
   ```

### Step D: the requester (after PR 15 merges and step C ran for PR 8)

1. Tap the `chaperone` release with PR 15. The `release` upstream then fails to start, because `install.toml` still has `--requested-by`. Only `code` is refused, the one family granted `release`. The other families keep working.
2. Remove the flag from the host file:

   ```bash
   ssh <host> "sudo sed -i 's/, \"--requested-by\", \"agent-control\"//' /etc/chaperone/install.toml"
   ```

3. Check that the flag is gone. Expect `0`:

   ```bash
   ssh <host> "grep -c requested-by /etc/chaperone/install.toml"
   ```

4. Restart the chaperone:

   ```bash
   ssh -t the host 'sudo systemctl restart creche-chaperone.service'
   ```

5. Check that no upstream is `down`:

   ```bash
   ssh <host> 'curl -s http://192.0.2.10:8300/healthz'
   ```

### Step E: the per-chat root (after PR 13 is applied by `registry-sync`)

1. Check that `coding-sandbox` reads `in_sync` in the caregiver journal (step B, item 2).
2. Tap the `attendance` release with PR 14.
3. Ask `chat` to run code in `coding-sandbox`. Check that the job runs in `/srv/agents/work/coding-sandbox/<session>`.

## 9. Follow-ups, not in this design

1. The catalog key `AGENT_PLATFORM_SERVER` exists and nothing reads it. Read it in place of `PLATFORM_SERVER`, or remove it.
2. The caregiver's chaperone watch could read the two lists of `/healthz` and raise a fault on each family that the chaperone refuses.
3. The chaperone exits with status 1 for a refused start. Decision 86 asks for 78.
4. A failed bind of the chaperone's admin socket does not stop start (`main.rs:320`, `admin.rs:281-287`). By rule 5.1 it should.
5. The caregiver writes token digests while a family file is invalid (a known gap in `caregiver/AGENTS.md`).
