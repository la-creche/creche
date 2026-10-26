"""The switch-sandbox handshake (contract 05 §5).

`caregiver` makes this one call. The incoming facade answers `initialize`
first, the jobs in flight on the outgoing facade are drained or
interrupted, and the outgoing facade is stopped before this service reports
the sandbox free.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from attendance.auth import Principal
from attendance.errors import ApiError, ErrorCode
from attendance.ids import new_ulid
from attendance.requests import DelegateRequest, SwitchMode, SwitchRequest
from attendance.service import SessionService
from attendance.switching import SwitchOutcome
from attendance_harness import FAMILY, SANDBOX, make_config, wait_until, write_status
from fake_facade import FacadeFleet, PromptMode

CAREGIVER = Principal.CAREGIVER
DELEGATE = Principal.DOOR_DELEGATE

NEXT_SANDBOX = "chat-s2"
REASON = "mounts changed: added /srv/agents/work/code-sandbox rw"
NOT_YET_S = 0.05

#: The kind of the family here. A delegate job reaches a thin family only.
KIND = "thin"


class SwitchHarness:
    """One thin family, two sandboxes, and a fake facade in each."""

    def __init__(self, tmp_path: Path, mode: PromptMode = PromptMode.OK) -> None:
        self.config = make_config(tmp_path)
        self.facades = FacadeFleet(prompt_mode=mode)
        self.service = self.revive()
        self.publish()

    def revive(self) -> SessionService:
        service = SessionService(
            self.config,
            acp_factory=self.facades.factory,
            cold_start_wait_s=1.0,
            cancel_wait_s=0.2,
        )
        service.start()
        return service

    def publish(self, *boxes: tuple[str, str], kind: str = KIND) -> None:
        """Write the status document `caregiver` would publish right now.

        One sandbox to begin with. `caregiver` creates the replacement and
        then makes the call, so the second row appears with the switch.
        """
        rows = boxes if boxes else ((SANDBOX, "ready"),)
        write_status(self.config.state_root, kind=kind, sandboxes=rows)

    def request(
        self,
        mode: SwitchMode = SwitchMode.DRAIN,
        outgoing: str | None = SANDBOX,
        to: str = NEXT_SANDBOX,
        deadline_s: int = 300,
        family: str = FAMILY,
    ) -> SwitchRequest:
        return SwitchRequest(
            family=family,
            to=to,
            mode=mode,
            reason=REASON,
            outgoing=outgoing,
            deadline_s=deadline_s,
        )

    def switch(self, **over: Any) -> asyncio.Task[dict[str, Any]]:
        """`caregiver` publishes the replacement, then calls."""
        self.publish((SANDBOX, "ready"), (NEXT_SANDBOX, "ready"))
        return asyncio.create_task(self.service.switch_sandbox(CAREGIVER, self.request(**over)))

    async def job(self) -> dict[str, Any]:
        return await self.service.run_delegate(DELEGATE, delegate_to())

    async def held_job(self) -> asyncio.Task[dict[str, Any]]:
        job = asyncio.create_task(self.job())
        await wait_until(lambda: SANDBOX in self.facades.facades)
        await wait_until(lambda: len(self.facades.facades[SANDBOX].held) == 1)
        return job

    def sandbox_of_last_job(self) -> str:
        """The sandbox whose facade took the newest `session/new`."""
        return next(
            box for box in reversed(self.facades.dials) if self.facades.facades[box].sessions
        )

    async def stop(self) -> None:
        await self.service.close()
        await self.facades.stop()


def delegate_to(family: str = FAMILY) -> DelegateRequest:
    return DelegateRequest(
        caller_family="vault-oracle",
        target_family=family,
        delegation_id=new_ulid(),
        claimed_session_id=None,
        message="ping",
    )


async def test_an_idle_family_switches_with_nothing_to_drain(tmp_path: Path) -> None:
    harness = SwitchHarness(tmp_path)
    await harness.job()
    body = await asyncio.wait_for(harness.switch(), 2.0)

    assert body["outcome"] == SwitchOutcome.DRAINED.value
    assert body["turns_running_at_start"] == 0
    assert body["sessions"] == 0
    await harness.stop()


async def test_a_first_create_has_no_outgoing_sandbox(tmp_path: Path) -> None:
    """§5.1: `from` is null on a first create."""
    harness = SwitchHarness(tmp_path)
    body = await asyncio.wait_for(harness.switch(outgoing=None), 2.0)

    assert body["switched"] is True
    assert body["turns_running_at_start"] == 0
    assert harness.facades.dials == [NEXT_SANDBOX]
    await harness.stop()


async def test_a_repeat_answers_the_same_and_does_nothing(tmp_path: Path) -> None:
    """§5.3 rule 7. `caregiver` may retry after a timeout."""
    harness = SwitchHarness(tmp_path)
    await harness.job()
    first = await asyncio.wait_for(harness.switch(mode=SwitchMode.INTERRUPT), 2.0)
    second = await asyncio.wait_for(harness.switch(mode=SwitchMode.INTERRUPT), 2.0)

    assert second == first
    assert harness.facades.dials == [SANDBOX, NEXT_SANDBOX]
    await harness.stop()


@pytest.mark.parametrize("state", ["planned", "draining", "stopping", "gone", "failed"])
async def test_an_incoming_sandbox_that_cannot_serve_is_refused(tmp_path: Path, state: str) -> None:
    """§5.3 rule 8, read with §4.2's rule that this service runs the handshake."""
    harness = SwitchHarness(tmp_path)
    harness.publish((SANDBOX, "ready"), (NEXT_SANDBOX, state))

    with pytest.raises(ApiError) as refused:
        await harness.service.switch_sandbox(CAREGIVER, harness.request())

    assert refused.value.code is ErrorCode.BAD_REQUEST
    await harness.stop()


