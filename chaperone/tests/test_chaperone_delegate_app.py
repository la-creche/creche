"""`invoke_agent` end to end through the family path (contract 04 §7).

The delegate door is a fake behind `DelegateDoor`, so nothing here needs
`attendance`, a sandbox or the host. Every row of the decision and every audit
consequence is asserted against the real app.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from chaperone.app import PepConfig, build_delegate_door, create_app
from chaperone.delegate import (
    UDS_BASE_URL,
    DelegateDoor,
    DelegateReply,
    DelegateRequest,
    DelegateStatus,
)
from chaperone.family_app import MANIFEST_ACTION
from chaperone.family_grants import token_digest
from chaperone.verbs import manifest_schema
from chaperone_family_helpers import FAMILY_TOKEN, OTHER_FAMILY_TOKEN, make_grants, write_grants
from chaperone_helpers import FakePool
from fastapi import FastAPI
from fastapi.testclient import TestClient

ORACLE = "vault-oracle"
ANSWER = "The boiler was serviced on 2026-03-11."
TURN = "01K5J9QWB9R1V3T5Y7H9J2K4P6"
SESSION = "owui-8f1c2e"

DELEGATE_CALL: dict[str, object] = {
    "tool": "invoke_agent",
    "args": {"family": ORACLE, "message": "where is the boiler note"},
}


@dataclass
class FakeDoor:
    """One `attendance` delegate door, recorded rather than reached."""

    reply: DelegateReply = field(
        default_factory=lambda: DelegateReply(DelegateStatus.OK, content=ANSWER)
    )
    seen: list[DelegateRequest] = field(default_factory=list[DelegateRequest])

    async def call(self, request: DelegateRequest) -> DelegateReply:
        self.seen.append(request)
        return self.reply


class HeldDoor(FakeDoor):
    """A door that parks its call until a test lets it go, so two delegate
    calls can be in flight at the same moment."""

    def __init__(self) -> None:
        super().__init__()
        self.arrived = asyncio.Event()
        self.release = asyncio.Event()

    async def call(self, request: DelegateRequest) -> DelegateReply:
        self.seen.append(request)
        self.arrived.set()
        await self.release.wait()
        return self.reply


def make_app(tmp_path: Path, door: DelegateDoor | None) -> FastAPI:
    return create_app(
        PepConfig(audit_dir=tmp_path / "audit", rework_dir=tmp_path / "rework"),
        pool=FakePool(),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(404))),
        delegate_door=door,
    )


def build(tmp_path: Path, door: DelegateDoor | None) -> TestClient:
    client = TestClient(make_app(tmp_path, door))
    client.__enter__()  # run lifespan
    return client


@asynccontextmanager
async def asgi_client(
    tmp_path: Path, door: DelegateDoor | None
) -> AsyncGenerator[httpx.AsyncClient]:
    """The app driven in this test's own event loop, so several requests can
    be in flight at once."""
    app = make_app(tmp_path, door)
    # Starlette's own lifespan, run directly: no extra test dependency, and
    # the same startup and shutdown the server would run.
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://chaperone") as client:
            yield client


def grants_dir(tmp_path: Path) -> Path:
    return tmp_path / "rework" / "grants"


def family_auth(**extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {FAMILY_TOKEN}", **extra}


def records(tmp_path: Path) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    for path in sorted((tmp_path / "rework" / "audit").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            found.append(json.loads(line))
    return [r for r in found if r["tool"] != MANIFEST_ACTION]


def delegating(**over: object):
    return make_grants(delegates=[ORACLE], **over)


# ---- the happy path --------------------------------------------------------


def test_a_granted_delegate_reaches_the_door(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), delegating())
    door = FakeDoor()
    reply = build(tmp_path, door).post("/call", json=DELEGATE_CALL, headers=family_auth())

    assert reply.status_code == 200
    assert reply.json()["result"] == {
        "untrusted": True,
        "source": f"family:{ORACLE}",
        "content": ANSWER,
    }
    assert [r.target_family for r in door.seen] == [ORACLE]
    assert door.seen[0].caller_family == "chat"


def test_the_pep_mints_the_delegation_id(tmp_path: Path) -> None:
    """Contract 04 §7.4: the sandbox never mints one, so a caller-supplied id
    can never become the id the door is told about."""
    write_grants(grants_dir(tmp_path), delegating())
    door = FakeDoor()
    forged = "01K5J9QWB2M4N6Q8S0V2W4Y6A8"
    build(tmp_path, door).post(
        "/call", json=DELEGATE_CALL, headers=family_auth(**{"X-Delegation-Id": forged})
    )
    assert door.seen[0].delegation_id != forged


def test_the_claimed_session_rides_along_as_advisory(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), delegating())
    door = FakeDoor()
    build(tmp_path, door).post(
        "/call", json=DELEGATE_CALL, headers=family_auth(**{"X-Session-Id": SESSION})
    )
    assert door.seen[0].claimed_session_id == SESSION


def test_a_malformed_session_header_reaches_the_door_as_nothing(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), delegating())
    door = FakeDoor()
    reply = build(tmp_path, door).post(
        "/call", json=DELEGATE_CALL, headers=family_auth(**{"X-Session-Id": "has spaces"})
    )
    # §3.1 rule 4: dropped, never a denial.
    assert reply.status_code == 200
    assert door.seen[0].claimed_session_id is None


# ---- the decision rows -----------------------------------------------------


def test_a_target_outside_delegates_is_refused(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), delegating())
    door = FakeDoor()
    reply = build(tmp_path, door).post(
        "/call",
        json={"tool": "invoke_agent", "args": {"family": "finance", "message": "hi"}},
        headers=family_auth(),
    )
    assert reply.status_code == 403
    assert reply.json()["reason"] == "tool_not_granted"
    assert door.seen == []


def test_a_family_with_no_delegates_is_refused(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), make_grants())
    reply = build(tmp_path, FakeDoor()).post("/call", json=DELEGATE_CALL, headers=family_auth())
    assert reply.status_code == 403
    assert reply.json()["reason"] == "tool_not_granted"


def test_a_pep_with_no_door_answers_not_implemented(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), delegating())
    reply = build(tmp_path, None).post("/call", json=DELEGATE_CALL, headers=family_auth())
    assert reply.status_code == 501
    assert reply.json()["reason"] == "not_implemented"


async def test_too_many_in_flight_is_rate_limited(tmp_path: Path) -> None:
    """`max_inflight_delegations` only bites while a call is running, so two
    calls have to overlap. A held door is what makes them overlap."""
    write_grants(grants_dir(tmp_path), delegating(limits={"max_inflight_delegations": 1}))
    door = HeldDoor()
    async with asgi_client(tmp_path, door) as client:
        first = asyncio.create_task(client.post("/call", json=DELEGATE_CALL, headers=family_auth()))
        await door.arrived.wait()

        second = await client.post("/call", json=DELEGATE_CALL, headers=family_auth())
        assert second.status_code == 429
        assert second.json()["reason"] == "rate_limited"

        door.release.set()
        assert (await first).status_code == 200


def test_a_second_hop_is_refused(tmp_path: Path) -> None:
    """Contract 01 §3.6 rule 4 keeps a chain to one hop by making a thin
    family's `delegates` empty. Here the oracle's grant file wrongly holds
    one, and the PEP refuses the second hop on its own account."""
    write_grants(grants_dir(tmp_path), delegating())
    write_grants(grants_dir(tmp_path), _oracle_that_delegates())
    door = FakeDoor()
    client = build(tmp_path, door)
    client.post("/call", json=DELEGATE_CALL, headers=family_auth())
    minted = door.seen[0].delegation_id

    # The oracle's sandbox echoes the id the PEP minted for it (§3).
    again = client.post(
        "/call",
        json={"tool": "invoke_agent", "args": {"family": "code-sandbox", "message": "go"}},
        headers={"Authorization": f"Bearer {OTHER_FAMILY_TOKEN}", "X-Delegation-Id": minted},
    )
    assert again.status_code == 403
    assert again.json()["reason"] == "tool_not_granted"
    # Only the first hop reached the door.
    assert len(door.seen) == 1


def test_the_second_family_records_the_whole_chain(tmp_path: Path) -> None:
    """§6.3: caller first, this family last, built from the PEP's own mint."""
    write_grants(grants_dir(tmp_path), delegating())
    write_grants(grants_dir(tmp_path), _oracle_that_delegates())
    door = FakeDoor()
    client = build(tmp_path, door)
    client.post("/call", json=DELEGATE_CALL, headers=family_auth())
    minted = door.seen[0].delegation_id

    client.post(
        "/call",
        json={"tool": "embed", "args": {"input": "x"}},
        headers={"Authorization": f"Bearer {OTHER_FAMILY_TOKEN}", "X-Delegation-Id": minted},
    )
    assert records(tmp_path)[-1]["chain"] == ["chat", ORACLE]


