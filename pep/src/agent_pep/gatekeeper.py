"""Approvals that block in place (contract 04 §8).

The HTTP call waits at the PEP until the phone answers or 15 minutes pass.
There is no 202, no checkpoint, no exit and no rebuild: the agent process
stays alive with one tool call outstanding (`docs/rework/spec.md` §7.7).

```
  /call (gated) --> open the gate --> Node-RED --> HA push --> phone
                         |                                       |
                         | the HTTP request stays open           | tap
                         v                                       v
                    resolve  <---- POST /approval/<gate>  <------'
                         |
                         `--> execute now, or deny
```

Five rules, each with its reason:

1. **No thread is parked.** The handler awaits an `asyncio.Event`, so 50 held
   calls cost 50 objects and no worker (§8.8 rule 1). `max_open_gates` bounds
   them per family on top of that.
2. **An undeliverable push denies at once** (§8.4). A gate nobody can see is a
   15 minute stall, and a stall reads like a hung agent.
3. **A decision is single use.** Resolving closes the gate, so a tap that
   arrives after the limit finds nothing and can never authorize a later
   identical call (§8.5). Where two identical calls share one gate, one tap
   authorizes one of them and the other is denied.
4. **A caller that goes away executes nothing** (§8.7). Its request task is
   cancelled, the wait unwinds, and the gate keeps its remaining time so an
   identical retry attaches instead of pushing again. A tap arriving while
   nobody waits stores no decision: it closes the gate and authorizes
   nothing.
5. **A removed grant cancels the gate** (§1.5.3). The reach check runs on a
   poll while the gate is held, so revocation lands without `managerd`
   reaching into this process.

No policy here, and no clock of this module's own: the deadline is the event
loop's. `family_app` decides that a gate is owed, then holds one.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Final, Protocol

import httpx

log = logging.getLogger("agent_pep.gatekeeper")

#: Contract 04 §8.5: then the PEP denies.
APPROVAL_LIMIT_S: Final = 15 * 60

#: How often a held gate re-checks that the action is still granted (§1.5.3).
#: One `stat` of a file the store already caches, so five seconds is cheap and
#: still well inside a human's reaction time.
REVOKE_POLL_S: Final = 5.0

#: The push is one small POST to a service on this host.
NOTIFY_TIMEOUT_S: Final = 10.0

#: The Node-RED hook distinguishes an approval from a plain notice by this
#: key (`docs/node-red-mcp.md` §1.3).
NOTIFY_KIND: Final = "approval"

HTTP_REDIRECT: Final = 300

_SPENT_DETAIL: Final = "an identical call spent this approval"
_TIMEOUT_DETAIL: Final = "no decision arrived within the approval limit"
_REVOKED_DETAIL: Final = "the grant was removed while the gate was open"
_UNDELIVERABLE_DETAIL: Final = "the approval push could not be delivered"


class Verdict(Enum):
    """What the phone said. Contract 04 §8.4 rule 4's body."""

    APPROVE = "approve"
    DENY = "deny"


class GateOutcome(Enum):
    """How a held call ended. Every value but `APPROVED` is one of contract
    04 §6.1's `approval_*` reasons, so the audit writes it verbatim."""

    APPROVED = "approved"
    DENIED = "approval_denied"
    TIMED_OUT = "approval_timeout"
    REVOKED = "approval_revoked"
    UNDELIVERABLE = "approval_undeliverable"


class Reach(Enum):
    """Whether the family still holds the gated action (contract 04 §1.5)."""

    GRANTED = "granted"
    REMOVED = "removed"


#: Asked on a poll while a gate is held. The app layer owns the grant store,
#: so it answers; this module only acts on the answer.
ReachCheck = Callable[[], Reach]


@dataclass(frozen=True)
class GateNotice:
    """Contract 04 §8.4 rule 1: what the PEP posts to Node-RED."""

    family: str
    gate: str
    summary: str


