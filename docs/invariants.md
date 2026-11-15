# Invariants

Date: 2026-11-14. These 20 rules are the operator's own words, lightly
edited for the names the platform has now. Every contract and every
`AGENTS.md` traces its rules back to a number here. The contracts cite an
invariant by its number.

The enforcement point is the chaperone. A thin agent is an agent whose job is
one purpose. An attended agent has a person watching. An autonomous agent
runs on a trigger.

## A. Sessions

1. A session always exists. It lives on the host. It outlives its sandbox, any
   permission change and any deploy.
2. Many sessions run at once. Sessions of one family share a sandbox.
3. One session, every UI. A chat client session and a TUI session within the same
   family are the same session.
4. A session keeps working after the UI disconnects.
5. A sandbox is disposable, and a session file holds conversation only.
   Permissions come from the family's current definition on every turn.

## B. Families and permissions

6. One runtime. Attended families, thin agents and autonomous agents run the
   same way. They differ in who starts a session, how long it lives, what it
   may reach, and what inherent instructions it has.
7. The family is the only unit of permission. Every session in a family has the
   same permissions. Folders change prompts only.
8. Permission breadth follows who watches: attended = broad, thin = one
   purpose, autonomous = narrow plus phone approval.
9. A permission change needs one action and no manual apply. It never ends a
   session. A removal applies at once.
10. Only the operator and the platform family can change the platform or any
    family's permissions. A platform release still needs the operator's phone
    approval.

## C. Enforcement

11. Deny by default. A family reaches only what its definition grants.
12. Enforcement sits outside the sandbox: mounts, egress, the chaperone, the model key. The
    agent process is untrusted.
13. Credentials die before processes. Upstream credentials never enter a
    sandbox, a session file, argv or a URL.
14. No LLM inside the enforcement point (the chaperone). Tool results and delegated answers are
    untrusted data.
15. Every action leaves an audit record. A thin agent leaves nothing else.

## D. Management

16. One file defines a family. A change to what a family can do never needs a
    second edit elsewhere.
17. One approved action releases the platform. No hand-run steps follow it. The
    platform release is composed of smaller, targeted releases. Subsets of the
    platform release independently of the whole.
18. Adding an MCP server is one action.
19. A bad definition yields a report. It never crashes the system and never
    half-applies.
20. One view shows every family, sandbox and session. That view is the truth.

# Rules that the code does not hold yet

Each row is a decided rule. The code follows. Until the row leaves, the code
is the fact. The rows come from a check of the code on 2026-11-14. A row that
the code now holds is not here. A row keeps its old number, so a number can
have no row.

| # | The rule | What the code does until then | The row leaves when |
|---|---|---|---|
| 2 | A time is an RFC 3339 date-time in UTC with `Z` (`AGENTS.md`, "Time"). | `door-trigger/src/agent_door_trigger/fire.py` writes `fired_at` with the offset `+00:00`. | each writer writes `Z` |
| 3 | A service that refuses its config exits with status 78, and its unit holds `RestartPreventExitStatus=78`. | `caregiver`, `env` and `memory` exit with 78. Only `creche-memory.service` and `creche-vmm.service` hold the line. The units of `attendance`, `caregiver`, `noticeboard` and the doors do not. | each service and each unit hold the rule |
| 5 | Every file kind is TOML (decision D0.1). | The component manifest is `component.yaml`, and `handover` reads it as YAML. | the manifest is TOML |
| 6 | Contract 01 section 3.15: the field `quiet.daily.zone` is refused. | `DailyCall` in `family/src/agent_family/model.py` requires the field and the validator checks it. | a file that holds the field is refused |
| 10 | Contract 03 section 8: each cap counts bytes. | Rust, Python and TypeScript count code points for a text cap (`agent/src/wire-read.ts`). | each side counts bytes |
| 11 | Contract 04 section 6.5 (the chaperone's grants and audit): each audit record has a `family` key. | The audit record of the chaperone has no `family` key. | the chaperone writes the key |
| 16 | The range of an epoch: a reader of the credential file refuses zero and a negative epoch. | `caregiver/src/caregiver/credentials.py` reads the epoch with `int(...)` and keeps no range. It has a `CONTRACT-QUESTION` for the types. | `caregiver` holds the range |

Rows that left at this check: 1 (strict JSON), 4 (addresses), 7 (the mount
swap), 8 (`stop_ack`), 13 (the `caregiver` token), 15 (the three grammars),
17 (limits of `protocol`) and 18 (one roster). The code holds each of them,
or the part of the code that broke them is gone.
