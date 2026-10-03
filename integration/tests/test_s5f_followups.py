"""Packet S5F: what the stage 5 follow-ups make possible, end to end.

Two scenarios that could not be written before, each on the real PEP, the
real `caregiver` and the real `door-trigger` of `stage5.py`.

1. **Item A's ruling.** A refused gate returns its turn to `running`
   (contract 02 draft 8 §4.3), so ONE job can meet an approved gate and a
   denied one. Draft 7 ended the turn at the first refusal, so the outcome
   record's derived `approved` counter happened to agree only because a
   turn could never meet two resolutions.
2. **Item C's minting.** A family file that declares a webhook opens a live
   route from the file alone (invariant 16, contract 05 §6.4). Before this,
   `door-trigger` answered 404 until an operator wrote the token by hand.

    family.yaml declares webhook: boiler-alert
      ▼
    caregiver mints <state>/triggers/webhooks/ha-review/boiler-alert.token
      ▼                                       │
    status.json names its PATH                ▼
                                    door-trigger serves POST /triggers/...
"""

from __future__ import annotations

import asyncio
import json
import stat
from collections.abc import AsyncIterator, Iterator
from enum import StrEnum
from pathlib import Path
from typing import Any

import pytest
from agent_door_trigger.tokens import MIN_WEBHOOK_TOKEN_BYTES
from stack import Stack, until
from stage5 import (
    CHAT,
    GATED_ARGS,
    GATED_TOOL,
    HA_REVIEW,
    WEBHOOK_NAME,
    WEBHOOK_TOKEN_MODE,
    Stage5,
    bridge_bundle_missing,
    serving_stage5,
)
from test_i5_stage5 import (
    HTTP_ACCEPTED,
    SETTLE_TIMEOUT_S,
    settled_job,
    waited_for_state,
)

from caregiver import paths as caregiver_paths

#: The same tool with other arguments hashes to another gate (contract 04
#: §8.2), which is what lets one turn meet two of them.
SECOND_GATED_ARGS: dict[str, Any] = {
    "domain": "notify",
    "service": "mobile_app_example_phone",
    "data": {"message": "the boiler is still cold"},
}


class Decision(StrEnum):
    """What the fake phone taps (contract 04 §8.4 rule 4)."""

    APPROVE = "approve"
    DENY = "deny"


#: TWO gates inside one turn, and `attendance` reads the PEP's audit once a
#: second, so this turn has to outlive four of those polls plus two pushes
#: and two taps. 600 deltas 40 ms apart is about 24 seconds.
TWO_GATE_EVENTS = 600
TWO_GATE_GAP_MS = 40

pytestmark = pytest.mark.skipif(
    bridge_bundle_missing(),
    reason="playpen/dist/pep-bridge.js is missing: run `pnpm install && pnpm build`",
)


@pytest.fixture
async def stage(roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Stage5]:
    """The stage 5 stack, published by the real manager."""
    tree, socket_dir = roots
    built = Stack(tree, socket_dir)
    built.build_fixture()
    ready = Stage5(built)

    # The stack writes `chat` a hand-made `creds.json` for stage 1, and an
    # apply that finds one refreshes a LiteLLM key this fake never minted.
    caregiver_paths.creds_path(built.state_root, CHAT).unlink(missing_ok=True)
    for result in ready.apply_all():
        assert result.ok, result.status.faults

    monkeypatch.delenv("SESSIOND_SANDBOX_SESSIONS_MOUNT", raising=False)
    for name, value in ready.sandbox_environ().items():
        monkeypatch.setenv(name, value)

    await built.serve()

    try:
        yield ready
    finally:
        await built.close()


@pytest.fixture
def gated(stage: Stage5, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Stage5]:
    with serving_stage5(stage, monkeypatch, tmp_path) as ready:
        yield ready


async def two_gate_job(stage: Stage5) -> tuple[str, str]:
    """A fired job whose turn is long enough to meet two gates.

    `test_i5_stage5.held_job` runs a ten-second turn, which is one gate's
    worth. The turn id comes from the REAL playpen's turn file, because
    a gated call must name the turn the host actually started.
    """
    stage.stack.set_pi_env(events=TWO_GATE_EVENTS, delay_ms=TWO_GATE_GAP_MS)
    fired = await stage.fire(HA_REVIEW)

    await until(
        lambda: stage.live_turn(HA_REVIEW, fired.session) is not None,
        f"the playpen's turn file for {fired.session}",
        timeout=SETTLE_TIMEOUT_S,
    )
    turn = stage.live_turn(HA_REVIEW, fired.session)
    assert turn == fired.turn, "the playpen is running a turn attendance did not start"

    return fired.session, turn


