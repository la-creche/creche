"""One job over ACP: the clock of its start, of its prompt and of a delegate.

Contract 02 §6 rule 4, §12 rules 3 and 4, contract 04 §7.5 and decision
CH21-3. The start of each job ends within its deadline. A delegate job
keeps one clock across its `host_full` tries and its prompt. Any other
job's prompt gets its whole deadline, counted from the prompt.
"""

from __future__ import annotations

import asyncio
import time

from attendance.acp_jobs import EndKind, JobEnd, JobPlan, run_job
from attendance.ids import new_ulid
from attendance_harness import FAMILY
from creche_wire import Auto, AutoJobId, AutoOrigin, SessionOrigin, Timer, TimerTrigger, Trigger
from fake_facade import PromptMode
from fake_vmm import FakeVmm

CWD = "/srv/agents/sessions/chat"
CANCEL_WAIT_S = 0.1

#: How long the open of the stream waits in a test of a slow start.
SLOW_OPEN_S = 0.6

#: The margin of each clock check, for a loaded machine.
MARGIN_S = 0.4


def plan(deadline_s: float, waits_for_room: bool) -> JobPlan:
    trigger = Trigger(TimerTrigger(timer=Timer()))
    origin = SessionOrigin(
        AutoOrigin(auto=Auto(job=AutoJobId(f"auto-{new_ulid()}"), trigger=trigger))
    )
    return JobPlan(
        family=FAMILY,
        origin=origin,
        cwd=CWD,
        message="hello",
        deadline_s=deadline_s,
        waits_for_room=waits_for_room,
        cancel_wait_s=CANCEL_WAIT_S,
    )


async def open_later(vmm: FakeVmm, gate: asyncio.Event) -> None:
    """Let the first open through after `SLOW_OPEN_S`."""
    while not vmm.opened:
        await asyncio.sleep(0.01)

    await asyncio.sleep(SLOW_OPEN_S)
    gate.set()


async def test_a_start_that_never_ends_is_a_timeout() -> None:
    """Decision CH50 rule 1 gives a VM start no outer bound, so the job
    gives it one: its deadline. The session drops all the same."""
    vmm = FakeVmm(prompt_mode=PromptMode.HANG)
    vmm.gate_every_open = asyncio.Event()
    started = time.monotonic()

    end = await asyncio.wait_for(run_job(vmm, plan(0.3, waits_for_room=True)), 5)

    assert isinstance(end, JobEnd)
    assert end.kind is EndKind.TIMEOUT
    assert "did not start" in str(end.error)
    assert time.monotonic() - started < 0.3 + MARGIN_S
    assert vmm.rows == {}
    vmm.gate_every_open.set()
    await vmm.stop()


async def test_a_dispatch_prompt_gets_its_whole_deadline() -> None:
    """Contract 02 §6 rule 4: a dispatch job waits in its queue, not in its
    prompt. Its prompt gets the whole deadline after a slow start."""
    vmm = FakeVmm(prompt_mode=PromptMode.HANG)
    vmm.gate_every_open = asyncio.Event()
    opener = asyncio.create_task(open_later(vmm, vmm.gate_every_open))
    started = time.monotonic()

    end = await asyncio.wait_for(run_job(vmm, plan(1.0, waits_for_room=False)), 5)

    spent = time.monotonic() - started
    assert isinstance(end, JobEnd)
    assert end.kind is EndKind.TIMEOUT
    assert "no answer within" in str(end.error)
    assert spent >= SLOW_OPEN_S + 1.0
    await opener
    await vmm.stop()


async def test_a_delegate_prompt_gets_what_is_left_of_its_deadline() -> None:
    """Contract 04 §7.5: the PEP gives up on one clock, so a slow start
    leaves the prompt less time."""
    vmm = FakeVmm(prompt_mode=PromptMode.HANG)
    vmm.gate_every_open = asyncio.Event()
    opener = asyncio.create_task(open_later(vmm, vmm.gate_every_open))
    started = time.monotonic()

    end = await asyncio.wait_for(run_job(vmm, plan(1.0, waits_for_room=True)), 5)

    assert isinstance(end, JobEnd)
    assert end.kind is EndKind.TIMEOUT
    assert time.monotonic() - started < 1.0 + CANCEL_WAIT_S + MARGIN_S
    await opener
    await vmm.stop()