async def test_an_incoming_sandbox_the_document_omits_is_refused(tmp_path: Path) -> None:
    harness = SwitchHarness(tmp_path)
    harness.publish((SANDBOX, "ready"))

    with pytest.raises(ApiError) as refused:
        await harness.service.switch_sandbox(CAREGIVER, harness.request())

    assert refused.value.code is ErrorCode.BAD_REQUEST
    await harness.stop()


async def test_a_creating_incoming_sandbox_is_accepted(tmp_path: Path) -> None:
    """§4.2: a `creating` sandbox is dialled, because the handshake promotes it."""
    harness = SwitchHarness(tmp_path)
    harness.publish((SANDBOX, "ready"), (NEXT_SANDBOX, "creating"))
    body = await asyncio.wait_for(harness.service.switch_sandbox(CAREGIVER, harness.request()), 2.0)

    assert body["switched"] is True

    await harness.job()

    assert harness.sandbox_of_last_job() == NEXT_SANDBOX
    await harness.stop()


@pytest.mark.parametrize(
    "over",
    [
        {"to": "other-s9"},
        {"outgoing": "other-s1"},
        {"to": SANDBOX},
        {"to": "not a sandbox"},
    ],
)
async def test_a_malformed_pair_is_refused(tmp_path: Path, over: dict[str, Any]) -> None:
    """§5.3 rule 8: another family's sandbox, and `from` equal to `to`."""
    harness = SwitchHarness(tmp_path)

    with pytest.raises(ApiError) as refused:
        await harness.service.switch_sandbox(CAREGIVER, harness.request(**over))

    assert refused.value.code is ErrorCode.BAD_REQUEST
    await harness.stop()


async def test_a_family_of_no_known_kind_is_not_switched(tmp_path: Path) -> None:
    """§5.3 rule 8 names the refusals of a switch. A link needs the kind of
    its family, so this service would not dial a sandbox of such a document."""
    harness = SwitchHarness(tmp_path)
    harness.publish((SANDBOX, "ready"), (NEXT_SANDBOX, "ready"), kind="robot")

    with pytest.raises(ApiError) as refused:
        await harness.service.switch_sandbox(CAREGIVER, harness.request())

    assert refused.value.code is ErrorCode.BAD_REQUEST
    assert refused.value.message == "the status document states no kind"
    assert harness.facades.dials == []
    await harness.stop()


async def test_an_unknown_family_is_refused(tmp_path: Path) -> None:
    harness = SwitchHarness(tmp_path)

    with pytest.raises(ApiError) as refused:
        await harness.service.switch_sandbox(
            CAREGIVER, harness.request(family="ghost", to="ghost-s1", outgoing=None)
        )

    assert refused.value.code is ErrorCode.FAMILY_UNKNOWN
    await harness.stop()


