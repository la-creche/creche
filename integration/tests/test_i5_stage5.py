"""Packet I5: stage 5, a trigger starts a job and a phone approves a call.

The operator's stage 5 test: "watch a timer start a
job, approve a gated call on the phone, and see the job finish with an
outcome record and no leftover session."

Every piece on the path was built apart and none of them ever ran together:
`door-trigger` (packet CR), the trigger intake, the autonomous turn limit and
queue, outcome records and `waiting-approval` (packet CS2), and the PEP's
approvals that block in place (packet CP). `stage5.py` holds the wiring.

    agent-trigger fire  /  POST /triggers/<family>/<name>
      ▼
    sessiond ──► auto-<ulid> ──► one turn ──► the real playpen
      ▼                                          │
    the job's model calls a GATED tool           ▼
      ▼                                     fake-pi.mjs
    the REAL PEP ──► the fake approval transport ──► POST /approval/<gate>
      ▼
    the call runs ──► the turn settles ──► outcomes/<family>/<ulid>.json
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from agent_door_trigger.errors import SessiondError
from agent_door_trigger.payload import MAX_PAYLOAD_BYTES
from agent_managerd import paths as managerd_paths
from agent_sessiond.ids import SessionPrefix, new_ulid
from agent_sessiond.queueing import MAX_QUEUED_TURNS
from stack import Stack, until
from stage5 import (
    ACTION_APPROVE,
    CHAT,
    GATED_ARGS,
    GATED_TOOL,
    HA_REVIEW,
    IMPATIENT_LIMIT_S,
    VAULT_ORACLE,
    WEBHOOK_NAME,
    Stage5,
    ToolCall,
    bridge_bundle_missing,
    serving_stage5,
)

HTTP_OK = 200
HTTP_ACCEPTED = 202
HTTP_BAD_REQUEST = 400
HTTP_NOT_FOUND = 404
HTTP_TOO_LARGE = 413
HTTP_TOO_MANY = 429

#: What `chat`'s model asks `vault-oracle`. Under 24 characters, because
#: `fake-pi.mjs` slices a prompt there before echoing it (`AGENTS.md` 19).
DELEGATE_QUESTION = "boiler-11c"

#: A job of the fixture family is one short fake turn, but it crosses five
#: processes on a loaded Mac. Stage 3 uses the same number for the same work.
SETTLE_TIMEOUT_S = 120.0

#: A job turn long enough to make a gated call INSIDE. `sessiond` reads the
#: PEP's audit on a one-second loop, so the turn has to outlive two of those
#: polls plus the push and the tap. 250 deltas 40 ms apart is about 10 s.
HELD_TURN_EVENTS = 250
HELD_TURN_GAP_MS = 40

#: A turn short enough that the queue's head has somewhere to go quickly.
QUICK_TURN_EVENTS = 6
QUICK_TURN_GAP_MS = 8

#: `ha-review` declares `max_running_turns: 1`, so three fires at once are
#: one running turn and two waiting (contract 02 §13 rules 2 and 3).
FIRES_AT_ONCE = 3

#: Fine enough to measure a one-second poll without spinning on the socket.
STATE_POLL_S = 0.02

#: Long enough to clear `door-trigger`'s 32-byte floor, so a refusal proves
#: the comparison and not the length check.
WRONG_BEARER = "not-the-minted-bearer-" + "z" * 32

#: `sessiond` reads the PEP's audit file on its upkeep loop, once a second
#: (`service.FLUSH_INTERVAL_S`). One whole poll plus slack for a loaded Mac
#: is the bound scenario 4 asserts, and the printed number is the sample.
MAX_GATE_LAG_S = 4.0


pytestmark = pytest.mark.skipif(
    bridge_bundle_missing(),
    reason="playpen/dist/pep-bridge.js is missing: run `pnpm install && pnpm build`",
)


@pytest.fixture
async def stage(roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Stage5]:
    """Three families published by the real manager, then a serving stack."""
    tree, socket_dir = roots
    built = Stack(tree, socket_dir)
    built.build_fixture()
    ready = Stage5(built)

    # The stack writes `chat` a hand-made `creds.json` for stage 1. An apply
    # that finds one REFRESHES its LiteLLM key rather than minting, and this
    # fake never minted that one, so the family would come up degraded and
    # with no grant file (`AGENTS.md` 18).
    managerd_paths.creds_path(built.state_root, CHAT).unlink(missing_ok=True)
    for result in ready.apply_all():
        assert result.ok, result.status.faults

    # One sessions root for every family would run every job in `chat`'s
    # directory (`AGENTS.md` 17). The default is already per family.
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
    """The same stack with the real PEP, its gate and the fake phone rail."""
    with serving_stage5(stage, monkeypatch, tmp_path) as ready:
        yield ready


@pytest.fixture
def impatient(stage: Stage5, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Stage5]:
    """The same stack with a gate that runs out while the test watches."""
    with serving_stage5(stage, monkeypatch, tmp_path, IMPATIENT_LIMIT_S) as ready:
        yield ready


async def settled_job(stage: Stage5, session: str) -> dict[str, object]:
    """Wait for one job to leave its outcome record, then return it."""
    await until(
        lambda: stage.outcome_of(HA_REVIEW, session) is not None,
        f"an outcome record for {session}",
        timeout=SETTLE_TIMEOUT_S,
    )
    record = stage.outcome_of(HA_REVIEW, session)
    assert record is not None

    return record


async def held_job(stage: Stage5) -> tuple[str, str]:
    """Fire a timer trigger and wait until its turn is running in a sandbox.

    The turn id comes from the REAL playpen's own turn file (contract 03
    §7.4), not from anything this harness invented: a gated call must carry
    the turn the host actually started, or `sessiond` drops the PEP's audit
    record as a claim about a turn it does not own.
    """
    stage.stack.set_pi_env(events=HELD_TURN_EVENTS, delay_ms=HELD_TURN_GAP_MS)
    fired = await stage.fire(HA_REVIEW)

    await until(
        lambda: stage.live_turn(HA_REVIEW, fired.session) is not None,
        f"the playpen's turn file for {fired.session}",
        timeout=SETTLE_TIMEOUT_S,
    )
    turn = stage.live_turn(HA_REVIEW, fired.session)
    assert turn == fired.turn, "the playpen is running a turn sessiond did not start"

    return fired.session, turn


async def waited_for_state(stage: Stage5, session: str, wanted: str, since: float = 0.0) -> float:
    """Wait for one session state and answer how long it took, in seconds.

    Scenario 4's measurement, when `since` is the push's own arrival time.
    `waiting-approval` reaches `sessiond` only through the PEP's audit file,
    read on a one-second loop, so the number is the lag a PEP-to-`sessiond`
    message would close.

    `stack.until` takes a synchronous check and reading a session state is
    one HTTP call, so this waits on its own rather than driving the state
    read from inside a lambda.
    """
    started = since or time.monotonic()
    deadline = time.monotonic() + SETTLE_TIMEOUT_S

    while time.monotonic() < deadline:
        if await stage.session_state(HA_REVIEW, session) == wanted:
            return time.monotonic() - started

        await asyncio.sleep(STATE_POLL_S)

    raise AssertionError(f"session {session} never read {wanted}")


# ---------------------------------------------------------------- scenario 1


async def test_a_timer_fire_runs_one_job(stage: Stage5) -> None:
    """Contract 02 §13 rules 1 and 7. A timer fire is a whole job."""
    fired = await stage.fire(HA_REVIEW)

    assert fired.session.startswith(SessionPrefix.AUTO.value)
    assert fired.state in {"running", "queued"}

    record = await settled_job(stage, fired.session)

    assert record["family"] == HA_REVIEW
    assert record["status"] == "ok"
    assert record["turns"] == 1


async def test_the_job_leaves_no_session_and_no_scratch(stage: Stage5) -> None:
    """Invariant 15. A job leaves the record and nothing else."""
    fired = await stage.fire(HA_REVIEW)
    await settled_job(stage, fired.session)

    await until(
        lambda: fired.session not in stage.sessions_of(HA_REVIEW),
        "the job session directory to go",
        timeout=SETTLE_TIMEOUT_S,
    )
    assert not stage.scratch_of(HA_REVIEW, fired.session).exists()
    assert not await stage.session_exists(HA_REVIEW, fired.session)


async def test_the_record_names_the_trigger_that_fired(stage: Stage5) -> None:
    """Contract 02 §13.1's `trigger`, `started_at`, `ended_at` and `spend`."""
    fired = await stage.fire(HA_REVIEW)
    record = await settled_job(stage, fired.session)
    trigger = record["trigger"]

    assert isinstance(trigger, dict)
    assert trigger["kind"] == "timer"
    assert record["started_at"] and record["ended_at"]
    assert isinstance(record["spend_usd"], (int, float))
    assert record["sandbox"] == f"{HA_REVIEW}-s1"


