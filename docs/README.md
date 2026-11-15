# Documents

Date: 2026-11-14. This index lists each living document of this repository.
Read `writing-standard.md` before you write or change one.

## Start here

| Document | What it holds |
|---|---|
| [writing-standard.md](writing-standard.md) | The writing standard. Every Markdown document follows it. |
| [invariants.md](invariants.md) | The 20 invariants, and the rules that the code does not hold yet. |
| [decisions/consolidation.md](decisions/consolidation.md) | Each decision of the consolidation, with its status. |
| [fleet-bootstrap.md](fleet-bootstrap.md) | How to bring a new host up. |

## Contracts

A contract fixes one interface. The code is the fact when a contract and the
code disagree. `invariants.md` lists each known disagreement.

| Contract | What it fixes |
|---|---|
| [01 family file](contracts/01-family-file.md) | The family file, its checks and what applies live. |
| [03 channel protocol](contracts/03-channel-protocol.md) | The link between the host and the guest facade. |
| [05 manager status](contracts/05-manager-status.md) | The status document of `caregiver` and the faults. |

Other contracts are not in this repository. Contract 01b (the server file),
contract 02 (the session API), contract 04 (grants and audit) and contract 06
(the component manifest) are in a private repository. A citation of one of
them, or of `spec.md`, resolves there. Keep the citation as it is.

## Design notes

A design note records why a change was made. The status line at the top of
each note says what is true now. A note keeps the text of its date. A later
decision can replace a row.

| Note | Subject |
|---|---|
| [cloud-hypervisor-playpen.md](design/cloud-hypervisor-playpen.md) | The move of the playpen to Cloud Hypervisor. The rows CH1 and so on. |
| [mcp-memory.md](design/mcp-memory.md) | The memory service. |
| [browser-mcp.md](design/browser-mcp.md) | A browser tool for an agent. |
| [ci-runner.md](design/ci-runner.md) | One throwaway VM per Forgejo Actions job. The rows R1 to R6. |
| [openshell-ideas.md](design/openshell-ideas.md) | Ideas taken from OpenShell. |
| [family-isolation.md](design/family-isolation.md) | The fences between families. |
| [code-creche-split.md](design/code-creche-split.md) | The two attended coding families. |
| [creche-release.md](design/creche-release.md) | The home of the host release process. |
| [tags-on-merge.md](design/tags-on-merge.md) | How a merge allocates a tag. |
| [phase4-repo-names.md](design/phase4-repo-names.md) | The change of the repository names. |
| [agent-control-removal-plan.md](design/agent-control-removal-plan.md) | The removal of the old family name. |

A design note names host names, addresses and accounts as placeholders. The
operator's private notes hold the real values. A note can cite a file of the
operator's private `reframe/` folder. That file is not in this repository.

## Rules of each directory

Each directory with code has an `AGENTS.md`. `AGENTS.md` at the root lists
them in the table "Where the rules for a directory live".
