"""A gated call, held open by the real app (contract 04 §8).

The phone rail is a fake behind `ApprovalNotifier`, so nothing here reaches
Node-RED, Home Assistant or a phone. The tap is `POST /approval/<gate>`, which
is the call Node-RED makes (§8.4 rule 4).

The 15 minute limit is milliseconds in these tests. What is under test is what
a deadline does, not how long one is.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from chaperone.app import PepConfig, build_gatekeeper, create_app
from chaperone.family_app import MANIFEST_ACTION
from chaperone.gatekeeper import Gatekeeper, GateNotice
from chaperone.gates import approval_summary, gate_id
from chaperone_family_helpers import FAMILY_TOKEN, make_grants, write_grants
from chaperone_helpers import FakePool
from fastapi import FastAPI
from fastapi.testclient import TestClient

CALLBACK_TOKEN = "node-red-callback-token-not-a-real-one"
EMBED_CALL: dict[str, object] = {"tool": "embed", "args": {"input": "where is the boiler note"}}
OTHER_CALL: dict[str, object] = {"tool": "embed", "args": {"input": "something else"}}
EMBED_GATE = gate_id("chat", "embed", {"input": "where is the boiler note"})

#: The default limit is for a gate a test TAPS, so it is long: the tap
#: has to land while the gate is open, and on a loaded CI runner the gap
#: between `started()` and the tap can run past 150 ms. Both calls then
#: time out, and the tap meets `approval for gate ... arrived with no gate
#: open`. A test that waits for the deadline names its own short
#: `limit_s`, so the default never makes a reader wait.
LIMIT_S = 5.0
POLL_S = 0.01


@dataclass
class FakeRail:
    """The phone rail, recorded rather than sent."""

    delivered: bool = True
    seen: list[GateNotice] = field(default_factory=list[GateNotice])

    async def notify(self, notice: GateNotice) -> bool:
        self.seen.append(notice)
        return self.delivered


def embedding(request: httpx.Request) -> httpx.Response:
    """The local embedding service, and nothing else on this client."""
    if request.url.path.endswith("/info"):
        return httpx.Response(200, json={"model_id": "BAAI/bge-base-en-v1.5"})
    return httpx.Response(200, json=[[0.1, 0.2, 0.3]])


def make_app(tmp_path: Path, rail: FakeRail | None, *, limit_s: float = LIMIT_S) -> FastAPI:
    keeper = None if rail is None else Gatekeeper(rail, limit_s=limit_s, poll_s=POLL_S)
    return create_app(
        PepConfig(
            audit_dir=tmp_path / "audit",
            rework_dir=tmp_path / "rework",
            approval_callback_token=CALLBACK_TOKEN,
            tei_url="http://tei.invalid:8085",
        ),
        pool=FakePool(),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(embedding)),
        gatekeeper=keeper,
    )


def build(tmp_path: Path, rail: FakeRail | None, *, limit_s: float = LIMIT_S) -> TestClient:
    client = TestClient(make_app(tmp_path, rail, limit_s=limit_s))
    client.__enter__()  # run lifespan
    return client


@asynccontextmanager
async def asgi_client(
    tmp_path: Path, rail: FakeRail, *, limit_s: float = LIMIT_S
) -> AsyncGenerator[httpx.AsyncClient]:
    """The app in this test's own event loop, so a call can be held while
    other calls are served."""
    app = make_app(tmp_path, rail, limit_s=limit_s)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://chaperone") as client:
            yield client


def grants_dir(tmp_path: Path) -> Path:
    return tmp_path / "rework" / "grants"


def gated(**over: object):
    """A family whose `embed` needs a tap, unless a test says otherwise."""
    base: dict[str, object] = {"approval": ["embed"]}
    base.update(over)
    return make_grants(**base)


def family_auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {FAMILY_TOKEN}"}


def callback_auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {CALLBACK_TOKEN}"}


def records(tmp_path: Path) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    for path in sorted((tmp_path / "rework" / "audit").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            found.append(json.loads(line))
    return [r for r in found if r["tool"] != MANIFEST_ACTION]


async def tap(client: httpx.AsyncClient, gate: str, decision: str) -> httpx.Response:
    return await client.post(
        f"/approval/{gate}", json={"decision": decision}, headers=callback_auth()
    )


async def started(rail: FakeRail, count: int = 1) -> None:
    """Wait until `count` pushes have gone out."""
    for _ in range(2000):
        if len(rail.seen) >= count:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"only {len(rail.seen)} of {count} gates opened")


# ---- approve, deny, and the limit ------------------------------------------


async def test_an_approved_call_runs_after_the_tap(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), gated())
    rail = FakeRail()
    async with asgi_client(tmp_path, rail) as client:
        call = asyncio.create_task(client.post("/call", json=EMBED_CALL, headers=family_auth()))
        await started(rail)

        assert (await tap(client, EMBED_GATE, "approve")).status_code == 200
        reply = await call

    assert reply.status_code == 200
    assert reply.json()["result"]["dims"] == 3


async def test_a_denied_call_is_a_tool_error(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), gated())
    rail = FakeRail()
    async with asgi_client(tmp_path, rail) as client:
        call = asyncio.create_task(client.post("/call", json=EMBED_CALL, headers=family_auth()))
        await started(rail)
        await tap(client, EMBED_GATE, "deny")
        reply = await call

    assert reply.status_code == 403
    assert reply.json()["reason"] == "approval_denied"


async def test_no_decision_denies_at_the_limit(tmp_path: Path) -> None:
    """§8.5: 15 minutes, then a tool error the agent can report."""
    write_grants(grants_dir(tmp_path), gated())
    async with asgi_client(tmp_path, FakeRail(), limit_s=0.05) as client:
        reply = await client.post("/call", json=EMBED_CALL, headers=family_auth())

    assert reply.status_code == 403
    assert reply.json()["reason"] == "approval_timeout"


async def test_a_tap_after_the_limit_lands_on_nothing(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), gated())
    async with asgi_client(tmp_path, FakeRail(), limit_s=0.05) as client:
        await client.post("/call", json=EMBED_CALL, headers=family_auth())
        assert (await tap(client, EMBED_GATE, "approve")).status_code == 404


# ---- the gate is the call's own hash (§8.2) --------------------------------


async def test_a_changed_argument_needs_its_own_tap(tmp_path: Path) -> None:
    """A tap approves the call it described. A re-issue with a different
    argument hashes elsewhere, so that tap authorizes nothing."""
    write_grants(grants_dir(tmp_path), gated())
    rail = FakeRail()
    async with asgi_client(tmp_path, rail, limit_s=0.4) as client:
        first = asyncio.create_task(client.post("/call", json=EMBED_CALL, headers=family_auth()))
        await started(rail)
        second = asyncio.create_task(client.post("/call", json=OTHER_CALL, headers=family_auth()))
        await started(rail, 2)

        assert rail.seen[0].gate != rail.seen[1].gate
        await tap(client, EMBED_GATE, "approve")

        assert (await first).status_code == 200
        # The second gate was never tapped, so it runs out its own limit.
        assert (await second).json()["reason"] == "approval_timeout"


async def test_key_order_lands_on_the_same_gate(tmp_path: Path) -> None:
    """Canonical args: a client that reorders its JSON re-issues the same
    call, and a human must not be asked twice for it."""
    write_grants(grants_dir(tmp_path), gated(verbs={"ha_call": HA_FENCE}, approval=["ha_call"]))
    rail = FakeRail()
    async with asgi_client(tmp_path, rail) as client:
        one = asyncio.create_task(client.post("/call", json=HA_CALL_A, headers=family_auth()))
        await started(rail)
        two = asyncio.create_task(client.post("/call", json=HA_CALL_B, headers=family_auth()))
        await asyncio.sleep(0)

        assert len(rail.seen) == 1
        await tap(client, rail.seen[0].gate, "deny")
        assert (await one).status_code == 403
        assert (await two).status_code == 403


HA_FENCE: dict[str, object] = {
    "allow": [{"domain": "notify", "service": "mobile_app_example_phone", "entity_id": None}]
}
HA_CALL_A: dict[str, object] = {
    "tool": "ha_call",
    "args": {"domain": "notify", "service": "mobile_app_example_phone"},
}
HA_CALL_B: dict[str, object] = {
    "tool": "ha_call",
    "args": {"service": "mobile_app_example_phone", "domain": "notify"},
}


# ---- a repeat attaches -----------------------------------------------------


async def test_a_repeat_attaches_to_the_open_gate(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), gated())
    rail = FakeRail()
    async with asgi_client(tmp_path, rail) as client:
        first = asyncio.create_task(client.post("/call", json=EMBED_CALL, headers=family_auth()))
        await started(rail)
        second = asyncio.create_task(client.post("/call", json=EMBED_CALL, headers=family_auth()))
        await asyncio.sleep(0)

        # One gate, one push, however many identical calls wait on it.
        assert len(rail.seen) == 1
        await tap(client, EMBED_GATE, "approve")
        codes = sorted([(await first).status_code, (await second).status_code])

    # One tap authorizes one call. The other is denied, never run twice.
    assert codes == [200, 403]


# ---- the push (§8.4) -------------------------------------------------------


async def test_an_undeliverable_push_denies_at_once(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), gated())
    reply = build(tmp_path, FakeRail(delivered=False)).post(
        "/call", json=EMBED_CALL, headers=family_auth()
    )

    assert reply.status_code == 403
    assert reply.json()["reason"] == "approval_undeliverable"


async def test_the_push_names_the_call_it_asks_about(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), gated())
    rail = FakeRail()
    async with asgi_client(tmp_path, rail) as client:
        call = asyncio.create_task(client.post("/call", json=EMBED_CALL, headers=family_auth()))
        await started(rail)
        await tap(client, EMBED_GATE, "deny")
        await call

    notice = rail.seen[0]
    assert notice.family == "chat"
    assert notice.gate == EMBED_GATE
    assert notice.summary == approval_summary("embed", {"input": "where is the boiler note"})


# ---- the audit (§6.4) ------------------------------------------------------


async def test_a_gated_call_writes_two_records(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), gated())
    rail = FakeRail()
    async with asgi_client(tmp_path, rail) as client:
        call = asyncio.create_task(client.post("/call", json=EMBED_CALL, headers=family_auth()))
        await started(rail)
        await asyncio.sleep(0.02)
        await tap(client, EMBED_GATE, "approve")
        await call

    opened, resolved = records(tmp_path)
    assert opened["decision"] == "pending"
    assert opened["gate"] == EMBED_GATE
    assert opened["waited_ms"] == 0

    assert resolved["decision"] == "allow"
    assert resolved["reason"] == "approved"
    assert resolved["gate"] == EMBED_GATE
    # §6.1: the wait is its own field, so a 15 minute gate never reads as a
    # slow upstream.
    assert resolved["waited_ms"] >= 1


async def test_a_denial_is_audited_with_its_wait(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), gated())
    rail = FakeRail()
    async with asgi_client(tmp_path, rail) as client:
        call = asyncio.create_task(client.post("/call", json=EMBED_CALL, headers=family_auth()))
        await started(rail)
        await tap(client, EMBED_GATE, "deny")
        await call

    resolved = records(tmp_path)[-1]
    assert (resolved["decision"], resolved["reason"]) == ("deny", "approval_denied")


# ---- the limits (§5 rows 8 and 9) ------------------------------------------


async def test_too_many_open_gates_is_rate_limited(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), gated(limits={"max_open_gates": 1}))
    rail = FakeRail()
    async with asgi_client(tmp_path, rail) as client:
        first = asyncio.create_task(client.post("/call", json=EMBED_CALL, headers=family_auth()))
        await started(rail)

        second = await client.post("/call", json=OTHER_CALL, headers=family_auth())
        assert second.status_code == 429
        assert second.json()["reason"] == "rate_limited"

        await tap(client, EMBED_GATE, "deny")
        await first


def test_a_pep_with_no_rail_keeps_the_seam(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), gated())
    reply = build(tmp_path, None).post("/call", json=EMBED_CALL, headers=family_auth())

    assert reply.status_code == 501
    assert reply.json()["reason"] == "not_implemented"


def test_an_ungated_call_never_waits(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), make_grants())
    rail = FakeRail()
    reply = build(tmp_path, rail).post("/call", json=EMBED_CALL, headers=family_auth())

    assert reply.status_code == 200
    assert rail.seen == []


# ---- revocation (§1.5.3) ---------------------------------------------------


async def test_a_removed_grant_cancels_a_held_gate(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), gated())
    rail = FakeRail()
    async with asgi_client(tmp_path, rail, limit_s=5.0) as client:
        call = asyncio.create_task(client.post("/call", json=EMBED_CALL, headers=family_auth()))
        await started(rail)

        # caregiver rewrites the grant file without `embed`.
        write_grants(grants_dir(tmp_path), gated(verbs={"ha_call": HA_FENCE}))
        reply = await call

    assert reply.status_code == 403
    assert reply.json()["reason"] == "approval_revoked"
    assert records(tmp_path)[-1]["reason"] == "approval_revoked"


# ---- the caller goes away (§8.7) -------------------------------------------


async def test_an_abandoned_gate_executes_nothing(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), gated())
    rail = FakeRail()
    async with asgi_client(tmp_path, rail, limit_s=5.0) as client:
        call = asyncio.create_task(client.post("/call", json=EMBED_CALL, headers=family_auth()))
        await started(rail)
        call.cancel()
        with suppress(asyncio.CancelledError, httpx.ReadError):
            await call

        # §8.7 rule 3: a tap arriving now stores nothing and runs nothing.
        assert (await tap(client, EMBED_GATE, "approve")).status_code == 404

    resolved = records(tmp_path)[-1]
    assert (resolved["decision"], resolved["reason"]) == ("deny", "approval_abandoned")


# ---- the callback's own bearer (§8.4 rule 4) -------------------------------


async def test_the_callback_needs_its_own_bearer(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), gated())
    client = build(tmp_path, FakeRail())

    refused = client.post(f"/approval/{EMBED_GATE}", json={"decision": "approve"})
    assert refused.status_code == 403
    assert refused.json()["reason"] == "unknown_token"


async def test_a_family_token_is_not_a_callback_bearer(tmp_path: Path) -> None:
    """The callback bearer is not a family token and grants nothing else."""
    write_grants(grants_dir(tmp_path), gated())
    client = build(tmp_path, FakeRail())

    refused = client.post(
        f"/approval/{EMBED_GATE}", json={"decision": "approve"}, headers=family_auth()
    )
    assert refused.status_code == 403


def test_a_pep_with_no_rail_grants_nothing_here(tmp_path: Path) -> None:
    """A deployment with no approval rail: the route exists and is closed to
    everyone. The bearer is checked before the rail, so a caller learns
    nothing about which gates are open from the answer."""
    app = create_app(
        PepConfig(audit_dir=tmp_path / "audit", rework_dir=tmp_path / "rework"),
        pool=FakePool(),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(embedding)),
    )
    with TestClient(app) as client:
        for headers in ({}, callback_auth(), family_auth()):
            refused = client.post(
                f"/approval/{EMBED_GATE}", json={"decision": "approve"}, headers=headers
            )
            assert refused.status_code == 403
            assert refused.json()["reason"] == "unknown_token"


def test_an_unknown_gate_is_not_found(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), gated())
    client = build(tmp_path, FakeRail())

    missing = client.post(
        "/approval/00000000000000ff", json={"decision": "approve"}, headers=callback_auth()
    )
    assert missing.status_code == 404


def test_a_gate_that_is_not_a_gate_id_is_not_found(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), gated())
    client = build(tmp_path, FakeRail())

    assert (
        client.post(
            "/approval/../../etc/passwd", json={"decision": "approve"}, headers=callback_auth()
        ).status_code
        == 404
    )


def test_an_unknown_verdict_is_refused(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), gated())
    client = build(tmp_path, FakeRail())

    refused = client.post(
        f"/approval/{EMBED_GATE}", json={"decision": "maybe"}, headers=callback_auth()
    )
    assert refused.status_code == 422


def test_the_callback_is_closed_without_a_rail(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), gated())
    client = build(tmp_path, None)

    reply = client.post(
        f"/approval/{EMBED_GATE}", json={"decision": "approve"}, headers=callback_auth()
    )
    assert reply.status_code == 501


# ---- the manifest (§4 rule 3) ----------------------------------------------


def test_the_manifest_flags_a_gated_tool(tmp_path: Path) -> None:
    """§4 rule 3: the agent can say a tap is coming before it blocks."""
    write_grants(grants_dir(tmp_path), gated())
    body = build(tmp_path, FakeRail()).get("/manifest", headers=family_auth()).json()

    flags = {entry["name"]: entry["approval"] for entry in body["tools"]}
    assert flags["embed"] is True
    assert flags["ha_call"] is False


def test_the_manifest_hides_a_gated_tool_with_no_rail(tmp_path: Path) -> None:
    """§4 rule 4: the manifest advertises only what `/call` can execute."""
    write_grants(grants_dir(tmp_path), gated())
    body = build(tmp_path, None).get("/manifest", headers=family_auth()).json()

    assert [t["name"] for t in body["tools"] if t["name"] == "embed"] == []


# ---- a held gate parks no worker -------------------------------------------


async def test_a_held_gate_does_not_stall_the_server(tmp_path: Path) -> None:
    """§8.8 rule 1: the handler is asynchronous, so no worker is parked. 50
    gates are held while a plain call is served on the same process."""
    write_grants(grants_dir(tmp_path), gated(limits={"pep_rpm": 500, "max_open_gates": 60}))
    rail = FakeRail()
    async with asgi_client(tmp_path, rail, limit_s=5.0) as client:
        holds = [
            asyncio.create_task(
                client.post(
                    "/call",
                    json={"tool": "embed", "args": {"input": f"question {n}"}},
                    headers=family_auth(),
                )
            )
            for n in range(50)
        ]
        await started(rail, 50)

        # The family path still answers, on the same process, at once.
        plain = await client.get("/manifest", headers=family_auth())
        assert plain.status_code == 200

        for notice in rail.seen:
            await tap(client, notice.gate, "deny")
        assert [(await held).status_code for held in holds] == [403] * 50


# ---- configuration ---------------------------------------------------------


def test_half_a_rail_builds_no_gatekeeper(tmp_path: Path) -> None:
    """Fail closed: a gate that can open and never resolve is a 15 minute
    stall on every gated call."""
    base = PepConfig(audit_dir=tmp_path / "a", rework_dir=tmp_path / "r")
    assert build_gatekeeper(base) is None
    assert build_gatekeeper(_with(base, approval_hook_url="http://127.0.0.1:1881/x")) is None
    assert (
        build_gatekeeper(
            _with(base, approval_hook_url="http://127.0.0.1:1881/x", approval_hook_token="t" * 32)
        )
        is None
    )


def test_a_whole_rail_builds_a_gatekeeper(tmp_path: Path) -> None:
    base = PepConfig(audit_dir=tmp_path / "a", rework_dir=tmp_path / "r")
    whole = _with(
        base,
        approval_hook_url="http://127.0.0.1:1881/hook/approval",
        approval_hook_token="t" * 32,
        approval_callback_token="c" * 32,
    )
    assert build_gatekeeper(whole) is not None


def _with(cfg: PepConfig, **over: object) -> PepConfig:
    fields = {"audit_dir": cfg.audit_dir, "rework_dir": cfg.rework_dir, **over}
    return PepConfig(**fields)  # pyright: ignore[reportArgumentType]
