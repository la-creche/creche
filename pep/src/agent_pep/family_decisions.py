"""The family path's decision core: contract 04 §5, in order.

Pure and synchronous: no I/O, no clock, no globals. The
whole reachable-set question is a function of its inputs, and the suite holds
this module at 100% branch coverage (`pep/AGENTS.md`). Every row of §5 is a
row of `docs/pep-decisions.md`.

First match wins:

| # | Condition | Reason |
|---|---|---|
| 1 | no bearer, unknown digest, missing/malformed/unknown-version grants | `unknown_token` |
| 2 | over `limits.pep_rpm` | `rate_limited` |
| 3 | an upstream fence refuses an argument | `arg_validation` |
| 4 | the tool is in neither `tools` nor `verbs`, nor a granted delegate | `tool_not_granted` |
| 5 | arguments fail the schema or a verb's fence | `arg_validation` |
| 6 | `invoke_agent` target outside `delegates`, or a second hop | `tool_not_granted` |
| 7 | `invoke_agent` over `max_inflight_delegations` | `rate_limited` |
| 8 | gated, and the family already holds `max_open_gates` | `rate_limited` |
| 9 | gated | allow with `approval`, which the app layer blocks on |
| 10 | otherwise | allow, or a seam |

Row 10's seams answer `not_implemented`, the way the PEP already
treats a granted tool that does not exist yet. The policy above each seam is
real and tested: only the execution is missing.

Four flags say what this process can execute, and all default to off:
`delegate_ready` for `attendance`'s delegate door (§7), `approvals_ready` for
the approval transport (§8), `dispatch_ready` for `attendance`'s dispatch door
(contract 02 §13.4, which serves `enqueue` and `job_status`) and
`release_ready` for root's release spool (`stage7-releases.md` §2.3, which
serves `release`). A PEP without one keeps the seam rather than allowing a
call it would then fail to run. That is the same fail-closed rule, applied to
configuration.

Nothing here reads an advisory header. The decision reads the family from the
token, the grant file, the call's tool and its arguments, and nothing else
(contract 04 §3.1 rule 1, invariants 12 and 14).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final, Literal

from . import fences as upstream_fences
from . import verbs
from .delegations import MAX_DELEGATION_HOPS
from .family_grants import FamilyGrants
from .family_ids import INVOKE_AGENT, MCP_TOOL_SEPARATOR
from .fences import NO_FENCES, ServerFences

FamilyReason = Literal[
    "unknown_token",
    "rate_limited",
    "arg_validation",
    "tool_not_granted",
    "not_implemented",
    "upstream_failed",
    "internal_error",
    "delegate_timeout",
    "approval_denied",
    "approval_timeout",
    "approval_revoked",
    "approval_undeliverable",
    "approval_abandoned",
]

#: Contract 04 §5's HTTP column, plus the outcomes §6.1 names that are not
#: decision rows. `not_implemented` is the PEP's own code for a granted tool
#: whose execution has not shipped. `delegate_timeout` is §7.5's 504, and the
#: five `approval_*` values are how §8 ends a gate that did not approve.
#: `approval_abandoned` never reaches a client — §8.7's caller is already gone
#: — and it carries a status so one table answers for every reason.
FAMILY_DENY_STATUS: Final[dict[FamilyReason, int]] = {
    "unknown_token": 403,
    "rate_limited": 429,
    "arg_validation": 400,
    "tool_not_granted": 403,
    "not_implemented": 501,
    "upstream_failed": 502,
    "internal_error": 500,
    "delegate_timeout": 504,
    "approval_denied": 403,
    "approval_timeout": 403,
    "approval_revoked": 403,
    "approval_undeliverable": 403,
    "approval_abandoned": 403,
}

#: Contract 04 §6.1's `reason` on an allowed call.
REASON_GRANTED: Final = "granted"


class Executor(Enum):
    """Who runs an allowed call. The app layer dispatches on this."""

    MCP = "mcp"
    VERB = "verb"
    DELEGATE = "delegate"


@dataclass(frozen=True)
class FamilyDecision:
    allow: bool
    reason: FamilyReason | None = None
    detail: str | None = None
    executor: Executor | None = None
    server: str | None = None
    tool: str | None = None
    args: dict[str, object] | None = None
    #: Contract 04 §5 row 9. The decision allows the call and the app layer
    #: holds it at a gate until a phone decision arrives (§8). The core says
    #: whether a gate is owed, never how long one waits: that is a clock.
    approval: bool = False


def deny(reason: FamilyReason, detail: str | None = None) -> FamilyDecision:
    return FamilyDecision(allow=False, reason=reason, detail=detail)


#: The seam a PEP with no approval transport keeps (§4 rule 4, §5 row 5a).
NO_APPROVAL_TRANSPORT: Final = "this PEP has no approval transport configured"
NO_DELEGATE_DOOR: Final = "this PEP has no delegate door configured"
NO_DISPATCH_DOOR: Final = "this PEP has no dispatch door configured"
NO_RELEASE_SPOOL: Final = "this PEP has no release spool configured"


def _gate_denial(
    grants: FamilyGrants, action: str, open_gates: int, approvals_ready: bool
) -> FamilyDecision | None:
    """Rows 8 and 9's refusals. None means the call may go on, gated or not.

    `caregiver` expanded `<server>__*` before it wrote the file, so the action
    name is compared whole.
    """
    if action not in grants.approval:
        return None
    if open_gates >= grants.limits.max_open_gates:
        return deny("rate_limited", "max_open_gates reached for this family")
    if approvals_ready:
        return None
    return deny("not_implemented", NO_APPROVAL_TRANSPORT)


def _decide_mcp(
    grants: FamilyGrants,
    tool: str,
    args: dict[str, object],
    fences: ServerFences,
    known_servers: frozenset[str],
    open_gates: int,
    approvals_ready: bool,
) -> FamilyDecision:
    server, _, bare = tool.partition(MCP_TOOL_SEPARATOR)
    if not server or not bare:
        return deny("tool_not_granted", f"{tool!r} is not a well-formed MCP tool name")

    # Row 3 before row 4: the fence is on the upstream by name, so it holds for
    # a family that was never granted that server (contract 01b §7).
    refused = upstream_fences.refuse(fences, server, bare, args)
    if refused is not None:
        return deny("arg_validation", refused)

    granted = grants.tools.get(server)
    if granted is None:
        return deny("tool_not_granted", f"MCP server {server!r} is not granted")
    if bare not in granted:
        return deny("tool_not_granted", f"{bare!r} is not granted on {server!r}")

    refused_gate = _gate_denial(grants, tool, open_gates, approvals_ready)
    if refused_gate is not None:
        return refused_gate

    if server not in known_servers:
        return deny("not_implemented", f"server {server!r} is not loaded on this PEP")

    return FamilyDecision(
        allow=True,
        executor=Executor.MCP,
        server=server,
        tool=bare,
        args=args,
        approval=tool in grants.approval,
    )


def _decide_verb(
    grants: FamilyGrants,
    tool: str,
    args: dict[str, object],
    open_gates: int,
    approvals_ready: bool,
    dispatch_ready: bool,
    release_ready: bool,
) -> FamilyDecision:
    fence = grants.verbs.get(tool)
    if fence is None:
        return deny("tool_not_granted", f"{tool!r} is not granted to this family")
    if tool not in verbs.VERB_CATALOG:
        # The grant file names a verb this PEP's catalog does not hold, so
        # there is no schema to decide against. Deny by default.
        return deny("tool_not_granted", f"{tool!r} is not in the verb catalog")

    failed = verbs.check_schema(tool, args)
    if failed is None:
        failed = verbs.check_fence(tool, args, fence)
    if failed is not None:
        return deny(failed.reason, failed.detail)

    refused_gate = _gate_denial(grants, tool, open_gates, approvals_ready)
    if refused_gate is not None:
        return refused_gate

    seam = verbs.VERB_SEAMS.get(tool)
    if seam is not None:
        return deny("not_implemented", f"{tool}: {seam}")

    # Contract 02 §13.4. Like the delegate door, a missing dispatch door is a
    # configuration answer: the policy above ran in full and this process
    # cannot make the call.
    if tool in verbs.DISPATCH_VERBS and not dispatch_ready:
        return deny("not_implemented", f"{tool}: {NO_DISPATCH_DOOR}")

    # `stage7-releases.md` §2.3, the same rule again: a PEP with no spool
    # cannot write a request, so it does not allow one.
    if tool in verbs.SPOOL_VERBS and not release_ready:
        return deny("not_implemented", f"{tool}: {NO_RELEASE_SPOOL}")

    return FamilyDecision(
        allow=True,
        executor=Executor.VERB,
        tool=tool,
        args=args,
        approval=tool in grants.approval,
    )


def _decide_delegate(
    grants: FamilyGrants,
    args: dict[str, object],
    inflight: int,
    open_gates: int,
    hops: int,
    delegate_ready: bool,
    approvals_ready: bool,
) -> FamilyDecision:
    """Row 4 for the synthesized tool, then rows 5, 6, 7 and 9 (§7.2)."""
    if not grants.delegates:
        return deny("tool_not_granted", "invoke_agent is not granted (delegates is empty)")

    failed = verbs.check_schema(INVOKE_AGENT, args)
    if failed is not None:
        return deny(failed.reason, failed.detail)

    target = args.get("family")
    if target not in grants.delegates:
        return deny("tool_not_granted", f"{target!r} is not in delegates")

    # Contract 01 §3.6 rule 4 keeps a chain to one hop by making a thin
    # family's `delegates` empty. The PEP holds the same bound itself, because
    # deny by default does not rest on another file being written correctly.
    if hops >= MAX_DELEGATION_HOPS:
        return deny("tool_not_granted", "invoke_agent is one hop, and this call is already a hop")

    if inflight >= grants.limits.max_inflight_delegations:
        return deny("rate_limited", "max_inflight_delegations reached")

    refused_gate = _gate_denial(grants, INVOKE_AGENT, open_gates, approvals_ready)
    if refused_gate is not None:
        return refused_gate

    if not delegate_ready:
        return deny("not_implemented", f"invoke_agent: {NO_DELEGATE_DOOR}")

    return FamilyDecision(
        allow=True,
        executor=Executor.DELEGATE,
        tool=INVOKE_AGENT,
        args=args,
        approval=INVOKE_AGENT in grants.approval,
    )


def decide_family(
    grants: FamilyGrants | None,
    tool: str,
    args: dict[str, object],
    *,
    rate_exceeded: bool,
    fences: ServerFences = NO_FENCES,
    known_servers: frozenset[str] = frozenset(),
    inflight: int = 0,
    open_gates: int = 0,
    hops: int = 0,
    delegate_ready: bool = False,
    approvals_ready: bool = False,
    dispatch_ready: bool = False,
    release_ready: bool = False,
) -> FamilyDecision:
    """Contract 04 §5, first match wins. See the module docstring for the rows."""
    if grants is None:
        return deny("unknown_token")
    if rate_exceeded:
        return deny("rate_limited", "pep_rpm exceeded")

    if MCP_TOOL_SEPARATOR in tool:
        return _decide_mcp(grants, tool, args, fences, known_servers, open_gates, approvals_ready)

    if tool == INVOKE_AGENT:
        return _decide_delegate(
            grants, args, inflight, open_gates, hops, delegate_ready, approvals_ready
        )

    return _decide_verb(
        grants, tool, args, open_gates, approvals_ready, dispatch_ready, release_ready
    )


def is_granted(grants: FamilyGrants, tool: str) -> bool:
    """Whether the family still holds this action at all (contract 04 §1.5.3).

    Narrower than `decide_family` on purpose: a gate the human is already
    looking at re-asks the reach question and nothing else, so a rate limit or
    an in-flight count cannot cancel an approval that is only slow to arrive.
    """
    if MCP_TOOL_SEPARATOR in tool:
        server, _, bare = tool.partition(MCP_TOOL_SEPARATOR)
        return bare in grants.tools.get(server, ())

    if tool == INVOKE_AGENT:
        return bool(grants.delegates)

    return tool in grants.verbs


def manifest_actions(
    grants: FamilyGrants,
    known_servers: frozenset[str],
    *,
    delegate_ready: bool = False,
    approvals_ready: bool = True,
    dispatch_ready: bool = False,
    release_ready: bool = False,
) -> list[str]:
    """Which action names this family's manifest offers (contract 04 §4).

    Names only: the app layer joins an MCP name to the upstream's schema and a
    verb name to the catalog's.

    The manifest only advertises what `/call` can execute, which is the rule
    the PEP already holds for a granted-but-unimplemented tool. So a server
    this PEP has not loaded, a verb this PEP cannot execute, and a gated
    action on a PEP with no approval transport are all absent.

    `invoke_agent` is synthesized once when `delegates` is non-empty (§4 rule
    1) and this PEP holds a delegate door. It is never granted by name.
    """
    names: list[str] = []
    for server in sorted(grants.tools):
        if server not in known_servers:
            continue
        for bare in grants.tools[server]:
            names.append(f"{server}{MCP_TOOL_SEPARATOR}{bare}")

    offered = (
        verbs.IMPLEMENTED_VERBS
        | (verbs.DISPATCH_VERBS if dispatch_ready else frozenset[str]())
        | (verbs.SPOOL_VERBS if release_ready else frozenset[str]())
    )

    for verb in sorted(grants.verbs):
        if verb in offered:
            names.append(verb)

    if grants.delegates and delegate_ready:
        names.append(INVOKE_AGENT)

    if approvals_ready:
        return names

    return [name for name in names if name not in grants.approval]
