"""The gate that blocks in place (contract 04 §8), without an HTTP layer.

The notifier is a fake, so nothing here reaches Node-RED, Home Assistant or a
phone. Time limits are milliseconds rather than the real 15 minutes: the
behaviour under test is what a deadline does, not how long one is.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field

import httpx
from agent_pep.gatekeeper import (
    Gatekeeper,
    GateNotice,
    GateOutcome,
    GateTicket,
    HttpApprovalNotifier,
    Reach,
    Verdict,
)

FAMILY = "chat"
GATE = "0123456789abcdef"
OTHER_GATE = "fedcba9876543210"
SUMMARY = 'ha_call domain="notify"'

#: Long enough that no test times out by accident, on a loaded CI runner
#: too (`test_pep_approval_hold.py` says how 150 ms was not). Every test
#: that waits for a deadline names its own short `limit_s`.
LIMIT_S = 5.0
POLL_S = 0.01


@dataclass
class FakeNotifier:
    """One push, recorded rather than sent."""

    delivered: bool = True
    seen: list[GateNotice] = field(default_factory=list[GateNotice])

    async def notify(self, notice: GateNotice) -> bool:
        self.seen.append(notice)
        return self.delivered


def ticket(gate: str = GATE, family: str = FAMILY) -> GateTicket:
    return GateTicket(family=family, tool="ha_call", gate=gate, summary=SUMMARY)


def granted() -> Reach:
    return Reach.GRANTED


def removed() -> Reach:
    return Reach.REMOVED


def keeper(notifier: FakeNotifier, *, limit_s: float = LIMIT_S) -> Gatekeeper:
    return Gatekeeper(notifier, limit_s=limit_s, poll_s=POLL_S)


async def held(gate: Gatekeeper, at: str = GATE) -> asyncio.Task[object]:
    """Start one held call and wait until the push has gone out."""
    task = asyncio.create_task(gate.hold(ticket(at), granted))
    for _ in range(100):
        await asyncio.sleep(0)
        if gate.open_count(FAMILY) and at in gate.open_gates():
            return task
    raise AssertionError("the gate never opened")


# ---- a decision arrives ----------------------------------------------------


async def test_an_approval_lets_the_call_through() -> None:
    notifier = FakeNotifier()
    gate = keeper(notifier)
    task = await held(gate)

    assert gate.resolve(GATE, Verdict.APPROVE) is True
    outcome = await task
    assert outcome.outcome is GateOutcome.APPROVED
    assert [n.summary for n in notifier.seen] == [SUMMARY]


async def test_a_denial_ends_the_call() -> None:
    gate = keeper(FakeNotifier())
    task = await held(gate)

    assert gate.resolve(GATE, Verdict.DENY) is True
    assert (await task).outcome is GateOutcome.DENIED


async def test_the_wait_is_measured() -> None:
    gate = keeper(FakeNotifier())
    task = await held(gate)
    await asyncio.sleep(0.02)
    gate.resolve(GATE, Verdict.APPROVE)
    assert (await task).waited_ms >= 1


async def test_a_resolved_gate_is_closed() -> None:
    """§8.5: a decision is single use. The second tap finds nothing, so it can
    never authorize a later identical call."""
    gate = keeper(FakeNotifier())
    task = await held(gate)
    gate.resolve(GATE, Verdict.APPROVE)
    await task

    assert gate.resolve(GATE, Verdict.APPROVE) is False
    assert gate.open_count(FAMILY) == 0


# ---- the limit (§8.5) ------------------------------------------------------


async def test_the_limit_denies() -> None:
    gate = keeper(FakeNotifier(), limit_s=0.05)
    assert (await gate.hold(ticket(), granted)).outcome is GateOutcome.TIMED_OUT
    assert gate.open_count(FAMILY) == 0


async def test_a_decision_after_the_limit_is_discarded() -> None:
    gate = keeper(FakeNotifier(), limit_s=0.05)
    await gate.hold(ticket(), granted)
    assert gate.resolve(GATE, Verdict.APPROVE) is False


# ---- the push (§8.4) -------------------------------------------------------


async def test_an_undeliverable_push_denies_at_once() -> None:
    """A gate nobody can see is a 15 minute stall, and a stall reads like a
    hung agent."""
    gate = keeper(FakeNotifier(delivered=False))
    outcome = await gate.hold(ticket(), granted)

    assert outcome.outcome is GateOutcome.UNDELIVERABLE
    assert outcome.waited_ms == 0
    assert gate.open_count(FAMILY) == 0


async def test_the_push_carries_family_gate_and_summary() -> None:
    notifier = FakeNotifier()
    gate = keeper(notifier)
    task = await held(gate)
    gate.resolve(GATE, Verdict.DENY)
    await task

    assert notifier.seen[0] == GateNotice(family=FAMILY, gate=GATE, summary=SUMMARY)


# ---- a repeat attaches -----------------------------------------------------


async def test_a_repeat_attaches_to_the_open_gate() -> None:
    """An identical re-issue joins the gate that is already open rather than
    opening a second one and pushing twice."""
    notifier = FakeNotifier()
    gate = keeper(notifier)
    first = await held(gate)
    second = asyncio.create_task(gate.hold(ticket(), granted))
    await asyncio.sleep(0)

    assert len(notifier.seen) == 1
    assert gate.open_count(FAMILY) == 1

    gate.resolve(GATE, Verdict.APPROVE)
    outcomes = {(await first).outcome, (await second).outcome}
    # One tap authorizes one call. The other attached call is denied, never
    # executed a second time.
    assert outcomes == {GateOutcome.APPROVED, GateOutcome.DENIED}


async def test_a_second_gate_is_counted_separately() -> None:
    gate = keeper(FakeNotifier())
    first = await held(gate)
    second = await held(gate, OTHER_GATE)
    assert gate.open_count(FAMILY) == 2

    gate.resolve(GATE, Verdict.DENY)
    gate.resolve(OTHER_GATE, Verdict.DENY)
    await first
    await second


async def test_another_family_holds_its_own_count() -> None:
    gate = keeper(FakeNotifier())
    task = asyncio.create_task(gate.hold(ticket(GATE, "code-sandbox"), granted))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert gate.open_count(FAMILY) == 0
    assert gate.open_count("code-sandbox") == 1
    gate.resolve(GATE, Verdict.DENY)
    await task


# ---- revocation (§1.5.3) ---------------------------------------------------


async def test_a_removed_grant_cancels_the_gate() -> None:
    gate = keeper(FakeNotifier())
    outcome = await gate.hold(ticket(), removed)

    assert outcome.outcome is GateOutcome.REVOKED
    assert gate.open_count(FAMILY) == 0


# ---- the caller goes away (§8.7) -------------------------------------------


async def test_an_abandoned_gate_keeps_its_remaining_time() -> None:
    """The caller's request was cancelled. Nothing executes, and the gate
    stays open so an identical retry attaches instead of pushing again."""
    notifier = FakeNotifier()
    gate = keeper(notifier)
    task = await held(gate)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    assert gate.open_count(FAMILY) == 1
    retry = await held(gate)
    assert len(notifier.seen) == 1

    gate.resolve(GATE, Verdict.APPROVE)
    assert (await retry).outcome is GateOutcome.APPROVED


async def test_a_tap_on_an_abandoned_gate_authorizes_nothing() -> None:
    """§8.7 rule 3: no decision is stored. The gate closes, so the next call
    asks again rather than finding a tap nobody is waiting on."""
    gate = keeper(FakeNotifier())
    task = await held(gate)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    assert gate.resolve(GATE, Verdict.APPROVE) is False
    assert gate.open_count(FAMILY) == 0


async def test_an_abandoned_gate_expires() -> None:
    gate = keeper(FakeNotifier(), limit_s=0.02)
    task = await held(gate)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    await asyncio.sleep(0.05)
    assert gate.open_count(FAMILY) == 0


# ---- the HTTP notifier -----------------------------------------------------


async def test_the_notifier_posts_the_gate_under_its_bearer() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    notifier = HttpApprovalNotifier(
        "http://127.0.0.1:1881/hook/approval",
        "t" * 32,
        transport=httpx.MockTransport(handler),
    )
    assert await notifier.notify(GateNotice(FAMILY, GATE, SUMMARY)) is True
    await notifier.aclose()

    assert seen[0].headers["authorization"] == f"Bearer {'t' * 32}"
    assert b'"gate":"0123456789abcdef"' in seen[0].content


async def test_a_refused_push_is_not_delivered() -> None:
    notifier = HttpApprovalNotifier(
        "http://127.0.0.1:1881/hook/approval",
        "t" * 32,
        transport=httpx.MockTransport(lambda _: httpx.Response(500)),
    )
    assert await notifier.notify(GateNotice(FAMILY, GATE, SUMMARY)) is False
    await notifier.aclose()


async def test_an_unreachable_hook_is_not_delivered() -> None:
    def boom(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    notifier = HttpApprovalNotifier(
        "http://127.0.0.1:1881/hook/approval",
        "t" * 32,
        transport=httpx.MockTransport(boom),
    )
    assert await notifier.notify(GateNotice(FAMILY, GATE, SUMMARY)) is False
    await notifier.aclose()


async def test_the_notifier_reuses_one_client() -> None:
    """Built on first use, then kept: `app.py` constructs the notifier before
    the event loop exists, and a client binds its loop when it is used."""
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(204)

    notifier = HttpApprovalNotifier(
        "http://127.0.0.1:1881/hook/approval",
        "t" * 32,
        transport=httpx.MockTransport(handler),
    )
    await notifier.notify(GateNotice(FAMILY, GATE, SUMMARY))
    await notifier.notify(GateNotice(FAMILY, OTHER_GATE, SUMMARY))
    await notifier.aclose()

    assert len(sent) == 2


async def test_closing_an_unused_notifier_is_safe() -> None:
    notifier = HttpApprovalNotifier("http://127.0.0.1:1881/hook/approval", "t" * 32)
    await notifier.aclose()


async def test_closing_the_keeper_releases_the_rail() -> None:
    """`app.py`'s lifespan calls this. A fake notifier holds nothing, so the
    same call has to be safe either way."""
    http = HttpApprovalNotifier(
        "http://127.0.0.1:1881/hook/approval",
        "t" * 32,
        transport=httpx.MockTransport(lambda _: httpx.Response(204)),
    )
    await http.notify(GateNotice(FAMILY, GATE, SUMMARY))
    await Gatekeeper(http).aclose()
    await Gatekeeper(FakeNotifier()).aclose()


async def test_a_notifier_that_raises_denies() -> None:
    """Fail closed: a rail that breaks in a way this module did not name is
    still a gate nobody can see."""

    class Broken:
        async def notify(self, notice: GateNotice) -> bool:
            raise RuntimeError(f"no rail for {notice.gate}")

    gate = Gatekeeper(Broken(), limit_s=LIMIT_S, poll_s=POLL_S)
    assert (await gate.hold(ticket(), granted)).outcome is GateOutcome.UNDELIVERABLE
