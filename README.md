# crèche

A control plane for agents that run in deny-by-default microVMs. One file
defines an agent family. The plane converges the host to that file.

The source is public. The deployment it was built for is private. The design
documents and the contracts of that deployment are in a private repository.
A citation such as `docs/rework/spec.md` or `contract 04 §6` resolves there.

**Documentation standard.** Every Markdown document in this repository is
written in ASD-STE100 Simplified Technical English. This is a rule, not a
preference. Read [docs/writing-standard.md](docs/writing-standard.md) before
you write or change a document.

## 1. The three planes

A sandbox has two egress holes and no ingress.

```
MODEL    sandbox -> LiteLLM :4000      one virtual key per family: models, budget
ACTION   sandbox -> chaperone :8300    one decision per call, every call audited
DATA     sandbox <-> bind mounts       the kernel enforces, no network
```

The revocation order is fixed: credentials first, process second. An
interrupted change leaves a sandbox with less reach, never with more.

## 2. One file defines a family

```
<registry>/families/<name>/family.yaml      the one file
<registry>/families/<name>/instructions.md  prose for the model
<registry>/mcp/<name>/server.yaml           one MCP server
```

A family file is the only unit of permission. It names the models and the
budget, the mounts, the egress, the tools, the approvals and the triggers.
Change the file, and the caregiver converges the host to it. There is no
apply command and no revoke command. The `family` package holds the schema.
The noticeboard edits the same file from a form.

## 3. The components

Every kind of agent is one call: create a session in a family, then run a
turn. The `attendance` service takes that call from four doors.

| Directory | Component | Unit | What it owns | Read |
|---|---|---|---|---|
| `chaperone/` | `chaperone` | `creche-chaperone.service`, system, own user | Every action outside a sandbox. The one hardened unit. | [chaperone/AGENTS.md](chaperone/AGENTS.md) |
| `attendance/` | `attendance` | `creche-attendance.service`, user | Every session, turn, journal and event stream. | [attendance/AGENTS.md](attendance/AGENTS.md) |
| `door-owui/` | in `attendance` | `creche-door-owui.service`, user | The Open WebUI door: an OpenAI-compatible surface. | [door-owui/AGENTS.md](door-owui/AGENTS.md) |
| `door-tui/` | in `attendance` | none, a command | The terminal door: `agent-tui <family>`. | [door-tui/AGENTS.md](door-tui/AGENTS.md) |
| `door-trigger/` | in `attendance` | `creche-trigger-webhooks.service`, `creche-trigger@.service`, user | The trigger door: timers and webhooks. | [door-trigger/AGENTS.md](door-trigger/AGENTS.md) |
| `caregiver/` | `caregiver` | `creche-caregiver.service`, user | The reconciler. It converges every family to its file. | [caregiver/AGENTS.md](caregiver/AGENTS.md) |
| `noticeboard/` | `noticeboard` | `creche-noticeboard.service`, user | The one view: every family, sandbox, session and audit line. | [noticeboard/AGENTS.md](noticeboard/AGENTS.md) |
| `playpen/` | `playpen` | none, an OCI image | The process inside each sandbox, the pi bridge and the terminal launcher. | [playpen/AGENTS.md](playpen/AGENTS.md) |
| `handover/` | `handover` | `creche-handover.{path,service}`, system, root. `creche-follow.{service,timer}`, user | The release tool: resolver, tag allocator, executor, requester, follower, secret intake. | [handover/AGENTS.md](handover/AGENTS.md) |
| `family/` | in `caregiver` | none, a library | The `family.yaml` and `server.yaml` schemas, the validator, the registry loader. | [family/AGENTS.md](family/AGENTS.md) |
| `library/` | none | `index@.timer`, `index-code@.timer`, user | The retrieval index builder. Not an agent. | [library/AGENTS.md](library/AGENTS.md) |
| `toybox/` | none | none | The one pi version pin. | [toybox/AGENTS.md](toybox/AGENTS.md) |

The other directories:

| Directory | What it holds | Read |
|---|---|---|
| `bin/` | Every script that runs on the host, and the three shell libraries. | [bin/AGENTS.md](bin/AGENTS.md) |
| `systemd/` | Every unit file. A unit is installed by a script, never by hand. | [systemd/AGENTS.md](systemd/AGENTS.md) |
| `integration/` | The cross-package suite. It runs the real doors, `attendance`, the chaperone and the playpen together. | [integration/AGENTS.md](integration/AGENTS.md) |
| `rust/` | The Cargo workspace: every Rust crate. No release uses it. | [rust/AGENTS.md](rust/AGENTS.md) |
| `githooks/` | `pre-commit` and `pre-push`. They run `bin/quality-gate.sh`. | [CONTRIBUTING.md](CONTRIBUTING.md) |
| `.github/` | The pull request gate, the release run and `!retest`. | [CONTRIBUTING.md](CONTRIBUTING.md) |
| `docs/` | The writing standard. | [docs/writing-standard.md](docs/writing-standard.md) |