# ---------------------------------------------------------------- scenario 7


async def test_a_trigger_for_an_attended_family_is_refused(stage: Stage5) -> None:
    """Contract 02 §13 rule 5. `chat` is a human at a keyboard."""
    code = await stage.fire_from_cli(CHAT)

    assert code != 0
    assert stage.outcomes(CHAT) == []


async def test_a_trigger_for_a_thin_family_is_refused(stage: Stage5) -> None:
    """The same rule from the other side: a thin family is a job already."""
    code = await stage.fire_from_cli(VAULT_ORACLE)

    assert code != 0
    assert stage.outcomes(VAULT_ORACLE) == []


# ---------------------------------------------------------------- scenario 2


async def test_a_webhook_payload_reaches_the_job_byte_for_byte(gated: Stage5) -> None:
    """The packet's own words: the payload is DATA for the job, untouched.

    The prompt `sessiond` journals is what `door-trigger` built, and the
    payload sits inside it exactly as it arrived — no re-serialized JSON,
    no reordered keys, no changed spacing.
    """
    body = b'{"sensor":"boiler",  "temp_c":11.5,"tags":["cold","attic"]}'
    reply = await gated.post_webhook(HA_REVIEW, WEBHOOK_NAME, body)

    assert reply.status_code == HTTP_ACCEPTED
    session = str(reply.json()["session"])

    await until(
        lambda: _turn_prompt(gated, session) is not None,
        f"the turn_started line of {session}",
        timeout=SETTLE_TIMEOUT_S,
    )
    prompt = _turn_prompt(gated, session)
    assert prompt is not None
    assert body.decode() in prompt
    assert "untrusted external data" in prompt