async def tapped(stage: Stage5, decision: Decision, seen: str = "") -> str:
    """Wait for a push about a NEW gate, answer it, and name it.

    `seen` is the gate of the previous push. Without it the second call of
    a two-gate turn reads the first notice again, taps a gate that already
    resolved, and the PEP logs "arrived with no gate open" while the real
    gate runs out its limit.
    """
    await until(
        lambda: _fresh_gate(stage, seen) is not None,
        "the PEP's push to the phone rail",
        timeout=SETTLE_TIMEOUT_S,
    )
    gate = _fresh_gate(stage, seen)
    assert gate is not None

    if decision is Decision.APPROVE:
        await stage.transport.approve(HA_REVIEW, gate)
        return gate

    await stage.transport.deny(HA_REVIEW, gate)
    return gate


def _fresh_gate(stage: Stage5, seen: str) -> str | None:
    notice = stage.transport.last(HA_REVIEW)

    if notice is None or notice.gate == seen:
        return None

    return notice.gate


# ------------------------------------------------------------------- item A


async def test_one_job_records_an_approved_gate_and_a_denied_one(gated: Stage5) -> None:
    """Contract 02 §13.1.1, which draft 7 could not have represented."""
    session, turn = await two_gate_job(gated)

    first = asyncio.ensure_future(
        gated.call_tool(GATED_TOOL, GATED_ARGS, session=session, turn=turn)
    )
    approved_gate = await tapped(gated, Decision.APPROVE)
    assert (await first).ok

    # The turn survived the first gate, so it can open a second one.
    await waited_for_state(gated, session, "running")
    second = asyncio.ensure_future(
        gated.call_tool(GATED_TOOL, SECOND_GATED_ARGS, session=session, turn=turn)
    )
    denied_gate = await tapped(gated, Decision.DENY, seen=approved_gate)
    refused = await second

    assert not refused.ok
    assert denied_gate != approved_gate

    record = await settled_job(gated, session)
    approvals = record["approvals"]
    assert isinstance(approvals, dict)
    assert approvals == {"requested": 2, "approved": 1, "denied": 1, "timed_out": 0}
    assert record["status"] == "ok"


# ------------------------------------------------------------------- item C


def test_caregiver_mints_the_declared_webhooks_bearer(stage: Stage5) -> None:
    """Contract 05 §6.4. Nothing in this harness writes this file."""
    path = caregiver_paths.webhook_token_path(stage.stack.state_root, HA_REVIEW, WEBHOOK_NAME)

    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == WEBHOOK_TOKEN_MODE
    assert len(path.read_text(encoding="utf-8").strip()) >= MIN_WEBHOOK_TOKEN_BYTES


def test_the_status_document_names_the_path_and_not_the_value(stage: Stage5) -> None:
    """Contract 05 §6.4 rule 5. The document is 0644."""
    path = caregiver_paths.status_path(stage.stack.state_root, HA_REVIEW)
    body: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    rows = body["triggers"]["webhooks"]
    minted = stage.webhook_token(HA_REVIEW, WEBHOOK_NAME)

    assert rows == [
        {
            "name": WEBHOOK_NAME,
            "token_path": str(
                caregiver_paths.webhook_token_path(stage.stack.state_root, HA_REVIEW, WEBHOOK_NAME)
            ),
        }
    ]
    assert minted not in path.read_text(encoding="utf-8")


async def test_a_declared_webhook_answers_with_no_hand_made_file(gated: Stage5) -> None:
    """A declared webhook needs no hand-made token. The family file is the one act."""
    before = set(gated.sessions_of(HA_REVIEW))

    reply = await gated.post_webhook(HA_REVIEW, WEBHOOK_NAME, b'{"boiler": "cold"}')

    assert reply.status_code == HTTP_ACCEPTED
    await until(
        lambda: set(gated.sessions_of(HA_REVIEW)) - before != set(),
        "the job the webhook started",
        timeout=SETTLE_TIMEOUT_S,
    )