## 4. Where to look for a capability

| You want to | Look in |
|---|---|
| Decide whether a tool call is allowed | `chaperone/src/chaperone/family_decisions.py` |
| Add a verb the chaperone executes itself | `chaperone/src/chaperone/verbs.py`, then `family_app.py` |
| Change what a family file may say | `family/src/agent_family/model.py` (shape), `validate.py` (values) |
| Change how a family change is priced as live or replace | `family/src/agent_family/classify.py` |
| Change a reconcile step | `caregiver/src/caregiver/steps.py`, `reconcile.py` |
| Change the sbx commands the host runs | `caregiver/src/caregiver/driver.py` |
| Change a session, turn or journal rule | `attendance/src/attendance/service.py`, `journal.py` |
| Change the channel protocol, host side | `attendance/src/attendance/wire.py`, `playpen_link.py` |
| Change the channel protocol, sandbox side | `playpen/src/protocol.ts`, `playpen.ts` |
| Change how pi is started | `playpen/src/pi-args.ts`, the one builder |
| Change how a manifest becomes pi tools | `playpen/bridge/tools.ts` |
| Change a page or the edit form | `noticeboard/src/noticeboard/pages.py`, `familyform.py` |
| Change the release steps | `handover/src/handover/executor/steps.py` |
| Change what a `component.yaml` may say | `handover/src/handover/manifest.py` |
| Change how a merge allocates a tag | `handover/src/handover/allocate.py`, `handover/bin/allocate-tags.sh` |
| Change when a new tag files a request | `handover/src/handover/follow/`, `systemd/creche-follow.service` |
| Change what a deploy does | `bin/creche-deploy` |
| Change the quality gate or the docs rule | `bin/quality-gate.sh`, `bin/lib/docsrule.sh` |
| Change the Rust checks, or the rule that starts them | `bin/rust-gate.sh`, `bin/lib/rustrule.sh` |
| Change the coverage rule of the Rust code | `bin/rust-coverage.sh`, `rust/coverage-files.txt` |
| Add a Rust type for a contract | `rust/crates/creche-contracts/`. Read `rust/AGENTS.md` first. |
| Change the index schema | `library/src/library/library.py` and `playpen/bridge/index-store.ts`, together |

## 5. Develop

Prerequisites: Python 3.12, `uv`, for `playpen/` Node 24 with `pnpm`, and
for `rust/` rustup.

```bash
git config core.hooksPath githooks     # once per clone
uv sync                                # every workspace member, one venv
bin/quality-gate.sh                    # ruff, ruff format, pyright
uv run pytest -n auto                  # every Python suite in pyproject.toml
cd playpen && pnpm install && pnpm test && pnpm run typecheck && pnpm run build
uv run pytest integration/tests -m slow   # the cross-package gate, after pnpm build
bin/rust-gate.sh --tests               # the Rust workspace: fmt, clippy, deny, test
```

No test needs a host, a sandbox, LiteLLM or a model. Every external thing
has a fake behind a small protocol. Keep it that way.

The playpen build needs `AGENT_LAN_ADDRESS` in the environment. CI sets
`192.0.2.10` and throws the bundle away.

## 6. Deploy and release

The two verbs differ, and the difference is a security property.

- **A deploy** puts this repository at `/opt/creche`, fills its venv and
  restarts one unit: the chaperone. `sudo creche-deploy <ref>` is the single
  sudoers entry on the host.
- **A release** replaces one component tree under `/opt/components/<name>`
  or `~/.local/components/<name>`, then restarts that component's unit.
  The `handover` executor does this as root, from a request file, after a
  phone tap. Every other service runs from its own component tree.
- **A request** comes from the operator, from one fenced agent family, or
  from `creche-follow.timer`. The timer files a request for each newer tag
  of a component that has a release. No requester can approve a release.

Everything that differs between deployments is in the site file,
`/etc/creche/site.env`. This repository names no host, no account and no
owner. A missing site value refuses. It never falls back to a default.

Versions are allocated at merge. No number lives in a file. A `bump:minor`
or `bump:major` label on the pull request raises the next tag of each
component the pull request changes. The root `pyproject.toml` stays at
`0.0.0`.

## 7. Invariants

The design holds twenty invariants. The ten that most code cites:

1. Authority lives only in the control plane.
2. Deny by default on every axis: mounts, egress, tools, approvals.
3. Credentials die before processes.
4. No LLM in the chaperone. A decision is deterministic.
5. One key per family.
6. Every tool result is untrusted data. It is rendered as data.
7. No secret ever sits on a process's argv or in a URL.
8. The agent process is assumed hostile.
9. A bad family file produces a report, not a crash.
10. One file defines a family. One view is the truth.

## 8. Repository rules

- Issues are off. Pull requests from forks are not accepted.
- Security reports: `SECURITY.md`.
- License: MIT, `LICENSE`.
- Contributor rules: `AGENTS.md`, then `CONTRIBUTING.md`.