async def test_a_wrong_webhook_bearer_reaches_nothing(gated: Stage5) -> None:
    """A bad signature is refused at the door (packet build item 2)."""
    before = set(gated.sessions_of(HA_REVIEW))
    reply = await gated.post_webhook(HA_REVIEW, WEBHOOK_NAME, b"{}", token=WRONG_BEARER)

    assert reply.status_code == HTTP_NOT_FOUND
    assert set(gated.sessions_of(HA_REVIEW)) == before
    assert gated.outcomes(HA_REVIEW) == []


async def test_an_unknown_hook_reads_like_a_wrong_token(gated: Stage5) -> None:
    """The same 404 and the same body: the listener leaks no trigger names."""
    unknown = await gated.post_webhook(HA_REVIEW, "no-such-hook", b"{}")
    wrong = await gated.post_webhook(HA_REVIEW, WEBHOOK_NAME, b"{}", token=WRONG_BEARER)

    assert unknown.status_code == wrong.status_code == HTTP_NOT_FOUND
    assert unknown.json() == wrong.json()
    assert gated.outcomes(HA_REVIEW) == []


async def test_an_oversized_body_is_refused_at_the_door(gated: Stage5) -> None:
    """`payload.py`'s cap. Nothing this big ever reaches `sessiond`."""
    body = b'{"pad":"' + b"p" * (MAX_PAYLOAD_BYTES + 1) + b'"}'
    before = set(gated.sessions_of(HA_REVIEW))
    reply = await gated.post_webhook(HA_REVIEW, WEBHOOK_NAME, body)

    assert reply.status_code == HTTP_TOO_LARGE
    assert set(gated.sessions_of(HA_REVIEW)) == before


async def test_a_body_that_is_not_json_is_refused_at_the_door(gated: Stage5) -> None:
    """Require valid JSON: the shape is checked before anything is started."""
    before = set(gated.sessions_of(HA_REVIEW))
    reply = await gated.post_webhook(HA_REVIEW, WEBHOOK_NAME, b"not json at all")

    assert reply.status_code == HTTP_BAD_REQUEST
    assert set(gated.sessions_of(HA_REVIEW)) == before


def _turn_prompt(stage: Stage5, session: str) -> str | None:
    """The prompt `sessiond` journalled for one session's first turn."""
    for line in stage.journal_lines(HA_REVIEW, session):
        if line["kind"] == "turn_started":
            return str(line["body"].get("prompt", ""))

    return None


# ---------------------------------------------------------------- scenario 3