async def test_no_door_may_switch(tmp_path: Path) -> None:
    """Contract 02 §3.1. The `caregiver` token reaches `/internal/*` alone."""
    harness = SwitchHarness(tmp_path)

    with pytest.raises(ApiError) as refused:
        await harness.service.switch_sandbox(DELEGATE, harness.request())

    assert refused.value.code is ErrorCode.FORBIDDEN
    await harness.stop()


async def test_a_crash_mid_switch_converges_on_the_document(tmp_path: Path) -> None:
    """The status document is the truth about which sandbox serves.

    The service dies mid-switch. Nothing about the switch is on disk, so the
    restart reads what `caregiver` published and sends the next job to the
    new sandbox.
    """
    harness = SwitchHarness(tmp_path, PromptMode.HOLD)
    await harness.held_job()
    switch = harness.switch()
    await asyncio.sleep(NOT_YET_S)
    switch.cancel()
    await harness.service.close()

    # `caregiver` has since retired the old sandbox.
    harness.publish((SANDBOX, "stopping"), (NEXT_SANDBOX, "ready"))
    harness.facades.prompt_mode = PromptMode.OK
    harness.service = harness.revive()
    answer = await harness.job()

    assert answer["status"] == "ok"
    assert harness.sandbox_of_last_job() == NEXT_SANDBOX
    await harness.stop()


async def test_a_switch_starts_no_process_of_its_own(tmp_path: Path) -> None:
    """§5.3 rule 8 asks for the handshake, and nothing more.

    So the switch dials the incoming sandbox once and starts nothing in it.
    """
    harness = SwitchHarness(tmp_path)
    await harness.job()
    await asyncio.wait_for(harness.switch(), 2.0)

    assert harness.facades.dials == [SANDBOX, NEXT_SANDBOX]
    assert harness.facades.facades[NEXT_SANDBOX].methods == ["initialize"]
    await harness.stop()


async def test_a_failed_handshake_keeps_the_family_serving(tmp_path: Path) -> None:
    """Contract 05 §5.3 rule 8: nothing moves until the handshake passes.

    The incoming facade answers `initialize` with another protocol
    version, so the call is refused. No job moves, the outgoing facade
    keeps serving, and `caregiver` still owns both sandboxes.
    """
    harness = SwitchHarness(tmp_path)
    harness.facades.mismatched.add(NEXT_SANDBOX)
    await harness.job()

    with pytest.raises(ApiError) as refused:
        await asyncio.wait_for(harness.switch(), 2.0)

    assert refused.value.code is ErrorCode.SANDBOX_UNAVAILABLE

    # The next job still runs on the sandbox the family was serving on.
    answer = await harness.job()

    assert answer["status"] == "ok"
    assert harness.facades.facades[SANDBOX].sessions == ["sess-1", "sess-2"]
    await harness.stop()


async def test_a_refused_switch_is_retried_not_replayed(tmp_path: Path) -> None:
    """§5.3 rule 7 makes a completed switch idempotent, not a refused one.

    `caregiver` retries the same call on its next pass. Answering that retry
    from the remembered failure would leave the family on two sandboxes for
    ever.
    """
    harness = SwitchHarness(tmp_path)
    harness.facades.mismatched.add(NEXT_SANDBOX)

    with pytest.raises(ApiError):
        await asyncio.wait_for(harness.switch(), 2.0)

    harness.facades.mismatched.clear()
    body = await asyncio.wait_for(harness.switch(), 2.0)

    assert body["switched"] is True
    await harness.stop()


async def test_a_refused_switch_with_no_caller_is_retried(tmp_path: Path) -> None:
    """A refused run is forgotten, with a caller or with none.

    The caller leaves before the run ends, and the run is then refused. No
    caller is there to forget the run. The retry must start a run of its own.
    """
    harness = SwitchHarness(tmp_path)
    harness.facades.mismatched.add(NEXT_SANDBOX)
    call = harness.switch()
    await asyncio.sleep(0)
    call.cancel()

    with pytest.raises(asyncio.CancelledError):
        await call

    await wait_until(lambda: NEXT_SANDBOX in harness.facades.dials)
    await asyncio.sleep(NOT_YET_S)

    harness.facades.mismatched.clear()
    body = await asyncio.wait_for(harness.switch(), 2.0)

    assert body["switched"] is True
    await harness.stop()


