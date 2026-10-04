"""The root executor: `docs/rework/stage7-releases.md` §2.4.

Everything in this sub-package runs as ROOT on the host, which is the one
place in `handover` that touches a host. The rest of the package is pure
and stays that way (`handover/AGENTS.md`, "the rule that contains every other
one"): the resolver computes, the executor acts, and the split is what holds
when the requester is hostile (§2.1, §6).

Module map, in the order one release walks through them:

| Module | What it owns |
|---|---|
| `layout` | Root's own root: every path root writes, under root's parents. |
| `spool` | The five directories, `O_NOFOLLOW` everywhere, the lock. |
| `request` | §2.3's request, parsed as hostile bytes. |
| `live_state` | Contract 06 §11's document, held to the version stamps. |
| `provenance` | P1 to P5 against the GitHub API, fail closed. |
| `approval` | The gate id, the summary, the transport seam. |
| `quiesce` | Step 7: no turn mid-start. |
| `host` | The one place a child process starts. No shell, ever. |
| `install` | Steps 8 to 10 for one component. |
| `ledger` | §2.6's entry. |
| `steps` | The ten steps in order. |
| `drain` | The unit's entry point: every request, then the ledger. |

Every import in this sub-package sits at module top. A release re-syncs the
venv under the running executor (contract 06 §1.1), so an import resolved
half-way through a run reads the tree that is being replaced.
"""

from __future__ import annotations