async def test_three_fires_give_one_running_and_two_queued(stage: Stage5) -> None:
    """Contract 02 §13 rules 2 and 3. `max_running_turns: 1` is one at a time."""
    stage.stack.set_pi_env(events=HELD_TURN_EVENTS, delay_ms=HELD_TURN_GAP_MS)
    fired = [await stage.fire(HA_REVIEW) for _ in range(FIRES_AT_ONCE)]

    assert fired[0].state == "running"
    assert [one.state for one in fired[1:]] == ["queued"] * (FIRES_AT_ONCE - 1)


async def test_the_queue_runs_in_fire_order(stage: Stage5) -> None:
    """A FIFO, so a backlog of firings keeps the order they arrived in."""
    stage.stack.set_pi_env(events=QUICK_TURN_EVENTS, delay_ms=QUICK_TURN_GAP_MS)
    fired = [await stage.fire(HA_REVIEW) for _ in range(FIRES_AT_ONCE)]

    started: list[str] = []
    for one in fired:
        record = await settled_job(stage, one.session)
        started.append(str(record["started_at"]))

    assert started == sorted(started), started


async def test_an_attended_turn_runs_while_the_queue_is_full(stage: Stage5) -> None:
    """Contract 02 §13 rule 5. An attended family has no limit and no queue.

    A human at a keyboard behind a timer's backlog is a worse answer than
    running the turn, so `slot_for` says RUN whatever
    the autonomous family beside it is doing.
    """
    stage.stack.set_pi_env(events=HELD_TURN_EVENTS, delay_ms=HELD_TURN_GAP_MS)
    for _ in range(FIRES_AT_ONCE):
        await stage.fire(HA_REVIEW)

    accepted = await stage.accepted_turn(CHAT, f"owui-{uuid.uuid4()}")

    assert accepted["state"] == "running", accepted


async def test_a_thin_job_runs_while_the_queue_is_full(gated: Stage5) -> None:
    """The same rule from the thin side: a job blocking at the PEP never waits.

    The call is the real one — `chat`'s model asks `vault-oracle` through
    the real bridge and the real PEP — so the job is started by the same
    `/delegate` path the host uses.
    """
    gated.stack.set_pi_env(events=HELD_TURN_EVENTS, delay_ms=HELD_TURN_GAP_MS)
    for _ in range(FIRES_AT_ONCE):
        await gated.fire(HA_REVIEW)

    gated.stack.set_pi_env(events=QUICK_TURN_EVENTS, delay_ms=QUICK_TURN_GAP_MS)
    chat_session = f"owui-{uuid.uuid4()}"
    answered = await gated.call_tool(
        "invoke_agent",
        {"family": VAULT_ORACLE, "message": DELEGATE_QUESTION},
        session=chat_session,
        turn=new_ulid(),
        family=CHAT,
    )

    assert answered.ok, answered.error
    assert DELEGATE_QUESTION in answered.text


# ---------------------------------------------------------------- scenario 4


async def test_a_gated_call_waits_for_the_phone_and_then_runs(gated: Stage5) -> None:
    """The whole of contract 04 §8, inside a real autonomous job.

    The PEP holds the call, the session reads `waiting-approval`, the fake
    transport taps approve, the call runs and the job finishes with a record
    that counts the approval.
    """
    session, turn = await held_job(gated)
    call = asyncio.ensure_future(
        gated.call_tool(GATED_TOOL, GATED_ARGS, session=session, turn=turn)
    )

    await until(
        lambda: gated.transport.last(HA_REVIEW) is not None,
        "the PEP's push to the phone rail",
        timeout=SETTLE_TIMEOUT_S,
    )
    notice = gated.transport.last(HA_REVIEW)
    assert notice is not None
    assert notice.kind == "approval"
    assert notice.summary.startswith(GATED_TOOL)
    assert "mobile_app_example_phone" in notice.summary

    lag_s = await waited_for_state(gated, session, "waiting-approval", since=notice.at)
    print(f"\nI5 measurement: waiting-approval reached sessiond {lag_s:.2f}s after the push")
    # The bound, not the sample: `sessiond` reads the PEP's audit on its
    # one-second upkeep loop, so one poll plus slack is the whole of it.
    assert lag_s < MAX_GATE_LAG_S

    tap = await gated.transport.approve(HA_REVIEW, notice.gate)
    assert tap.status_code == HTTP_OK

    answered: ToolCall = await call
    assert answered.ok, answered.error

    record = await settled_job(gated, session)
    approvals = record["approvals"]
    assert isinstance(approvals, dict)
    assert approvals["requested"] == 1
    assert approvals["approved"] == 1
    assert approvals["denied"] == 0
    assert record["status"] == "ok"


