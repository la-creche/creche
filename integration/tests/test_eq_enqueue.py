"""Packet EQ: one family starts another family's job, and reads it back.

Stage 5 retires the job spool, and nothing could replace it while `enqueue`
and `job_status` were seams in the PEP. This
suite runs the replacement end to end: the real PEP, the real dispatch door,
the real `sessiond`, the real supervisor and the real bridge.

    chat's turn ──enqueue──► PEP ──► /dispatch ──► auto-<ulid> in scrum-lead
                     │                                    │
    job_status ◄─────'  queued / running / ended ◄────────'

    scrum-lead's job ──enqueue──► the gate ──► the fake phone's tap
                                                    │
                                 issue-worker's job ┘

`stage_eq.py` holds the wiring and names the one thing it stands in for.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from agent_managerd import paths as managerd_paths
from agent_sessiond.ids import SessionPrefix
from stack import Stack, until
from stage5 import ToolCall, bridge_bundle_missing
from stage_eq import (
    CHAT,
    ENQUEUE,
    JOB_STATUS,
    LEAD,
    NO_DISPATCH,
    WORKER,
    StageEq,
    serving_eq,
)

#: A job of a fixture family is one short fake turn, but it crosses five
#: processes on a loaded Mac. Stage 5 uses the same number for the same work.
SETTLE_TIMEOUT_S = 120.0

#: A turn long enough to make a gated call INSIDE it. `sessiond` reads the
#: PEP's audit on a one-second loop, so the turn outlives two of those polls
#: plus the push and the tap. Stage 5's own numbers.
HELD_TURN_EVENTS = 250
HELD_TURN_GAP_MS = 40

#: What a caller asks for. `fake-pi.mjs` slices a prompt at 24 characters
#: before echoing it, so a fixture prompt stays under that (`AGENTS.md` 19).
LEAD_PROMPT = "triage 412"
WORKER_PROMPT = "take 412"

#: A session id no family ever minted, for the rule that says an unknown id
#: and another caller's id read alike (contract 02 §13.4.2 rule 2).
STRANGER_SESSION = "auto-01JBQ7WZ0X4T9V6K2H8M3N5PQR"

#: The attended session every caller scenario talks in. An `owui-` id, which
#: is the prefix the Open WebUI door mints (contract 02 §2).
CALLER_SESSION = "owui-eq-caller"

#: How often a scenario re-reads a session state over the real API.
STATE_POLL_S = 0.05


pytestmark = pytest.mark.skipif(
    bridge_bundle_missing(),
    reason="supervisor/dist/pep-bridge.js is missing: run `pnpm install && pnpm build`",
)


@pytest.fixture
async def stage(
    roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[StageEq]:
    """Four families published by the real manager, then a serving stack."""
    tree, socket_dir = roots
    built = Stack(tree, socket_dir)
    built.build_fixture()
    ready = StageEq(built)

    # The stack writes `chat` a hand-made `creds.json` for stage 1. An apply
    # that finds one REFRESHES its LiteLLM key rather than minting, and this
    # fake never minted that one (`AGENTS.md` 18).
    managerd_paths.creds_path(built.state_root, CHAT).unlink(missing_ok=True)
    for result in ready.apply_all():
        assert result.ok, result.status.faults

    dispatchable = ready.published_dispatch_flags()
    assert dispatchable == (LEAD, WORKER), dispatchable

    monkeypatch.delenv("SESSIOND_SANDBOX_SESSIONS_MOUNT", raising=False)
    for name, value in ready.sandbox_environ().items():
        monkeypatch.setenv(name, value)

    await built.serve()

    try:
        yield ready
    finally:
        await built.close()


@pytest.fixture
def dispatching(
    stage: StageEq, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[StageEq]:
    """The same stack with the real PEP, its dispatch door and its gate."""
    with serving_eq(stage, monkeypatch, tmp_path) as ready:
        yield ready


async def chat_turn(stage: StageEq) -> tuple[str, str]:
    """One live attended turn, and the turn id the supervisor is running.

    A tool call has to carry the turn the host started, or the PEP's audit
    record names a turn `sessiond` does not own (contract 04 §8.6).
    """
    stage.stack.set_pi_env(events=HELD_TURN_EVENTS, delay_ms=HELD_TURN_GAP_MS)
    session = CALLER_SESSION
    await stage.accepted_turn(CHAT, session)

    await until(
        lambda: stage.live_turn(CHAT, session) is not None,
        f"the supervisor's turn file for {session}",
        timeout=SETTLE_TIMEOUT_S,
    )
    turn = stage.live_turn(CHAT, session)
    assert turn is not None

    return session, turn


async def lead_job(stage: StageEq) -> tuple[str, str]:
    """One timer-fired job in the lead, held open long enough to call out."""
    stage.stack.set_pi_env(events=HELD_TURN_EVENTS, delay_ms=HELD_TURN_GAP_MS)
    fired = await stage.fire(LEAD)

    await until(
        lambda: stage.live_turn(LEAD, fired.session) is not None,
        f"the supervisor's turn file for {fired.session}",
        timeout=SETTLE_TIMEOUT_S,
    )
    turn = stage.live_turn(LEAD, fired.session)
    assert turn == fired.turn, "the supervisor is running a turn sessiond did not start"

    return fired.session, turn


async def enqueue_from(
    stage: StageEq,
    caller: str,
    session: str,
    turn: str,
    target: str,
    prompt: str = LEAD_PROMPT,
) -> ToolCall:
    """One `enqueue` call from inside a running turn, through the bridge."""
    return await stage.call_tool(
        ENQUEUE,
        {"family": target, "message": prompt},
        session=session,
        turn=turn,
        family=caller,
    )


async def jobs_of(stage: StageEq, caller: str, session: str, turn: str) -> list[dict[str, Any]]:
    """`job_status` from inside a running turn, as the model reads it."""
    call = await stage.call_tool(JOB_STATUS, {}, session=session, turn=turn, family=caller)
    assert call.ok, call.error

    return _rows(call)


def _rows(call: ToolCall) -> list[dict[str, Any]]:
    """The `jobs` list out of the bridge's rendered untrusted block."""
    found = re.search(r'\{"jobs":.*\}', call.text, re.DOTALL)
    assert found is not None, call.text
    body: dict[str, Any] = json.loads(found.group(0))
    listed: Any = body["jobs"]

    return list(listed)