@dataclass(frozen=True)
class GateTicket:
    """One gated call, as the app layer describes it."""

    family: str
    tool: str
    gate: str
    summary: str


@dataclass(frozen=True)
class GateHeld:
    """How the wait ended, and how long it took (§6.1's `waited_ms`)."""

    outcome: GateOutcome
    waited_ms: int
    detail: str | None = None


class ApprovalNotifier(Protocol):
    """What this module needs from the phone rail, and nothing more."""

    async def notify(self, notice: GateNotice) -> bool: ...


@dataclass
class _Gate:
    """One open gate. `outcome` defaults to a denial so a gate read before it
    was settled can only fail closed."""

    family: str
    gate: str
    deadline: float
    decided: asyncio.Event = field(default_factory=asyncio.Event)
    outcome: GateOutcome = GateOutcome.DENIED
    detail: str | None = None
    waiters: int = 0
    spent: bool = False


class Gatekeeper:
    """Every gate this process holds open. One object per PEP."""

    def __init__(
        self,
        notifier: ApprovalNotifier,
        *,
        limit_s: float = APPROVAL_LIMIT_S,
        poll_s: float = REVOKE_POLL_S,
    ) -> None:
        self._notifier = notifier
        self._limit_s = limit_s
        self._poll_s = poll_s
        self._gates: dict[str, _Gate] = {}

    # ---- what the decision core asks ---------------------------------------

    def open_count(self, family: str) -> int:
        """Contract 04 §5 row 8's counter, per family."""
        self._sweep()
        return sum(1 for gate in self._gates.values() if gate.family == family)

    def open_gates(self) -> frozenset[str]:
        """Which gate ids are open. For the one view and for tests."""
        self._sweep()
        return frozenset(self._gates)

    async def aclose(self) -> None:
        """Release what the rail holds at shutdown. Only the HTTP rail holds
        a client, so a fake notifier needs no method of its own."""
        if isinstance(self._notifier, HttpApprovalNotifier):
            await self._notifier.aclose()

    # ---- the wait ----------------------------------------------------------

    async def hold(self, ticket: GateTicket, reach: ReachCheck) -> GateHeld:
        """Open or join a gate, then block until it resolves."""
        gate = self._gates.get(ticket.gate)
        if gate is None:
            gate = self._open(ticket)
            if not await self._push(ticket):
                self._settle(gate, GateOutcome.UNDELIVERABLE, _UNDELIVERABLE_DETAIL)
                return GateHeld(GateOutcome.UNDELIVERABLE, 0, _UNDELIVERABLE_DETAIL)

        started = time.monotonic()
        gate.waiters += 1
        try:
            outcome, detail = await self._decide(gate, reach)
        finally:
            # A cancelled caller leaves the gate open for its remaining time,
            # so an identical retry attaches instead of pushing again (§8.7).
            gate.waiters -= 1

        return GateHeld(outcome, int((time.monotonic() - started) * 1000), detail)

    def resolve(self, gate_id: str, verdict: Verdict) -> bool:
        """Contract 04 §8.4 rule 4. False means the tap landed on nothing."""
        self._sweep()
        gate = self._gates.get(gate_id)
        if gate is None:
            log.warning("approval for gate %s arrived with no gate open; ignored", gate_id)
            return False

        if gate.waiters == 0:
            # §8.7 rule 3: no decision is stored. Closing the gate makes the
            # next identical call ask again rather than find a stale tap.
            log.warning("approval for gate %s arrived with nobody waiting; ignored", gate_id)
            self._close(gate)
            return False

        decided = GateOutcome.APPROVED if verdict is Verdict.APPROVE else GateOutcome.DENIED
        self._settle(gate, decided)
        return True

    # ---- internals ---------------------------------------------------------

    def _open(self, ticket: GateTicket) -> _Gate:
        gate = _Gate(
            family=ticket.family,
            gate=ticket.gate,
            deadline=time.monotonic() + self._limit_s,
        )
        self._gates[ticket.gate] = gate
        return gate

    async def _push(self, ticket: GateTicket) -> bool:
        """§8.4 rule 1. A failure here is a denial, never an exception."""
        notice = GateNotice(family=ticket.family, gate=ticket.gate, summary=ticket.summary)
        try:
            return await self._notifier.notify(notice)
        except Exception:
            log.exception("approval push for gate %s failed; denying", ticket.gate)
            return False

    async def _decide(self, gate: _Gate, reach: ReachCheck) -> tuple[GateOutcome, str | None]:
        """Wait in slices, so a removed grant is noticed while the gate is
        held rather than only when the call finally returns."""
        while True:
            left = gate.deadline - time.monotonic()
            if left <= 0:
                self._settle(gate, GateOutcome.TIMED_OUT, _TIMEOUT_DETAIL)
                return GateOutcome.TIMED_OUT, _TIMEOUT_DETAIL

            try:
                await asyncio.wait_for(gate.decided.wait(), min(left, self._poll_s))
            except TimeoutError:
                if reach() is Reach.REMOVED:
                    self._settle(gate, GateOutcome.REVOKED, _REVOKED_DETAIL)
                    return GateOutcome.REVOKED, _REVOKED_DETAIL
                continue

            return self._claim(gate)

    def _claim(self, gate: _Gate) -> tuple[GateOutcome, str | None]:
        """One tap authorizes one call. A second call attached to the same
        gate is denied rather than executed on somebody else's approval."""
        if gate.outcome is not GateOutcome.APPROVED:
            return gate.outcome, gate.detail

        if gate.spent:
            return GateOutcome.DENIED, _SPENT_DETAIL

        gate.spent = True
        return GateOutcome.APPROVED, None

    def _settle(self, gate: _Gate, outcome: GateOutcome, detail: str | None = None) -> None:
        gate.outcome = outcome
        gate.detail = detail
        self._close(gate)
        gate.decided.set()

    def _close(self, gate: _Gate) -> None:
        """Out of the table, so the gate is single use. A waiter still holds
        the object and reads the outcome off it."""
        self._gates.pop(gate.gate, None)

    def _sweep(self) -> None:
        """Drop gates nobody waits on and whose time is up (§8.7's remaining
        time). A gate with waiters expires through its own waiter."""
        now = time.monotonic()
        expired = [
            gate for gate in self._gates.values() if gate.waiters == 0 and now >= gate.deadline
        ]
        for gate in expired:
            self._close(gate)


