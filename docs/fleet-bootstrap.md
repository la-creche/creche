# Fleet bootstrap

Status: draft design. Not in service.

This document tells how to set up the fleet layer of a host from nothing.
The script is `bin/fleet-up`. It runs as the operator. It replaces the
`up` verb of the old cutover script for a fresh host, and for a fleet layer
that the operator removed.

## 1. Three owners

The host holds three layers. Each layer has one owner. `fleet-up` makes
the first layer only.

### 1.1 The fleet layer: the operator, through `fleet-up`

| Part | Where |
|---|---|
| The record of `fleet-up` | `/srv/agents/state/rework/fleet/record.json` |
| One token file for each principal of attendance | `/srv/agents/state/rework/tokens/` |
| The access key of the noticeboard | `/srv/agents/state/rework/tokens/view-access.key` |
| The key of the ACP door | `/srv/agents/state/rework/door-acp.key` |
| Three env files | `/srv/agents/state/rework/{sessiond,view,managerd}.env` |
| The state directories | `families/`, `leases/`, `faults/`, `triggers/webhooks/`, `log/`, `sock/` under the state root |
| The user units | `~/.config/systemd/user/` |

The fleet layer also holds two parts that `fleet-up` does not make:

- The three user component trees, `~/.local/components/{attendance,caregiver,noticeboard}`.
  A release installs them. `fleet-up` reads the release stamp of each tree
  and writes nothing into a tree.
- The families and their sandboxes. The caregiver makes them from the
  registry. `fleet-up` never touches a family or a sandbox.

### 1.2 The root layer: root, by hand

- The accounts and the groups: `chaperone`, `agents` and each `mcp-<name>`
  account.
- `/etc/chaperone`, `/etc/creche/site.env` and the sops keys.
- `/opt/creche` (the deploy), `/opt/components` (the releases) and
  `/opt/mcp`.
- `/var/lib/creche-handover` and `/var/lib/chaperone`.
- The system units: `creche-chaperone.service`, `creche-handover.path`
  and `creche-handover.service`.
- The compose stack: LiteLLM, CouchDB and the other containers.
- The state root `/srv/agents/state/rework` itself, and the sessions root
  `/srv/agents/sessions`.
- `cosign`, `uv` and the firewall.

### 1.3 The owner layer: the owner of the deployment

- The registry checkout, `/srv/agents/registry`, and each family file in it.
- The Node-RED approval flow: its resume URL and its two bearers.
- The NPM routes and Authentik, which run on another machine.
- The private repository that holds the site file.

## 2. The order

1. Root does each step of the root checklist (section 3). The operator runs
   `fleet-up check`, and each line of the root checklist passes. R7's
   listener and R8 pass only after step 5. That is correct.
2. The owner does O1: the registry checkout.
3. The operator runs `fleet-up up`. It makes the tokens, the keys, the env
   files and the directories. It installs each unit whose tree is there.
   On a fresh host no component tree is there, so `up` exits with status 3.
   It prints the release request to file.
4. Root does R10: two token copies into `/etc/chaperone/secrets`.
5. The operator files the release request that `up` printed. The order of
   the set is `chaperone`, `playpen`, `attendance`, `caregiver`,
   `noticeboard`. The phone asks for one tap.
6. The owner does O2: the Node-RED flow.
7. The operator runs `fleet-up up` again. It installs the units of the new
   trees and starts them. It exits with status 0.
8. The operator runs `fleet-up check`. Each line passes.

A second `up` on a whole fleet layer changes nothing. Run it after each
change of the host. It is the repair too.

## 3. The root checklist

`fleet-up check` verifies each line that the operator can read. It never
does a step. The host cutover runbook of the private repository, part A,
holds each command.

