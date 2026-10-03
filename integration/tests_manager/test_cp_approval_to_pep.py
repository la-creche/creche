"""Packet CP: the approval list, from `caregiver`'s writer to the PEP's gate.

`caregiver` expands a family file's `approval` entries and writes them into
`grants/<family>.json`. The PEP reads that list and holds a call open until a
phone answers. Both sides were built from contract 04 by different agents.
These tests put the real writer and the real reader on one temp root.

    family.yaml approval: [ha_call]
        │
        └─► apply_once ──► grants/chat.json ──► decide_family ──► the gate
                                    ▲                                │
              apply_once again, without ha_call                      │
                                    └──── approval_revoked ──────────┘

The phone rail is the only fake: nothing here reaches Node-RED, Home
Assistant or a phone. The revocation scenario is the one this packet cannot
prove in `pep/tests`, where the writer is a fixture rather than `caregiver`:
contract 04 §1.5.3 says a gate on a removed action is cancelled and denied,
and the only signal between the two processes is the grant file itself.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from agent_pep.gatekeeper import Gatekeeper, GateNotice
from fastapi import FastAPI

from .conftest import CHAT_FAMILY, Applied
from .pep_harness import build_pep

HTTP_OK = 200
HTTP_FORBIDDEN = 403

#: The one triple the fixture family's `ha_call` fence allows.
HA_ARGS: dict[str, Any] = {"domain": "notify", "service": "mobile_app_example_phone"}
HA_CALL: dict[str, object] = {"tool": "ha_call", "args": HA_ARGS}

#: Obvious fixtures. Invariant 13 forbids a real secret in a test fixture.
HOOK_TOKEN = "FIXTURE-APPROVAL-HOOK-TOKEN"
CALLBACK_TOKEN = "FIXTURE-APPROVAL-CALLBACK-TOKEN"
HOOK_URL = "http://127.0.0.1:1881/hook/approval"

#: Milliseconds, not the real 15 minutes and 5 seconds. What is under test is
#: what a deadline and a poll do, not how long either one is.
LIMIT_S = 5.0
POLL_S = 0.01


@dataclass
class FakeRail:
    """The phone rail, recorded rather than sent."""

    seen: list[GateNotice] = field(default_factory=list[GateNotice])

    async def notify(self, notice: GateNotice) -> bool:
        self.seen.append(notice)
        return True


def gated_family() -> dict[str, Any]:
    """The fixture family, with its one Home Assistant call behind a tap."""
    return {**CHAT_FAMILY, "approval": ["ha_call"]}


def without_ha_call() -> dict[str, Any]:
    """The same family after the operator takes the verb away."""
    verbs = {name: fence for name, fence in CHAT_FAMILY["verbs"].items() if name != "ha_call"}
    return {**CHAT_FAMILY, "verbs": verbs}


@dataclass
class Rig:
    app: FastAPI
    rail: FakeRail
    applied: Applied


@pytest.fixture
def rig(applied: Applied, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    """The real PEP over the state root a real `apply_once` just wrote, with
    the family file's `approval` list in it."""
    applied.reapply(gated_family())
    rail = FakeRail()
    monkeypatch.setenv("PEP_APPROVAL_URL", HOOK_URL)
    app = build_pep(
        monkeypatch,
        tmp_path,
        applied.state_root,
        secrets={
            "approval_hook_token": HOOK_TOKEN,
            "approval_callback_token": CALLBACK_TOKEN,
        },
        gatekeeper=Gatekeeper(rail, limit_s=LIMIT_S, poll_s=POLL_S),
    )
    return Rig(app=app, rail=rail, applied=applied)


@asynccontextmanager
async def serving(rig: Rig) -> AsyncGenerator[httpx.AsyncClient]:
    """The app in this test's own loop, so a call can be held open."""
    async with rig.app.router.lifespan_context(rig.app):
        transport = httpx.ASGITransport(app=rig.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://pep") as client:
            yield client


def auth(rig: Rig) -> dict[str, str]:
    return {"Authorization": f"Bearer {rig.applied.token}"}


async def pushed(rail: FakeRail) -> GateNotice:
    for _ in range(2000):
        if rail.seen:
            return rail.seen[0]
        await asyncio.sleep(0)
    raise AssertionError("caregiver's approval list never reached the gate")


async def test_the_written_approval_list_opens_a_gate(rig: Rig) -> None:
    """Seam: `caregiver` wrote `approval: ["ha_call"]` and the PEP gated on
    it. A tap then runs that exact call."""
    async with serving(rig) as client:
        call = asyncio.create_task(client.post("/call", json=HA_CALL, headers=auth(rig)))
        notice = await pushed(rig.rail)
        assert notice.family == rig.applied.family
        assert notice.summary.startswith("ha_call ")

        tapped = await client.post(
            f"/approval/{notice.gate}",
            json={"decision": "approve"},
            headers={"Authorization": f"Bearer {CALLBACK_TOKEN}"},
        )
        assert tapped.status_code == HTTP_OK
        assert (await call).status_code == HTTP_OK


async def test_the_manifest_flags_what_caregiver_gated(rig: Rig) -> None:
    async with serving(rig) as client:
        body = (await client.get("/manifest", headers=auth(rig))).json()

    flags = {entry["name"]: entry["approval"] for entry in body["tools"]}
    assert flags["ha_call"] is True
    assert flags["embed"] is False


async def test_a_reapply_cancels_a_held_gate(rig: Rig) -> None:
    """Contract 04 §1.5.3, across two real processes. `caregiver` rewrites the
    grant file without `ha_call` while the PEP is holding the gate. No signal
    passes between them: the PEP re-reads the file and denies."""
    async with serving(rig) as client:
        call = asyncio.create_task(client.post("/call", json=HA_CALL, headers=auth(rig)))
        await pushed(rig.rail)

        rig.applied.reapply(without_ha_call())
        reply = await call

    assert reply.status_code == HTTP_FORBIDDEN
    assert reply.json()["reason"] == "approval_revoked"