class HttpApprovalNotifier:
    """The push leg of contract 04 §8.4: one POST to the protected Node-RED.

    The client is built on first use, because `app.py` constructs this object
    before the event loop exists and an `httpx.AsyncClient` binds its loop
    when it is first used.
    """

    def __init__(
        self,
        url: str,
        token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = NOTIFY_TIMEOUT_S,
    ) -> None:
        self._url = url
        self._token = token
        self._transport = transport
        self._timeout_s = timeout_s
        self._client: httpx.AsyncClient | None = None

    async def notify(self, notice: GateNotice) -> bool:
        """§8.4 rule 1: family, gate and summary. A bearer rides every call —
        loopback on a shared host is not nobody."""
        body = {
            "kind": NOTIFY_KIND,
            "family": notice.family,
            "gate": notice.gate,
            "summary": notice.summary,
        }
        try:
            reply = await self._open().post(
                self._url,
                json=body,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=self._timeout_s,
            )
        except httpx.HTTPError as exc:
            log.error("approval push to the hook failed: %s", exc)
            return False

        return reply.status_code < HTTP_REDIRECT

    async def aclose(self) -> None:
        if self._client is None:
            return
        await self._client.aclose()
        self._client = None

    def _open(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client

        self._client = httpx.AsyncClient(transport=self._transport)
        return self._client