| # | Step | `check` reads |
|---|---|---|
| R1 | Make the state root and the sessions root, owned by the operator. | The owner of the state root. The sessions root is writable. |
| R2 | Install `cosign` (runbook A1) and `uv` 0.12.17 or newer (A2). Install `sbx`. | `/usr/local/bin/cosign`, `uv --version`, `sbx` on `PATH`. |
| R3 | Run `sudo creche-deploy`. Install the handover tree and its root wrapper by hand. | `/opt/creche/bin/creche-watchdog.sh`, the stamp of `/opt/components/handover`. |
| R4 | Make the group `chaperone`. Add the operator to `chaperone` and to `agents` (A5). Restart the user manager. | `getent group`, and `id -nG` of this login. |
| R5 | Make `/etc/chaperone`, mode 1775, group `agents`, with `plugins/`, `secrets/` and `install.toml` (A6). | The mode and the group. Each of the three paths is there. |
| R6 | Add `CHAPERONE_LISTEN` and `AGENT_COSIGN_PROGRAM` to the site file (A8). | `creche-env check` of the site file. The file sets `CHAPERONE_LISTEN`. |
| R7 | Install the unit of the Rust chaperone and its drop-in (A7, C2). Put the release binary at `~/chaperone-rel/bin/chaperone` (A6). | The binary at that path or at `/opt/components/chaperone/bin/chaperone`. A listener on port 8310. |
| R8 | The first release of `playpen`. | `/opt/components/playpen/images.env`. |
| R9 | Put `LITELLM_MASTER_KEY` into `hooks.env`. `docs/rework/cutover.md` §4.1 gives the one command. | The key in `hooks.env` or in `managerd.env`. The value is never printed. |
| R10 | After the first `up`: copy `tokens/door-delegate.token` and `tokens/door-dispatch.token` into `/etc/chaperone/secrets`. Mint `approval-token` and `resume-token` there (A6). | Nothing. The operator cannot read `secrets/`. |
| R11 | Review the firewall (A10). | Nothing. |

The owner steps:

| # | Step |
|---|---|
| O1 | Clone the registry to `/srv/agents/registry`. `check` reads its `.git`. |
| O2 | Set the resume URL and the two bearers of the Node-RED flow (runbook C3). |
| O3 | Add the NPM route `/acp/<family>` with Authentik, and the drop-in of `creche-door-acp.service` (`door-acp/AGENTS.md`). Only a browser client needs it. |
| O4 | Put `APPROVAL_URL` and `APPROVAL_TOKEN` into `hooks.env`. Without them, the watchdog writes each notice to the journal and pushes none. `check` gives a warning. |

## 4. What `up` makes, and what `down` removes

`up` records each path that it makes. `down` reads the record. It removes
each recorded path in reverse order and nothing else.

| Made | Where | Owner and mode | Removed by `down` |
|---|---|---|---|
| The record | `.../rework/fleet/record.json` | operator, 0600 in a 0700 directory | Last, when `down` kept no path. Else `down` writes the kept paths into it. |
| `tokens/` | `.../rework/tokens/` | operator, 0700 | Only when `up` made it and it is empty. |
| Eight principal token files | `.../rework/tokens/<principal>.token` | operator, 0600 | Only when `up` minted it. |
| The access key of the noticeboard | `.../rework/tokens/view-access.key` | operator, 0600 | Only when `up` minted it. |
| The key of the ACP door | `.../rework/door-acp.key` | operator, 0600 | Only when `up` minted it. |
| Three env files | `.../rework/{sessiond,view,managerd}.env` | operator, 0600 | Only when `up` made the file. |
| `families/`, `leases/`, `faults/`, `triggers/`, `triggers/webhooks/`, `log/` | under the state root | operator, 0750 | Only when `up` made it and it is empty. |
| `sock/` | `.../rework/sock/` | operator, group `agents`, 2750 | Only when `up` made it and it is empty. |
| `~/.config`, `~/.config/systemd`, `~/.config/systemd/user` | the home of the operator | operator, 0755 | Only when `up` made it and it is empty. |
| Twelve unit files | `~/.config/systemd/user/` | operator, 0644 | Only when `up` installed it. A file that `up` refreshed stays. |
| The state "enabled" of eight units | the user manager | | `systemctl --user disable --now`, only for a unit that `up` enabled. |

**What `up` does with a file that is there before it runs.**

- A token or a key: `up` keeps it, whatever it holds. `down` keeps it too.
- An env file: `up` sets each key that it owns, and leaves each other key.
  `down` keeps the file.
- A unit file that differs from the tree: `up` refreshes it and says so.
  `down` keeps it. `up` does not restart the unit.

**What `up` does not make.**