async def approve_next_gate(stage: StageEq, family: str) -> str:
    """Wait for the push this call raised, then tap approve. Answers the gate."""
    await until(
        lambda: stage.transport.last(family) is not None,
        f"an approval push for {family}",
        timeout=SETTLE_TIMEOUT_S,
    )
    notice = stage.transport.last(family)
    assert notice is not None
    reply = await stage.transport.approve(family, notice.gate)
    assert reply.status_code == 200, reply.text

    return notice.gate


# ------------------------------------------------------------- scenario 1


async def test_a_chat_enqueues_a_lead(dispatching: StageEq) -> None:
    """Contract 04 §4.1. A granted caller starts one job and does not wait."""
    session, turn = await chat_turn(dispatching)

    call = await enqueue_from(dispatching, CHAT, session, turn, LEAD)

    assert call.ok, call.error
    assert ENQUEUE in call.registered
    assert JOB_STATUS in call.registered

    started = dispatching.ledger_entries(CHAT)
    assert len(started) == 1
    assert started[0]["family"] == LEAD
    assert started[0]["session"].startswith(SessionPrefix.AUTO.value)
    assert started[0]["session"] in call.text


async def test_the_job_carries_the_callers_chain(dispatching: StageEq) -> None:
    """Contract 04 §6.3. The chain continues across an enqueue, and the
    outcome record is where a reader finds it after the session is gone."""
    session, turn = await chat_turn(dispatching)
    call = await enqueue_from(dispatching, CHAT, session, turn, LEAD)
    assert call.ok, call.error

    job = dispatching.ledger_entries(CHAT)[0]["session"]
    record = await _settled(dispatching, LEAD, job)
    trigger: Any = record["trigger"]

    assert trigger["kind"] == "dispatch"
    assert trigger["name"] == CHAT
    assert trigger["chain"] == [CHAT, LEAD]


async def test_the_audit_names_the_chain(dispatching: StageEq) -> None:
    """Invariant 15 and contract 04 §6.3, in the PEP's own file.

    The audit record's chain is "caller first, THIS family last", so the
    caller's own record ends at the caller. The hop it started is in the
    ledger entry, and from there in the job's outcome record.
    """
    session, turn = await chat_turn(dispatching)
    await enqueue_from(dispatching, CHAT, session, turn, LEAD)

    written = dispatching.enqueue_audit(CHAT)

    assert written
    assert written[-1]["decision"] == "allow"
    assert written[-1]["chain"] == [CHAT]
    assert dispatching.ledger_entries(CHAT)[0]["chain"] == [CHAT, LEAD]


# ------------------------------------------------------------- scenario 2


async def test_job_status_walks_to_the_outcome(dispatching: StageEq) -> None:
    """Contract 02 §13.4.2. A live job has a status, and a finished one
    carries its record, so `job_status` sees a job that has not ended."""
    session, turn = await chat_turn(dispatching)
    await enqueue_from(dispatching, CHAT, session, turn, LEAD)

    live = await jobs_of(dispatching, CHAT, session, turn)
    assert len(live) == 1
    assert live[0]["status"] in {"dispatched", "queued", "running"}
    assert live[0]["outcome"] is None

    job = str(live[0]["session"])
    await _settled(dispatching, LEAD, job)

    ended = await jobs_of(dispatching, CHAT, session, turn)
    assert ended[0]["status"] == "ended"
    outcome: Any = ended[0]["outcome"]
    assert outcome["session"] == job
    assert outcome["status"] == "ok"