async def test_the_audit_holds_the_two_records_of_one_gate(gated: Stage5) -> None:
    """Contract 04 §6.4: one record when the gate opens, one when it resolves."""
    session, turn = await held_job(gated)
    call = asyncio.ensure_future(
        gated.call_tool(GATED_TOOL, GATED_ARGS, session=session, turn=turn)
    )

    await until(
        lambda: gated.transport.last(HA_REVIEW) is not None,
        "the PEP's push to the phone rail",
        timeout=SETTLE_TIMEOUT_S,
    )
    notice = gated.transport.last(HA_REVIEW)
    assert notice is not None

    await gated.transport.approve(HA_REVIEW, notice.gate)
    assert (await call).ok

    lines = gated.gate_lines(notice.gate)
    assert len(lines) == 2, lines
    assert lines[0]["decision"] == "pending"
    assert lines[0]["reason"] == "approval_required"
    assert lines[1]["decision"] == "allow"
    assert lines[1]["reason"] == "approved"
    assert lines[0]["tool"] == GATED_TOOL


async def test_the_journal_names_the_gate_on_both_sides(gated: Stage5) -> None:
    """Contract 02 §8.1's two lines, which is what the one view reads."""
    session, turn = await held_job(gated)
    call = asyncio.ensure_future(
        gated.call_tool(GATED_TOOL, GATED_ARGS, session=session, turn=turn)
    )

    await until(
        lambda: gated.transport.last(HA_REVIEW) is not None,
        "the PEP's push to the phone rail",
        timeout=SETTLE_TIMEOUT_S,
    )
    notice = gated.transport.last(HA_REVIEW)
    assert notice is not None

    await waited_for_state(gated, session, "waiting-approval")
    await gated.transport.approve(HA_REVIEW, notice.gate)
    assert (await call).ok

    await until(
        lambda: _journal_kinds(gated, session).count("approval_resolved") == 1,
        "the approval_resolved journal line",
        timeout=SETTLE_TIMEOUT_S,
    )
    lines = gated.journal_lines(HA_REVIEW, session)
    opened = [line for line in lines if line["kind"] == "approval_requested"]
    resolved = [line for line in lines if line["kind"] == "approval_resolved"]

    assert len(opened) == 1
    assert opened[0]["body"]["gate_id"] == notice.gate
    assert opened[0]["body"]["tool"] == GATED_TOOL
    assert len(resolved) == 1
    assert resolved[0]["body"]["decision"] == "approved"


def _journal_kinds(stage: Stage5, session: str) -> list[str]:
    return [str(line["kind"]) for line in stage.journal_lines(HA_REVIEW, session)]


# ---------------------------------------------------------------- scenario 5


async def test_a_denied_call_is_a_tool_error_and_the_job_still_ends(gated: Stage5) -> None:
    """Contract 04 §8.4 rule 4's other value. The model reads an error."""
    session, turn = await held_job(gated)
    call = asyncio.ensure_future(
        gated.call_tool(GATED_TOOL, GATED_ARGS, session=session, turn=turn)
    )

    await until(
        lambda: gated.transport.last(HA_REVIEW) is not None,
        "the PEP's push to the phone rail",
        timeout=SETTLE_TIMEOUT_S,
    )
    notice = gated.transport.last(HA_REVIEW)
    assert notice is not None

    await waited_for_state(gated, session, "waiting-approval")
    await gated.transport.deny(HA_REVIEW, notice.gate)
    answered = await call

    assert not answered.ok
    assert answered.error

    lines = gated.gate_lines(notice.gate)
    assert lines[-1]["decision"] == "deny"
    assert lines[-1]["reason"] == "approval_denied"

    # Contract 02 draft 8 §4.3, the orchestrator's ruling of 2026-09-19: the
    # turn returns to `running`, so the model can report the tool error and
    # finish. The job ends `ok` with one denied gate inside it, which is the
    # whole point of the ruling.
    record = await settled_job(gated, session)
    assert record["status"] == "ok"
    approvals = record["approvals"]
    assert isinstance(approvals, dict)
    assert approvals["requested"] == 1
    assert approvals["denied"] == 1
    assert approvals["approved"] == 0
    assert approvals["timed_out"] == 0