def _oracle_that_delegates():
    return make_grants(
        family=ORACLE,
        token_sha256=[token_digest(OTHER_FAMILY_TOKEN)],
        tools={},
        delegates=["code-sandbox"],
    )


# ---- what the door answers -------------------------------------------------


def test_a_door_timeout_is_its_own_reason(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), delegating())
    door = FakeDoor(reply=DelegateReply(DelegateStatus.TIMEOUT, error="too slow"))
    reply = build(tmp_path, door).post("/call", json=DELEGATE_CALL, headers=family_auth())

    assert reply.status_code == 504
    assert reply.json()["reason"] == "delegate_timeout"
    last = records(tmp_path)[-1]
    # §7.5 follows an allow: the turn ran, so this is not a policy denial.
    assert last["decision"] == "allow"
    assert last["reason"] == "delegate_timeout"


def test_a_door_failure_is_an_allow_that_failed(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), delegating())
    door = FakeDoor(reply=DelegateReply(DelegateStatus.FAILED, error="no sandbox"))
    reply = build(tmp_path, door).post("/call", json=DELEGATE_CALL, headers=family_auth())

    assert reply.status_code == 502
    assert reply.json()["reason"] == "upstream_failed"
    assert records(tmp_path)[-1]["decision"] == "allow"


# ---- the audit chain (§6.3) ------------------------------------------------


