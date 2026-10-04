"""The family path's service layer: one manifest, one call, one audit line.

Contract 04 §4, §5 and §6. `app.py` owns HTTP and hands this layer a bearer
token, a tool name, arguments and the request's headers. Nothing here builds a
response object or reads a `Request`, so the decision path is testable without
a client (`AGENTS.md`: a layer talks only to the layer directly below).

```
 bearer --> FamilyStore.lookup --> grants or None
                                     |
   None -> app.py: 403, unidentified  `-> rate --> decide_family --> execute
                                                       |                |
                                                       `-- audit v2 ----'
```

The family is decided by where the token resolves, never by a header or a
body field (contract 04 §3.1 rule 1).

This layer executes the granted MCP tools, `embed`, `ha_call`, `invoke_agent`
(§7) and any granted action that needs a phone tap (§8). A gated call takes
the same path with one wait in the middle: the decision is made, the gate
opens, the HTTP call is held here, and only a tap runs it.

It also executes `enqueue` and `job_status` through `attendance`'s dispatch
door (contract 02 §13.4).

`release` and a server this PEP has not loaded are named seams: the decision
runs in full and the last row answers `not_implemented`, the way the PEP
already treats a granted tool that does not exist yet. So are the three
configurable executions when their configuration is missing — a PEP with no
delegate door, no dispatch door or no phone rail keeps the seam rather than
allowing a call it would fail to run.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Final, cast

import httpx

from .audit import AuditError
from .delegate import DelegateDoor, DelegateRequest, DelegateStatus, wrap_untrusted
from .delegations import DelegationTable
from .dispatch import DispatchDoor, DispatchRefused, DispatchRequest, JobQuery
from .family_audit import AuditEntry, FamilyAudit, Outcome, can_hold, resolve_sandbox
from .family_decisions import (
    FAMILY_DENY_STATUS,
    REASON_GRANTED,
    Executor,
    FamilyDecision,
    FamilyReason,
    decide_family,
    deny,
    is_granted,
    manifest_actions,
)
from .family_grants import FamilyGrants, FamilyStore
from .family_ids import INVOKE_AGENT, MCP_TOOL_SEPARATOR
from .gatekeeper import Gatekeeper, GateOutcome, GateTicket, Reach
from .gates import approval_summary, gate_id
from .headers import Claimed, read_claimed
from .mcp_client import UpstreamError, UpstreamPool
from .release_door import (
    CORPUS_NOTE,
    RELEASE_KIND,
    ReleaseDoor,
    ReleaseRefused,
    ReleaseRequest,
    ReleaseSpoolError,
)
from .reload_pool import NOTHING_SERVES, Serving
from .verbs import EMBED, ENQUEUE, HA_CALL, JOB_STATUS, RELEASE, VERB_CATALOG, manifest_schema

log = logging.getLogger("chaperone.family_app")

HTTP_OK: Final = 200

#: Contract 04 §4.2. The manifest a sandbox already holds is still current, so
#: the PEP serves nothing and says so.
HTTP_NOT_MODIFIED: Final = 304

#: RFC 9110 §13.1.2, looked up folded like every other header this file reads.
IF_NONE_MATCH_HEADER: Final = "if-none-match"

#: `FamilyGrants.rev` is capped at 128 characters, so a longer header cannot
#: be a revision this PEP serves and is refused before it is compared
#: (invariant 12).
MAX_ETAG_CHARS: Final = 160

#: Contract 04 §6.1's `reason` when an allowed call's upstream failed (§5 row
#: 11). The decision stays an allow: the effect may already have happened.
REASON_UPSTREAM_FAILED: Final[FamilyReason] = "upstream_failed"

#: §7.5's own outcome, kept apart from `upstream_failed` so a 120 second
#: delegate and a broken upstream never read alike in the audit.
REASON_DELEGATE_TIMEOUT: Final[FamilyReason] = "delegate_timeout"

#: §8.7 rule 4's resolving record. The caller is gone, so no client ever reads
#: this one: it exists so the audit says why the gate ended.
REASON_ABANDONED: Final[FamilyReason] = "approval_abandoned"

#: §6.1's `reason` on the two records a gated call writes (§6.4). The contract
#: names `approved` for the second and nothing for the first, so the first
#: takes the word the retired instance path wrote.
REASON_PENDING: Final = "approval_required"
REASON_APPROVED: Final = "approved"

#: The detail of a call that is refused because no audit line can hold its
#: arguments. A fixed text: it does not quote them.
ARGS_NOT_HELD: Final = (
    "the audit cannot hold these arguments: they nest too deep, "
    "or they hold a string that is not Unicode text"
)

#: The detail of a result that no reply can carry. It does not quote it.
RESULT_NOT_JSON: Final = "the result is not JSON text that a reply can carry"

#: The PEP's own audit names for the two endpoints that are not tool calls.
#: A `$` cannot start a tool name, so neither can collide with one.
MANIFEST_ACTION: Final = "$manifest"

#: One rate window object serves every key, so one process holds one bound.
#: The prefix stays so a second namespace never collides with this one
#: (contract 04 §5).
FAMILY_RATE_PREFIX: Final = "f/"

#: The local embedding service answers in well under a second on a warm model.
#: `/info` is a single small read.
TEI_EMBED_TIMEOUT_S: Final = 30.0
TEI_INFO_TIMEOUT_S: Final = 10.0
#: The PEP has run this call against the real Home Assistant with this
#: timeout.
HA_CALL_TIMEOUT_S: Final = 15.0

#: TEI serves one model, so `/info` answers the same id every time.
UNKNOWN_MODEL: Final = "unknown"


class ExecutionFailed(Exception):
    """An upstream or network failure after an allow. Contract 04 §5 row 11:
    audited as an allowed call that failed, never as a policy denial."""


class DelegateTimedOut(Exception):
    """Contract 04 §7.5: the delegate call passed 120 seconds. Like an
    upstream failure it follows an allow, and it carries its own reason so a
    15 second stall and a 120 second one never read alike."""


class NotImplementedSeam(Exception):
    """An allow this PEP cannot execute. Unreachable while the decision core
    and this executor agree, and fail-closed if they ever stop agreeing."""


@dataclass(frozen=True)
class Reply:
    """What `app.py` turns into a JSON response."""

    status: int
    payload: dict[str, object]


#: `(rate key, limit per minute) -> over the limit`. The clock belongs to the
#: caller, so this layer holds none.
RateCheck = Callable[[str, int], bool]


def _nothing_serves() -> Serving:
    """`FamilyDeps.serving` for a layer built with no upstream behind it."""
    return NOTHING_SERVES


@dataclass(frozen=True)
class FamilyDeps:
    """What this layer needs from the process around it.

    `pool` and `client` are read through a callable because `app.py` builds
    both inside its lifespan, after this object exists. `serving` is one for
    a second reason: a reload moves it.
    """

    store: FamilyStore
    audit: FamilyAudit
    rate: RateCheck
    pool: Callable[[], UpstreamPool | None]
    client: Callable[[], httpx.AsyncClient | None]
    #: The upstreams that serve and their fences, read once per decision.
    #: With a reloadable pool it is that pool's own reading, so
    #: the decision and the calls follow one roster.
    serving: Callable[[], Serving] = _nothing_serves
    tei_url: str = ""
    ha_url: str = ""
    secrets: Mapping[str, str] = field(default_factory=dict[str, str])
    #: Contract 04 §7. `None` leaves `invoke_agent` a seam: the decision runs
    #: in full and the last row answers `not_implemented`, so a PEP without a
    #: door never allows a call it cannot make.
    delegate_door: DelegateDoor | None = None
    #: The PEP's own record of the delegate calls it started (§6.3, §7.4).
    delegations: DelegationTable = field(default_factory=DelegationTable)
    #: Contract 04 §8. `None` leaves a gated action a seam: the decision runs
    #: in full and the last row answers `not_implemented`, so a PEP with no
    #: phone rail never opens a gate it could not resolve.
    gatekeeper: Gatekeeper | None = None
    #: Contract 02 §13.4. `None` leaves `enqueue` and `job_status` seams, for
    #: the same reason `delegate_door` leaves `invoke_agent` one.
    dispatch_door: DispatchDoor | None = None
    #: `stage7-releases.md` §2.3. `None` leaves `release` a seam, same rule:
    #: a PEP with no spool never allows a call it cannot write.
    release_door: ReleaseDoor | None = None


class FamilyGate:
    """Serves a bearer that resolves to a family, and nothing else."""

    def __init__(self, deps: FamilyDeps) -> None:
        self._deps = deps
        self._tei_model: str | None = None

    def lookup(self, token: str) -> FamilyGrants | None:
        """The family a bearer resolves to. `None` means this layer has no
        business with the request, and `app.py` carries on."""
        return self._deps.store.lookup(token)

    def sweep_retention(self) -> None:
        """Contract 04 §6: 180 days, swept daily. Driven by `app.py`'s
        lifespan, on the same schedule as the unidentified log."""
        self._deps.audit.sweep_retention()

    def sweep_faults(self) -> frozenset[str]:
        """Contract 04 §1.6 rule 7: re-read a faulted family's grant file with
        no call to drive it. Answers the families whose fault that cleared."""
        return self._deps.store.sweep_faults()

    # ---- the two endpoints -------------------------------------------------

    def manifest(self, grants: FamilyGrants, headers: Mapping[str, str]) -> Reply:
        """Contract 04 §4. The tool list this family may act with.

        §4.2: a sandbox asks again for the life of its pi process, so that a
        grant the operator ADDS reaches the chat they are in. A request that names the
        revision already served answers 304 and nothing else.
        """
        claimed = read_claimed(headers)
        if self._over_rate(grants):
            return self._refuse(grants, MANIFEST_ACTION, {}, deny("rate_limited"), claimed)

        # The rate window counts a revalidation exactly as it counts a fetch,
        # which is what bounds the poll without a second limit to reason
        # about. The AUDIT is the part that is skipped: nothing was served, so
        # a `$manifest` allow would claim a manifest this family never
        # received, and one line per process per minute would bury §6's log.
        if _holds_current_rev(headers, grants.rev):
            return Reply(HTTP_NOT_MODIFIED, {})

        self._write(grants, MANIFEST_ACTION, {}, Outcome.ALLOW, REASON_GRANTED, claimed)
        return Reply(
            HTTP_OK,
            {
                "family": grants.family,
                "rev": grants.rev,
                "model_alias": grants.model_alias,
                "tools": self._tool_entries(grants),
                "limits": {
                    "pep_rpm": grants.limits.pep_rpm,
                    "max_inflight_delegations": grants.limits.max_inflight_delegations,
                },
            },
        )

    async def call(
        self,
        grants: FamilyGrants,
        tool: str,
        args: dict[str, object],
        headers: Mapping[str, str],
    ) -> Reply:
        """Contract 04 §5, then execution, then one audit line either way."""
        claimed = read_claimed(headers)
        try:
            return await self._answer(grants, tool, args, claimed)
        except AuditError:
            raise
        except Exception:
            # §5 row 12, for a failure that neither `_decide` nor `_run`
            # names. The call still gets its line and the contract's body.
            log.exception("the call raised; failing closed")
            return self._refuse(grants, tool, args, deny("internal_error"), claimed)

    async def _answer(
        self, grants: FamilyGrants, tool: str, args: dict[str, object], claimed: Claimed
    ) -> Reply:
        decision = self._decide(grants, tool, args, claimed)
        if not decision.allow:
            return self._refuse(grants, tool, args, decision, claimed)

        # The rule of `_run`'s probe, for the arguments: no effect where the
        # audit cannot record it. A denial above keeps its own reason, and
        # its line holds a marker for such arguments (`family_audit`).
        if not can_hold(args):
            unheld = deny("internal_error", ARGS_NOT_HELD)
            return self._refuse(grants, tool, args, unheld, claimed)

        if decision.approval:
            return await self._gated(grants, tool, args, decision, claimed)

        return await self._run(grants, tool, args, decision, claimed)

    async def _run(
        self,
        grants: FamilyGrants,
        tool: str,
        args: dict[str, object],
        decision: FamilyDecision,
        claimed: Claimed,
        *,
        gate: str | None = None,
        waited_ms: int = 0,
    ) -> Reply:
        """Execute an allowed call and record it. `gate` is set when a phone
        tap authorized this run, which changes the audit's reason and nothing
        else about the execution."""
        # A broken audit blocks the effect rather than letting it happen
        # unrecorded. `app.py` turns this into a 500.
        if not self._deps.audit.probe_writable():
            raise AuditError("rework audit directory is not writable")

        started = time.monotonic()
        try:
            payload = await self._execute(grants, decision, claimed)
            if not _carries(payload):
                raise ExecutionFailed(RESULT_NOT_JSON)
        except DispatchRefused as exc:
            # Contract 02 §13.4.1 rule 5: a refused dispatch created nothing.
            # So this is a policy denial and not §5 row 11's allowed call that
            # failed, and the audit says deny.
            return self._refuse(grants, tool, args, deny(exc.reason, exc.detail), claimed)
        except ReleaseRefused as exc:
            # The same shape: no request file was written, so nothing
            # happened and the audit says deny with §5's own reason.
            return self._refuse(grants, tool, args, deny(exc.reason, exc.detail), claimed)
        except DelegateTimedOut as exc:
            return self._failed_after_allow(
                grants, tool, args, claimed, started, exc, REASON_DELEGATE_TIMEOUT, gate, waited_ms
            )
        except (ExecutionFailed, UpstreamError, httpx.HTTPError, ValueError) as exc:
            return self._failed_after_allow(
                grants, tool, args, claimed, started, exc, REASON_UPSTREAM_FAILED, gate, waited_ms
            )
        except AuditError:
            raise
        except Exception:
            log.exception("family execution failed; failing closed")
            broken = deny("internal_error")
            return self._refuse(grants, tool, args, broken, claimed)

        reason = REASON_GRANTED if gate is None else REASON_APPROVED
        self._write(
            grants,
            tool,
            args,
            Outcome.ALLOW,
            reason,
            claimed,
            latency_ms=self._since(started),
            gate=gate,
            waited_ms=waited_ms,
        )
        return Reply(HTTP_OK, {"ok": True, "result": payload})

    # ---- approvals that block in place (§8) --------------------------------

    async def _gated(
        self,
        grants: FamilyGrants,
        tool: str,
        args: dict[str, object],
        decision: FamilyDecision,
        claimed: Claimed,
    ) -> Reply:
        """Contract 04 §8. The HTTP call waits here until the phone answers.

        Two audit records, as §6.4 says: one when the gate opens, one when it
        resolves. Both carry the gate, so the wait is visible in the log
        rather than inferred from a hole in the timeline.
        """
        keeper = self._deps.gatekeeper
        if keeper is None:
            # Unreachable while the core and this layer agree on the flag:
            # `approvals_ready` is exactly `keeper is not None`.
            raise ExecutionFailed("no approval transport on this PEP")

        settled = decision.args or {}
        gate = gate_id(grants.family, tool, settled)
        ticket = GateTicket(
            family=grants.family, tool=tool, gate=gate, summary=approval_summary(tool, settled)
        )

        self._write(grants, tool, args, Outcome.PENDING, REASON_PENDING, claimed, gate=gate)
        try:
            held = await keeper.hold(ticket, lambda: self._reach(grants.family, tool))
        except asyncio.CancelledError:
            # §8.7: the sandbox died while waiting. Nothing executes, and the
            # audit records why the gate ended rather than leaving a hole.
            self._write(grants, tool, args, Outcome.DENY, REASON_ABANDONED, claimed, gate=gate)
            raise
        except Exception:
            # §6.4's second record, for a wait that failed. Nothing executes.
            log.exception("the hold of gate %s raised; failing closed", gate)
            broken: FamilyReason = "internal_error"
            self._write(grants, tool, args, Outcome.DENY, broken, claimed, gate=gate)
            return Reply(
                FAMILY_DENY_STATUS[broken], {"ok": False, "reason": broken, "detail": None}
            )

        if held.outcome is GateOutcome.APPROVED:
            return await self._run(
                grants, tool, args, decision, claimed, gate=gate, waited_ms=held.waited_ms
            )

        reason = cast("FamilyReason", held.outcome.value)
        self._write(
            grants,
            tool,
            args,
            Outcome.DENY,
            reason,
            claimed,
            gate=gate,
            waited_ms=held.waited_ms,
        )
        return Reply(
            FAMILY_DENY_STATUS[reason],
            {"ok": False, "reason": reason, "detail": held.detail},
        )

    def _reach(self, family: str, tool: str) -> Reach:
        """Contract 04 §1.5.3, asked on a poll while a gate is held. The grant
        file is re-read by name, so a removal lands without a restart."""
        grants = self._deps.store.current(family)
        if grants is None or not is_granted(grants, tool):
            return Reach.REMOVED

        return Reach.GRANTED

    def _open_gates(self, family: str) -> int:
        """Contract 04 §5 row 8. A PEP with no rail holds no gate."""
        keeper = self._deps.gatekeeper
        if keeper is None:
            return 0

        return keeper.open_count(family)

    # ---- decision ----------------------------------------------------------

    def _over_rate(self, grants: FamilyGrants) -> bool:
        return self._deps.rate(FAMILY_RATE_PREFIX + grants.family, grants.limits.pep_rpm)

    def _known_servers(self, serving: Serving) -> frozenset[str]:
        """Which upstreams this PEP actually serves right now. A configured
        server that failed to start lists no tools, so it is not known, and a
        grant naming it answers `not_implemented` rather than reaching it."""
        pool = self._deps.pool()
        if pool is None:
            return frozenset()
        return frozenset(name for name in serving.names if pool.tools(name))

    def _decide(
        self, grants: FamilyGrants, tool: str, args: dict[str, object], claimed: Claimed
    ) -> FamilyDecision:
        delegations = self._deps.delegations
        try:
            # One reading for the names and the fences alike. A
            # call with no gate reaches the pool's choice of process with no
            # await in between, so the fence that admits it is the fence of
            # the row that runs it.
            serving = self._deps.serving()
            return decide_family(
                grants,
                tool,
                args,
                rate_exceeded=self._over_rate(grants),
                fences=serving.fences,
                known_servers=self._known_servers(serving),
                inflight=delegations.inflight(grants.family),
                open_gates=self._open_gates(grants.family),
                # The header is only an index into the PEP's own mint table
                # (§7.4), so an id it never minted counts as no hop at all.
                hops=delegations.hops(claimed.delegation_id, grants.family),
                delegate_ready=self._deps.delegate_door is not None,
                approvals_ready=self._deps.gatekeeper is not None,
                dispatch_ready=self._deps.dispatch_door is not None,
                release_ready=self._deps.release_door is not None,
            )
        except Exception:
            log.exception("decide_family() raised; failing closed")
            return deny("internal_error")

    # ---- execution ---------------------------------------------------------

    async def _execute(
        self, grants: FamilyGrants, decision: FamilyDecision, claimed: Claimed
    ) -> dict[str, object]:
        if decision.executor is Executor.MCP:
            return await self._call_mcp(decision)

        if decision.executor is Executor.DELEGATE:
            return await self._delegate(grants, decision.args or {}, claimed)

        if decision.tool == EMBED:
            return await self._embed(decision.args or {})

        if decision.tool == HA_CALL:
            return await self._ha_call(decision.args or {})

        if decision.tool == ENQUEUE:
            return await self._enqueue(grants, decision.args or {}, claimed)

        if decision.tool == JOB_STATUS:
            return await self._job_status(grants, decision.args or {})

        if decision.tool == RELEASE:
            return self._release(grants, decision.args or {}, claimed)

        raise NotImplementedSeam(f"{decision.tool!r} was allowed and cannot be executed")

    def _release(
        self, grants: FamilyGrants, args: dict[str, object], claimed: Claimed
    ) -> dict[str, object]:
        """Contract 04 §4.1's `release`. One file into root's spool, and the
        request id back (`stage7-releases.md` §2.3).

        Synchronous, and deliberately: it is one 300-byte write into a
        directory this process already holds open, the same blocking profile
        as the audit line every call writes anyway. Nothing waits on root.

        `requested_by` is the family the BEARER resolved to, never an
        argument, so a caller cannot file under another family's name. Root
        still treats the field as a claim (§3.2).
        """
        door = self._deps.release_door
        if door is None:
            raise ExecutionFailed("no release spool on this PEP")

        components = args.get("components")
        asked = ReleaseRequest(
            family=grants.family,
            session=claimed.session_id,
            components=cast("dict[str, str]", components) if isinstance(components, dict) else {},
            kind=str(args.get("kind", RELEASE_KIND)),
            rollback_of=_text_or_none(args.get("rollback_of")),
        )
        try:
            request_id = door.file(asked)
        except ReleaseSpoolError as exc:
            # This host has no spool, or root's directory moved. §5 row 11:
            # an allowed call that failed, not a policy denial.
            raise ExecutionFailed(str(exc)) from None

        return {"request": request_id, "status": "filed", "note": CORPUS_NOTE}

    async def _enqueue(
        self, grants: FamilyGrants, args: dict[str, object], claimed: Claimed
    ) -> dict[str, object]:
        """Contract 04 §4.1's `enqueue`. One job in another family, no wait.

        The delegation id is minted here, as it is for a delegate call, so
        §6.3's chain continues across the enqueue and the job's own outcome
        record names the path that reached it.
        """
        door = self._deps.dispatch_door
        if door is None:
            raise ExecutionFailed("no dispatch door on this PEP")

        target = str(args.get("family", ""))
        delegations = self._deps.delegations
        chain = delegations.chain_for(claimed.delegation_id, grants.family)
        minted = delegations.mint((*chain, target))
        key = args.get("idempotency_key")

        reply = await door.enqueue(
            DispatchRequest(
                caller_family=grants.family,
                target_family=target,
                delegation_id=minted,
                chain=(*chain, target),
                claimed_session_id=claimed.session_id,
                message=str(args.get("message", "")),
                idempotency_key=str(key) if isinstance(key, str) else None,
            )
        )
        return {
            "session": reply.session,
            "family": target,
            "status": reply.status,
            "created": reply.created,
        }

    async def _job_status(self, grants: FamilyGrants, args: dict[str, object]) -> dict[str, object]:
        """Contract 04 §4.1's `job_status`. The scope is fixed, not chosen.

        The caller family comes from the token this request resolved to, so
        no argument can widen it and an id it never enqueued is simply absent.
        """
        door = self._deps.dispatch_door
        if door is None:
            raise ExecutionFailed("no dispatch door on this PEP")

        limit = args.get("limit")
        reply = await door.jobs(
            JobQuery(
                caller_family=grants.family,
                session=_text_or_none(args.get("session")),
                since=_text_or_none(args.get("since")),
                limit=limit if isinstance(limit, int) else None,
            )
        )
        return {"jobs": reply.jobs}

    async def _delegate(
        self, grants: FamilyGrants, args: dict[str, object], claimed: Claimed
    ) -> dict[str, object]:
        """Contract 04 §7. One hop, 120 seconds, and the answer is data.

        The delegation id is minted here rather than inside the door client,
        so a call that fails still spends its id and the audit chain of a
        retry cannot be confused with the first attempt's.
        """
        door = self._deps.delegate_door
        if door is None:
            raise ExecutionFailed("no delegate door on this PEP")

        target = str(args.get("family", ""))
        delegations = self._deps.delegations
        chain = delegations.chain_for(claimed.delegation_id, grants.family)
        minted = delegations.mint((*chain, target))

        delegations.begin(grants.family)
        try:
            reply = await door.call(
                DelegateRequest(
                    caller_family=grants.family,
                    target_family=target,
                    delegation_id=minted,
                    claimed_session_id=claimed.session_id,
                    message=str(args.get("message", "")),
                )
            )
        finally:
            delegations.end(grants.family)

        if reply.status is DelegateStatus.TIMEOUT:
            raise DelegateTimedOut(reply.error or f"{target} did not answer in time")

        if reply.status is not DelegateStatus.OK:
            raise ExecutionFailed(reply.error or f"{target} did not answer")

        # §7.6: the PEP never reads the answer. It wraps it and hands it on.
        return wrap_untrusted(target, reply.content or "")

    async def _call_mcp(self, decision: FamilyDecision) -> dict[str, object]:
        pool = self._deps.pool()
        if pool is None or not decision.server or not decision.tool:
            raise ExecutionFailed("no upstream pool on this PEP")
        text = await pool.call(decision.server, decision.tool, decision.args or {})
        return {"text": text}

    async def _embed(self, args: dict[str, object]) -> dict[str, object]:
        """Contract 04 §4.1's `embed`. The local service is stateless LAN
        compute, so the sandbox needs no egress hole of its own.

        The caller names no model (§4.1): the service serves one,
        and the reply reports its id so a caller can check it against the id
        its index recorded. A call carrying `model` never arrives here — the
        schema refuses it as an unknown argument.
        """
        if not self._deps.tei_url:
            raise ExecutionFailed("no TEI URL on this PEP: AGENT_LAN_ADDRESS is not set")

        client = self._deps.client()
        if client is None:
            raise ExecutionFailed("no HTTP client on this PEP")

        served = await self._embedding_model(client)
        reply = await client.post(
            f"{self._deps.tei_url}/embed",
            json={"inputs": [str(args.get("input", ""))]},
            timeout=TEI_EMBED_TIMEOUT_S,
        )
        if reply.status_code != HTTP_OK:
            raise ExecutionFailed(f"embed failed: HTTP {reply.status_code}")

        try:
            vectors = reply.json()
        except (ValueError, RecursionError):
            # The reader's own text can quote the reply, so it stays out.
            raise ExecutionFailed("embed failed: the reply is not JSON") from None

        embedding = _first_vector(vectors)
        return {"embedding": embedding, "model": served, "dims": len(embedding)}

    async def _embedding_model(self, client: httpx.AsyncClient) -> str:
        """The one model id the local service serves, read once per process."""
        if self._tei_model is not None:
            return self._tei_model
        try:
            info = await client.get(f"{self._deps.tei_url}/info", timeout=TEI_INFO_TIMEOUT_S)
            self._tei_model = _model_id(info.json())
        except (httpx.HTTPError, ValueError, RecursionError):
            self._tei_model = UNKNOWN_MODEL
        return self._tei_model

    async def _ha_call(self, args: dict[str, object]) -> dict[str, object]:
        """Contract 04 §4.1's `ha_call`. `data` reached here already fenced by
        `ha_data.refuse` (§4.1 rule 3), so it carries no target and no unlisted
        `notify` key, and the entity below can only be the granted one."""
        if not self._deps.ha_url:
            raise ExecutionFailed("no Home Assistant on this site: neither HA_URL nor AGENT_HA_URL")

        token = self._deps.secrets.get("HA_TOKEN", "")
        if not token:
            raise ExecutionFailed("HA_TOKEN is not configured in the PEP secrets")

        client = self._deps.client()
        if client is None:
            raise ExecutionFailed("no HTTP client on this PEP")

        data = args.get("data")
        body: dict[str, object] = (
            dict(cast("dict[str, object]", data)) if isinstance(data, dict) else {}
        )
        entity = args.get("entity_id")
        if entity:
            body["entity_id"] = entity

        reply = await client.post(
            f"{self._deps.ha_url}/api/services/{args['domain']}/{args['service']}",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
            timeout=HA_CALL_TIMEOUT_S,
        )
        if reply.status_code >= 400:
            raise ExecutionFailed(f"HA answered HTTP {reply.status_code}")
        return {"status": reply.status_code, "result": reply.text[:2048]}

    # ---- manifest ----------------------------------------------------------

    def _tool_entries(self, grants: FamilyGrants) -> list[dict[str, object]]:
        """Contract 04 §4's tool list, in `manifest_actions` order.

        A granted tool whose loaded server does not list it is skipped here:
        `manifest_actions` knows which servers this PEP serves, not which
        tools each one lists.
        """
        entries: list[dict[str, object]] = []
        offered = manifest_actions(
            grants,
            self._known_servers(self._deps.serving()),
            delegate_ready=self._deps.delegate_door is not None,
            approvals_ready=self._deps.gatekeeper is not None,
            dispatch_ready=self._deps.dispatch_door is not None,
            release_ready=self._deps.release_door is not None,
        )
        for name in offered:
            entry = self._entry(name)
            if entry is None:
                continue
            entry["approval"] = name in grants.approval
            if name == INVOKE_AGENT:
                _name_the_delegates(entry, grants)
            entries.append(entry)
        return entries

    def _entry(self, name: str) -> dict[str, object] | None:
        if MCP_TOOL_SEPARATOR not in name:
            return self._verb_entry(name)
        pool = self._deps.pool()
        if pool is None:
            return None
        return self._mcp_entry(pool, name)

    def _verb_entry(self, name: str) -> dict[str, object] | None:
        spec = VERB_CATALOG.get(name)
        if spec is None:
            return None
        return {"name": name, "description": spec.description, "schema": manifest_schema(name)}

    def _mcp_entry(self, pool: UpstreamPool, name: str) -> dict[str, object] | None:
        server, _, bare = name.partition(MCP_TOOL_SEPARATOR)
        listed = pool.tools(server).get(bare)
        if listed is None:
            return None
        return {"name": name, "description": listed.description, "schema": listed.input_schema}

    # ---- audit -------------------------------------------------------------

    def _refuse(
        self,
        grants: FamilyGrants,
        tool: str,
        args: dict[str, object],
        decision: FamilyDecision,
        claimed: Claimed,
    ) -> Reply:
        reason: FamilyReason = decision.reason or "internal_error"
        self._write(grants, tool, args, Outcome.DENY, reason, claimed)
        return Reply(
            FAMILY_DENY_STATUS[reason],
            {"ok": False, "reason": reason, "detail": _plain(decision.detail)},
        )

    def _failed_after_allow(
        self,
        grants: FamilyGrants,
        tool: str,
        args: dict[str, object],
        claimed: Claimed,
        started: float,
        exc: Exception,
        reason: FamilyReason,
        gate: str | None = None,
        waited_ms: int = 0,
    ) -> Reply:
        """§5 row 11 and §7.5. The audit records an allow that failed, because
        the effect may already have happened."""
        self._write(
            grants,
            tool,
            args,
            Outcome.ALLOW,
            reason,
            claimed,
            latency_ms=self._since(started),
            gate=gate,
            waited_ms=waited_ms,
        )
        return Reply(
            FAMILY_DENY_STATUS[reason],
            {"ok": False, "reason": reason, "detail": _plain(str(exc))},
        )

    def _write(
        self,
        grants: FamilyGrants,
        tool: str,
        args: dict[str, object],
        outcome: Outcome,
        reason: str,
        claimed: Claimed,
        *,
        latency_ms: int | None = None,
        gate: str | None = None,
        waited_ms: int = 0,
    ) -> None:
        self._deps.audit.write(
            AuditEntry(
                family=grants.family,
                tool=tool,
                args=args,
                outcome=outcome,
                reason=reason,
                grants_rev=grants.rev,
                sandbox=resolve_sandbox(None),
                latency_ms=latency_ms,
                waited_ms=waited_ms,
                gate=gate,
                claimed=claimed,
                # §6.3: built from the PEP's own mints, never from the header.
                chain=self._deps.delegations.chain_for(claimed.delegation_id, grants.family),
            )
        )

    @staticmethod
    def _since(started: float) -> int:
        return int((time.monotonic() - started) * 1000)


def _holds_current_rev(headers: Mapping[str, str], rev: str) -> bool:
    """Contract 04 §4.2. Does this request already hold the revision served?

    RFC 9110 §13.1.2's `If-None-Match`, read for ONE entity tag and compared
    weakly (§8.8.3), because a grants revision is an opaque string and a
    strong comparison would only add a way to miss a match:

        If-None-Match: "9f21c4"     -> the quoted form a client should send
        If-None-Match: W/"9f21c4"   -> the same tag, marked weak
        If-None-Match: 9f21c4       -> a bare value, taken as the tag

    `*` is deliberately NOT honoured. RFC 9110 reads it as "any current
    representation", which would answer 304 to every poll and leave a sandbox
    never learning that its grants moved. A list of tags is not honoured
    either: this route serves one representation and a sandbox holds one.
    """
    raw = headers.get(IF_NONE_MATCH_HEADER)
    if raw is None or len(raw) > MAX_ETAG_CHARS:
        return False

    tag = raw.strip()
    if tag.startswith("W/"):
        tag = tag[2:]
    if len(tag) >= 2 and tag.startswith('"') and tag.endswith('"'):
        tag = tag[1:-1]

    return tag == rev


def _carries(payload: dict[str, object]) -> bool:
    """Whether a JSON reply can carry this result: strict JSON, as text in
    UTF-8. A result is bytes of another process (invariant 12), and `app.py`
    writes the reply after the audit line says how the call ended."""
    try:
        json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, RecursionError):
        return False

    return True


def _plain(detail: str | None) -> str | None:
    """A detail as text that a JSON reply carries. A detail can quote the
    caller or another process, and a lone surrogate there has no UTF-8
    form. It is written as its escape."""
    if detail is None:
        return None

    return detail.encode("utf-8", "backslashreplace").decode("utf-8")


def _first_vector(reply: object) -> list[object]:
    """The one vector of an `/embed` reply (contract 04 §4.1), or
    `ExecutionFailed`.

    The call sends one input, so the reply is a list that holds one list of
    numbers. A value that is not a finite JSON number is refused: the reply
    of this PEP is strict JSON, which has no word for one.

    CONTRACT-QUESTION: §4.1 says "list of numbers" and gives no minimum
    length. A vector of no number is taken, as it was before this check. A
    change costs each caller that reads `dims`.
    """
    if not isinstance(reply, list) or not reply:
        raise ExecutionFailed("embed failed: the reply holds no vector")

    first = cast("list[object]", reply)[0]
    if not isinstance(first, list):
        raise ExecutionFailed("embed failed: the vector is not a list")

    vector = cast("list[object]", first)
    if not all(_is_finite_number(value) for value in vector):
        raise ExecutionFailed("embed failed: the vector holds a value that is not a finite number")

    return vector


def _is_finite_number(value: object) -> bool:
    """A JSON number that strict JSON can write. `True` is an `int` in
    Python and is no number in JSON. An integer is finite at any length."""
    if isinstance(value, bool):
        return False

    if isinstance(value, int):
        return True

    return isinstance(value, float) and math.isfinite(value)


def _model_id(info: object) -> str:
    """The `model_id` of an `/info` reply, or `UNKNOWN_MODEL`.

    CONTRACT-QUESTION: §4.1 says that the reply reports the id "as the
    service reports it" and is silent on a service that reports no string.
    Such a reply reads as a reply that cannot be read: `unknown`, and the
    call goes on. A change to an upstream failure costs every `embed` call
    while `/info` is down.
    """
    if not isinstance(info, dict):
        return UNKNOWN_MODEL

    served = cast("dict[str, object]", info).get("model_id")
    return served if isinstance(served, str) else UNKNOWN_MODEL


def _text_or_none(value: object) -> str | None:
    """One optional string argument. A value of another type reads as absent
    rather than as its `str()`, which the schema has already refused."""
    return value if isinstance(value, str) else None


def _name_the_delegates(entry: dict[str, object], grants: FamilyGrants) -> None:
    """Say WHICH families this one may ask (contract 04 §4 rule 1).

    The entry is synthesized per family, and the catalog schema only says
    that `family` looks like a family name. Without the names a model cannot
    know whom it may ask. `entry["schema"]` is `manifest_schema`'s deep copy,
    so this never reaches the catalog or another family's manifest, and the
    decision still checks `delegates` on every call (§4 rule 2): the list is
    advice to the model, never the fence.
    """
    schema = entry.get("schema")
    if not isinstance(schema, dict):
        return

    properties = cast(dict[str, object], schema).get("properties")
    if not isinstance(properties, dict):
        return

    family = cast(dict[str, object], properties).get("family")
    if isinstance(family, dict):
        cast(dict[str, object], family)["enum"] = sorted(grants.delegates)