async def test_an_unanswered_gate_times_out_and_the_job_still_ends(impatient: Stage5) -> None:
    """Contract 04 §8.5. Nobody taps, the PEP denies, the job finishes."""
    session, turn = await held_job(impatient)
    answered = await impatient.call_tool(GATED_TOOL, GATED_ARGS, session=session, turn=turn)

    assert not answered.ok

    notice = impatient.transport.last(HA_REVIEW)
    assert notice is not None
    lines = impatient.gate_lines(notice.gate)
    assert lines[-1]["reason"] == "approval_timeout"

    # Contract 02 draft 8 §4.3 again: a timeout is a tool error, not the end
    # of the turn.
    record = await settled_job(impatient, session)
    assert record["status"] == "ok"
    approvals = record["approvals"]
    assert isinstance(approvals, dict)
    assert approvals["requested"] == 1
    assert approvals["timed_out"] == 1
    assert approvals["approved"] == 0
    assert approvals["denied"] == 0


async def test_an_undeliverable_push_denies_at_once(gated: Stage5) -> None:
    """Contract 04 §8.4's last rule. A gate nobody can see is a 15 minute stall."""
    session, turn = await held_job(gated)
    gated.transport.undeliverable = True

    answered = await gated.call_tool(GATED_TOOL, GATED_ARGS, session=session, turn=turn)

    assert not answered.ok
    notice = gated.transport.last(HA_REVIEW)
    assert notice is not None, "the PEP must still have tried to push"
    assert gated.gate_lines(notice.gate)[-1]["reason"] == "approval_undeliverable"


# ---------------------------------------------------------------- scenario 9


async def test_a_replayed_approval_is_refused(gated: Stage5) -> None:
    """Contract 04 §8.5. One tap authorizes one call, once."""
    session, turn = await held_job(gated)
    call = asyncio.ensure_future(
        gated.call_tool(GATED_TOOL, GATED_ARGS, session=session, turn=turn)
    )

    await until(
        lambda: gated.transport.last(HA_REVIEW) is not None,
        "the PEP's push to the phone rail",
        timeout=SETTLE_TIMEOUT_S,
    )
    notice = gated.transport.last(HA_REVIEW)
    assert notice is not None

    first = await gated.transport.approve(HA_REVIEW, notice.gate)
    assert (await call).ok
    assert first.status_code == HTTP_OK

    replay = await gated.transport.replay(f"{ACTION_APPROVE}_{HA_REVIEW}_{notice.gate}")
    assert replay.status_code == HTTP_NOT_FOUND
    assert replay.json()["reason"] == "unknown_gate"


async def test_an_approval_for_another_gate_id_is_refused(gated: Stage5) -> None:
    """A tap that names a gate this PEP never opened resolves nothing."""
    session, turn = await held_job(gated)
    call = asyncio.ensure_future(
        gated.call_tool(GATED_TOOL, GATED_ARGS, session=session, turn=turn)
    )

    await until(
        lambda: gated.transport.last(HA_REVIEW) is not None,
        "the PEP's push to the phone rail",
        timeout=SETTLE_TIMEOUT_S,
    )
    notice = gated.transport.last(HA_REVIEW)
    assert notice is not None

    other = await gated.transport.replay(f"{ACTION_APPROVE}_{HA_REVIEW}_{'f' * 16}")
    assert other.status_code == HTTP_NOT_FOUND

    await gated.transport.approve(HA_REVIEW, notice.gate)
    assert (await call).ok, "the real gate must still be open after the wrong one"


async def test_an_approval_after_the_timeout_is_refused(impatient: Stage5) -> None:
    """§8.5: a decision that arrives late never authorizes a later call."""
    session, turn = await held_job(impatient)
    answered = await impatient.call_tool(GATED_TOOL, GATED_ARGS, session=session, turn=turn)

    assert not answered.ok
    notice = impatient.transport.last(HA_REVIEW)
    assert notice is not None

    late = await impatient.transport.approve(HA_REVIEW, notice.gate)
    assert late.status_code == HTTP_NOT_FOUND


# --------------------------------------------------------------- scenario 3b