- `hooks.env`. Section 5 tells why.
- A component tree. A release installs it.
- A family, its `creds.json`, its sandbox and its timers.
  The caregiver writes them. A timer of a family is a file in
  `~/.config/systemd/user/`, so `down` keeps that directory while a timer
  is there.
- `/etc/chaperone/policy.toml`. The caregiver writes it through
  `chaperone check`.

**What `down` never touches.** The vault, CouchDB, the registry checkout,
the sessions, the audit files, the outcome records, `hooks.env`, a
component tree, a sandbox and each path of root.

`down` stops each unit that it disables. It keeps `families/` while a family
directory is in it. To remove the families too, run `caregiver delete` for
each family before `down`.

## 5. Why `up` does not write `hooks.env`

`hooks.env` holds three values: `LITELLM_MASTER_KEY`, `APPROVAL_URL` and
`APPROVAL_TOKEN`. `fleet-up` mints none of them.

1. `LITELLM_MASTER_KEY` is a value of the sops file of root. Root decrypts
   it. `fleet-up` decrypts nothing.
2. `APPROVAL_TOKEN` is the bearer of the Node-RED flow. The flow keeps its
   own copy. A new value from `fleet-up` would not match it.
3. A `down` that removed the file would stop each phone notice of the
   watchdog and of the drift check.

`fleet-up` reads one value of `hooks.env`: `LITELLM_MASTER_KEY`. The
caregiver reads the key from its environment, so `managerd.env` must hold
it. `up` copies the value into `managerd.env` once. It writes the value with
a shell builtin, so the value reaches no argv. It never prints the value.

## 6. The units and their trees

`up` installs a unit only when the tree that the unit runs from is there.

| Unit | Runs from | `up` starts it |
|---|---|---|
| `creche-attendance.service` | the `attendance` tree | yes, first |
| `creche-caregiver.service` | the `caregiver` tree | yes, when `managerd.env` holds the master key and both images |
| `creche-noticeboard.service` | the `noticeboard` tree | yes |
| `creche-door-acp.service` | the `attendance` tree | yes |
| `creche-trigger-webhooks.service` | the `attendance` tree | yes |
| `creche-trigger@.service` | the `attendance` tree | no, it is a template |
| `creche-watchdog.{service,timer}` | `/opt/creche/bin` | the timer |
| `registry-sync.{service,timer}` | `/opt/creche/bin`, on the registry checkout | the timer, when the checkout is there |
| `creche-follow.{service,timer}` | the `handover` tree | the timer, when the tree is there |

`creche-door-owui.service` is not in the list. The ACP door replaces it.

## 7. Invariants

1. `fleet-up` refuses to run as root. It never runs `sudo`.
2. It never writes under `/etc`, `/opt`, `/usr`, the vault, the registry
   checkout or the compose stack.
3. It never writes `hooks.env`. It never touches a secret that it did not
   mint. The one exception is the read of section 5.
4. It never prints a secret, and it never puts one on argv.
5. A second `up` changes nothing. The tests hold each mode, each time of
   change and each byte equal.
6. `up` records each write. `down` removes only a recorded path.
7. `down` refuses the whole record when one line is not of the form that
   `up` writes. It also refuses the whole record when one path is outside
   the state root and outside `~/.config`. It then removes nothing.

## 8. Exit status

| Verb | 0 | 1 | 2 | 3 |
|---|---|---|---|---|
| `check` | Each line passes. | One line fails or more. | A bad command line. | |
| `up` | The fleet layer is whole. | A refusal, or a step failed. | A bad command line. | `up` did its part. It waits for a release or a root step. Each `PENDING:` line names the step. |
| `down` | Done. A kept path is in the record. | The record is refused. | A bad command line. | |

## 9. Known gaps

- `fleet-up` reads `CHAPERONE_LISTEN` from the site file with
  `envfile_value`. `env/AGENTS.md` lists this read as a known gap.
- `bin/creche-watchdog.sh` does not watch `creche-door-acp.service`.
- `systemd/AGENTS.md` names the old cutover script as the installer of the
  user units. That changes when the owner takes this design.
- The tokens of `door-delegate` and `door-dispatch` are 0600. The Python
  chaperone read them at 0640. The Rust chaperone reads the copies of R10.