def test_a_top_level_call_has_a_chain_of_one(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), delegating())
    build(tmp_path, FakeDoor()).post("/call", json=DELEGATE_CALL, headers=family_auth())
    assert records(tmp_path)[-1]["chain"] == ["chat"]


def test_a_forged_delegation_id_buys_no_chain(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), delegating())
    build(tmp_path, FakeDoor()).post(
        "/call",
        json={"tool": "embed", "args": {"input": "x"}},
        headers=family_auth(**{"X-Delegation-Id": "01K5J9QWB2M4N6Q8S0V2W4Y6A8"}),
    )
    last = records(tmp_path)[-1]
    assert last["chain"] == ["chat"]
    assert last["claimed"] == {
        "session_id": None,
        "turn_id": None,
        "delegation_id": "01K5J9QWB2M4N6Q8S0V2W4Y6A8",
    }


# ---- the manifest (§4 rule 1) ----------------------------------------------


def test_the_manifest_offers_invoke_agent(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), delegating())
    body = build(tmp_path, FakeDoor()).get("/manifest", headers=family_auth()).json()
    entry = next(t for t in body["tools"] if t["name"] == "invoke_agent")
    assert entry["approval"] is False
    assert "UNTRUSTED" in entry["description"]
    assert entry["schema"]["required"] == ["family", "message"]


def test_the_manifest_names_the_delegates(tmp_path: Path) -> None:
    """A schema that gives `family` a pattern and nothing else leaves a model
    unable to learn WHICH families it may ask. The entry is synthesized per
    family (§4 rule 1), so it can say."""
    write_grants(grants_dir(tmp_path), delegating())
    body = build(tmp_path, FakeDoor()).get("/manifest", headers=family_auth()).json()
    entry = next(t for t in body["tools"] if t["name"] == "invoke_agent")

    assert entry["schema"]["properties"]["family"]["enum"] == [ORACLE]


def test_naming_the_delegates_does_not_touch_the_catalog(tmp_path: Path) -> None:
    """The catalog schema is shared by every family. One family's names must
    never reach another family's manifest, or the decision's own schema."""
    write_grants(grants_dir(tmp_path), delegating())
    build(tmp_path, FakeDoor()).get("/manifest", headers=family_auth())

    assert "enum" not in manifest_schema("invoke_agent")["properties"]["family"]


def test_the_manifest_hides_invoke_agent_without_a_door(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), delegating())
    body = build(tmp_path, None).get("/manifest", headers=family_auth()).json()
    assert [t["name"] for t in body["tools"] if t["name"] == "invoke_agent"] == []


# ---- configuration ---------------------------------------------------------


def test_half_a_configuration_builds_no_door(tmp_path: Path) -> None:
    base = PepConfig(audit_dir=tmp_path / "a", rework_dir=tmp_path / "r")
    assert build_delegate_door(base) is None
    assert build_delegate_door(_with(base, delegate_token="t" * 32)) is None
    assert build_delegate_door(_with(base, attendance_socket=tmp_path / "s.sock")) is None


def test_a_token_and_a_socket_build_a_door(tmp_path: Path) -> None:
    base = PepConfig(audit_dir=tmp_path / "a", rework_dir=tmp_path / "r")
    built = _with(base, delegate_token="t" * 32, attendance_socket=tmp_path / "s.sock")
    assert build_delegate_door(built) is not None


def test_a_token_and_a_url_build_a_door(tmp_path: Path) -> None:
    base = PepConfig(audit_dir=tmp_path / "a", rework_dir=tmp_path / "r")
    built = _with(base, delegate_token="t" * 32, attendance_url="http://192.0.2.10:8350")
    assert build_delegate_door(built) is not None
    # The default URL names no host that could answer, so it is not a
    # configuration on its own.
    assert build_delegate_door(
        _with(base, delegate_token="t" * 32, attendance_url=UDS_BASE_URL)
    ) is (None)


def _with(cfg: PepConfig, **over: object) -> PepConfig:
    fields = {"audit_dir": cfg.audit_dir, "rework_dir": cfg.rework_dir, **over}
    return PepConfig(**fields)  # pyright: ignore[reportArgumentType]