async def test_a_new_sandbox_alone_moves_no_job(tmp_path: Path) -> None:
    """Contract 05 §4.2 rule 1a. The §5 call is what moves new jobs.

    §4.3 step 5b publishes the replacement before the call, so between the
    publish and the answer both sandboxes are `creating`. Reading the
    document per job would hand the family to the replacement with no
    switch at all, and that replacement has had no handshake.
    """
    harness = SwitchHarness(tmp_path)
    harness.publish((SANDBOX, "creating"))
    await harness.job()

    harness.publish((SANDBOX, "creating"), (NEXT_SANDBOX, "creating"))
    await harness.job()

    assert harness.facades.dials == [SANDBOX]
    await harness.stop()


async def test_a_shutdown_drops_a_running_switch(tmp_path: Path) -> None:
    """A drain waits for jobs, and `close()` may not wait for a drain.

    Nothing about a switch is on disk. A restart reads the status document
    and converges on it, so the run is dropped. A task left pending outlives
    the service it holds and warns when collected.
    """
    harness = SwitchHarness(tmp_path, PromptMode.HOLD)
    await harness.held_job()
    switch = harness.switch()
    await asyncio.sleep(NOT_YET_S)

    assert switch.done() is False

    await harness.service.close()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(switch, 2.0)

    await harness.facades.stop()


async def test_a_drain_waits_for_a_job_over_acp(tmp_path: Path) -> None:
    """§5.3 rule 2. A prompt in flight on the outgoing facade finishes."""
    harness = SwitchHarness(tmp_path, PromptMode.HOLD)
    job = await harness.held_job()
    switch = harness.switch()
    await asyncio.sleep(NOT_YET_S)

    assert switch.done() is False

    [session] = harness.facades.facades[SANDBOX].held
    await harness.facades.settle(SANDBOX, session)
    body = await asyncio.wait_for(switch, 2.0)

    assert (await job)["status"] == "ok"
    assert body["outcome"] == SwitchOutcome.DRAINED.value
    assert body["turns_running_at_start"] == 1
    assert body["turns_finished"] == 1
    assert harness.facades.facades[SANDBOX].methods[-1] == "_creche/shutdown"
    await harness.stop()


async def test_an_interrupt_ends_a_job_over_acp_at_once(tmp_path: Path) -> None:
    """§5.3 rule 3. The outgoing facade stops, and the job answers failed."""
    harness = SwitchHarness(tmp_path, PromptMode.HOLD)
    job = await harness.held_job()
    body = await asyncio.wait_for(harness.switch(mode=SwitchMode.INTERRUPT), 2.0)

    assert body["outcome"] == SwitchOutcome.INTERRUPTED.value
    assert body["turns_aborted"] == 1
    answer = await asyncio.wait_for(job, 2.0)
    assert answer["status"] == "failed"
    assert str(answer["error"]).startswith("channel_lost")
    await harness.stop()


async def test_a_drain_past_its_deadline_stops_the_job(tmp_path: Path) -> None:
    harness = SwitchHarness(tmp_path, PromptMode.HOLD)
    job = await harness.held_job()
    body = await asyncio.wait_for(harness.switch(deadline_s=0), 2.0)

    assert body["outcome"] == SwitchOutcome.DEADLINE_HIT.value
    assert body["turns_aborted"] == 1
    assert (await asyncio.wait_for(job, 2.0))["status"] == "failed"
    await harness.stop()


async def test_a_new_job_goes_to_the_incoming_facade(tmp_path: Path) -> None:
    """§5.3 rule 1. The incoming link is the one the handshake opened."""
    harness = SwitchHarness(tmp_path)
    await harness.job()
    await asyncio.wait_for(harness.switch(), 2.0)
    answer = await harness.job()

    assert answer["status"] == "ok"
    assert harness.facades.dials == [SANDBOX, NEXT_SANDBOX]
    assert harness.service.acp_link(SANDBOX) is None
    assert harness.facades.facades[NEXT_SANDBOX].sessions == ["sess-1"]
    await harness.stop()
