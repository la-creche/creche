"""The upkeep task: the queue pump (contract 02 §13 rule 3).

One try that fails must not end the task. A task that ended would stop the
pump until the process restarts, and nothing would say so.

The last tests here are about each other task the service starts. A task
that ends with an error says so in the log.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest
from attendance.service import SessionService
from attendance.tasks import report_failure
from attendance_harness import make_config, wait_until, write_status

#: The upkeep interval of these tests. The real one is a second.
TICK_S = 0.01

#: How many ticks prove that the task is still alive.
TICKS_WANTED = 3


async def test_upkeep_tries_the_queues_after_a_failed_try(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Contract 02 §13 rule 3. A try of the queues that fails does not end
    the task, and the next tick tries again."""
    monkeypatch.setattr("attendance.service.PUMP_INTERVAL_S", TICK_S)
    config = make_config(tmp_path)
    write_status(config.state_root)
    service = SessionService(config, cold_start_wait_s=1.0)
    service.start()
    calls = 0

    def failing() -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("the try failed")

    monkeypatch.setattr(service, "pump_queues", failing)

    with caplog.at_level(logging.ERROR, logger="attendance"):
        service.start_upkeep()
        await wait_until(lambda: calls >= TICKS_WANTED)

    assert "the queue pump" in caplog.text
    await service.close()


async def _fails() -> None:
    raise RuntimeError("the work failed")


async def test_a_task_that_fails_says_so(caplog: pytest.LogCaptureFixture) -> None:
    """The event loop keeps the error of a task that nothing awaits."""
    task = asyncio.create_task(_fails(), name="the work")
    task.add_done_callback(report_failure)

    with caplog.at_level(logging.ERROR, logger="attendance"):
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

    assert "task the work ended with an error" in caplog.text
    assert "the work failed" in caplog.text


async def test_a_cancelled_task_is_not_a_failure(caplog: pytest.LogCaptureFixture) -> None:
    """`close()` cancels each task that the service owns."""
    task = asyncio.create_task(asyncio.sleep(60), name="the work")
    task.add_done_callback(report_failure)
    task.cancel()

    with caplog.at_level(logging.ERROR, logger="attendance"):
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

    assert caplog.text == ""