async def test_an_unknown_id_reads_as_nothing(dispatching: StageEq) -> None:
    """§13.4.2 rule 2. An id leaks nothing, not even that it exists."""
    session, turn = await chat_turn(dispatching)
    call = await dispatching.call_tool(
        JOB_STATUS, {"session": STRANGER_SESSION}, session=session, turn=turn, family=CHAT
    )

    assert call.ok, call.error
    assert _rows(call) == []


# ------------------------------------------------------------- scenario 3


async def test_a_lead_enqueues_a_worker_behind_a_tap(dispatching: StageEq) -> None:
    """Contract 04 §8 and §4.1 together. The lead's `approval` holds the
    call at the PEP until the phone answers, and only then does a job start."""
    session, turn = await lead_job(dispatching)
    calling = asyncio.create_task(
        enqueue_from(dispatching, LEAD, session, turn, WORKER, WORKER_PROMPT)
    )

    gate = await approve_next_gate(dispatching, LEAD)
    call = await asyncio.wait_for(calling, timeout=SETTLE_TIMEOUT_S)

    assert call.ok, call.error
    started = dispatching.ledger_entries(LEAD)
    assert len(started) == 1
    assert started[0]["family"] == WORKER
    assert [line["reason"] for line in dispatching.gate_lines(gate)] == [
        "approval_required",
        "approved",
    ]


async def test_a_denied_enqueue_leaves_the_turn_running(dispatching: StageEq) -> None:
    """Contract 02 §4.3 draft 8, as packet S5F ruled it: a refused gate
    returns the turn to `running`, so the lead says what it could not do."""
    session, turn = await lead_job(dispatching)
    calling = asyncio.create_task(
        enqueue_from(dispatching, LEAD, session, turn, WORKER, WORKER_PROMPT)
    )

    await until(
        lambda: dispatching.transport.last(LEAD) is not None,
        f"an approval push for {LEAD}",
        timeout=SETTLE_TIMEOUT_S,
    )
    notice = dispatching.transport.last(LEAD)
    assert notice is not None
    await dispatching.transport.deny(LEAD, notice.gate)

    call = await asyncio.wait_for(calling, timeout=SETTLE_TIMEOUT_S)

    assert not call.ok
    assert dispatching.ledger_entries(LEAD) == []
    assert await _reaches_state(dispatching, LEAD, session, "running")


# ------------------------------------------------------------- scenario 4


async def test_a_family_without_the_trigger_is_refused(dispatching: StageEq) -> None:
    """Contract 02 §13.4.1 rule 3. The caller's grant is not enough: only
    the target's own file says it may be dispatched to."""
    session, turn = await chat_turn(dispatching)

    call = await enqueue_from(dispatching, CHAT, session, turn, NO_DISPATCH)

    assert not call.ok
    assert dispatching.ledger_entries(CHAT) == []
    assert dispatching.sessions_of(NO_DISPATCH) == []


async def test_a_target_outside_the_fence_is_refused(dispatching: StageEq) -> None:
    """Contract 04 §4.1. `chat` may not reach the worker: the lead may."""
    session, turn = await chat_turn(dispatching)

    call = await enqueue_from(dispatching, CHAT, session, turn, WORKER)

    assert not call.ok
    written = dispatching.enqueue_audit(CHAT)
    assert written[-1]["decision"] == "deny"
    assert written[-1]["reason"] == "tool_not_granted"


async def test_a_refusal_leaves_the_callers_turn_alive(dispatching: StageEq) -> None:
    """A denial is an answer, not an outage: the chat carries on."""
    session, turn = await chat_turn(dispatching)
    await enqueue_from(dispatching, CHAT, session, turn, NO_DISPATCH)

    assert await dispatching.session_state(CHAT, session) == "running"


async def _settled(stage: StageEq, family: str, session: str) -> dict[str, Any]:
    """Wait for one job to leave its outcome record, then return it."""
    await until(
        lambda: stage.outcome_of(family, session) is not None,
        f"an outcome record for {session}",
        timeout=SETTLE_TIMEOUT_S,
    )
    record = stage.outcome_of(family, session)
    assert record is not None

    return record


async def _reaches_state(stage: StageEq, family: str, session: str, wanted: str) -> bool:
    """Wait for one session state over the real API.

    `stack.until` takes a synchronous check and a state read is one HTTP
    call, so this polls on its own, the way stage 5's own state waiter does.
    """
    deadline = asyncio.get_running_loop().time() + SETTLE_TIMEOUT_S

    while asyncio.get_running_loop().time() < deadline:
        if await stage.session_state(family, session) == wanted:
            return True

        await asyncio.sleep(STATE_POLL_S)

    return False