async def test_the_hundred_and_first_queued_turn_is_refused(stage: Stage5) -> None:
    """Contract 02 §13 rule 4. `max_queued_turns` is `sessiond`'s own 100.

    The real constant, not a shrunken one: a queued turn costs two host
    calls and no sandbox, so filling the queue for real is cheaper than
    reaching into the service to move the number.
    """
    stage.stack.set_pi_env(events=HELD_TURN_EVENTS, delay_ms=HELD_TURN_GAP_MS)
    for _ in range(MAX_QUEUED_TURNS + 1):
        await stage.fire(HA_REVIEW)

    with pytest.raises(SessiondError) as refused:
        await stage.fire(HA_REVIEW)

    assert refused.value.code == "queue_full"
    assert refused.value.status == HTTP_TOO_MANY


# ---------------------------------------------------------------- scenario 6


async def test_a_restart_ends_every_queued_turn(stage: Stage5) -> None:
    """Contract 02 §13.3. The queue lives in memory, so a restart ends it.

    Every queued turn leaves an outcome record saying `queue_lost`, which
    names the event, where `internal` would have claimed a bug in
    `sessiond` that a plain restart is not (contract 02 §4.3).
    """
    stage.stack.set_pi_env(events=HELD_TURN_EVENTS, delay_ms=HELD_TURN_GAP_MS)
    fired = [await stage.fire(HA_REVIEW) for _ in range(FIRES_AT_ONCE)]
    queued = [one.session for one in fired[1:]]

    await stage.stack.close()
    restarted = stage.restart_service()

    try:
        for session in queued:
            record = stage.outcome_of(HA_REVIEW, session)
            assert record is not None, f"{session} left no record"
            assert record["status"] == "cancelled"
            assert record["error"] == "queue_lost"
            assert not stage.scratch_of(HA_REVIEW, session).exists()
    finally:
        await restarted.close()


async def test_a_restart_sweeps_the_job_it_interrupted(stage: Stage5) -> None:
    """The running half. `_recover_turns` fails it, `_sweep_jobs` ends it."""
    stage.stack.set_pi_env(events=HELD_TURN_EVENTS, delay_ms=HELD_TURN_GAP_MS)
    fired = await stage.fire(HA_REVIEW)

    await until(
        lambda: stage.live_turn(HA_REVIEW, fired.session) is not None,
        f"the playpen's turn file for {fired.session}",
        timeout=SETTLE_TIMEOUT_S,
    )

    await stage.stack.close()
    restarted = stage.restart_service()

    try:
        record = stage.outcome_of(HA_REVIEW, fired.session)
        assert record is not None
        assert record["status"] == "failed"
        assert record["error"] == "channel_lost"
        assert stage.sessions_of(HA_REVIEW) == []
    finally:
        await restarted.close()


# ---------------------------------------------------------------- scenario 8


async def test_a_removed_verb_is_denied_between_two_calls(gated: Stage5, tmp_path: Path) -> None:
    """Invariant 9, inside a live job. The second call is refused at once.

    The family FILE drives it (`AGENTS.md` 21): the verb is removed from a
    copy of the fixture registry and the real `managerd` applies it. Nothing
    restarts, and invariant 8 holds — the session goes on and the job still
    finishes with a record.
    """
    session, turn = await held_job(gated)
    first = asyncio.ensure_future(
        gated.call_tool(GATED_TOOL, GATED_ARGS, session=session, turn=turn)
    )

    await until(
        lambda: gated.transport.last(HA_REVIEW) is not None,
        "the PEP's push to the phone rail",
        timeout=SETTLE_TIMEOUT_S,
    )
    notice = gated.transport.last(HA_REVIEW)
    assert notice is not None
    await gated.transport.approve(HA_REVIEW, notice.gate)
    assert (await first).ok

    # The removal. `verbs` loses `ha_call` and `approval` loses the name it
    # gated, which is the only way a family stops holding a verb at all.
    registry = gated.rewrite_family(
        HA_REVIEW, tmp_path / "registry-without-ha-call", verbs={"embed": {}}, approval=[]
    )
    applied = gated.apply_from(registry, HA_REVIEW)
    assert applied.ok, applied.status.faults

    second = await gated.call_tool(GATED_TOOL, GATED_ARGS, session=session, turn=turn)

    assert not second.ok
    assert GATED_TOOL not in second.registered

    # Invariant 8: a permission change never kills the session under it.
    record = await settled_job(gated, session)
    assert record["status"] == "ok"
