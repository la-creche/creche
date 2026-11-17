"""A stop or a kill of the service never kills the graph database.

The stop cancels the tasks of the store loop, waits at most 10 s for them,
then exits at once. It writes nothing to the graph database. Ladybug
0.22.3 opens a file whose worker died in a checkpoint, with every committed
row. With ladybug 0.19.0, 11 of 100 such kills on 2026-11-17 left a file
that did not open (`memory/AGENTS.md`, the stop).

`MEMORY_STOP_TRIALS` sets how many stops and kills in a checkpoint each of
the first two tests runs. The gate runs the default. The pull request of
the stop ran them with a larger number."""

import asyncio
import os
import random
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from creche_memory.cli import EX_OK
from creche_memory.search import STOP_TIMEOUT_S
from creche_memory.sync import in_daemon_thread
from memory_graph_helpers import (
    GRAPH_NAME,
    STUBBORN_ROWS,
    WRITE_ROWS,
    Stop,
    count_rows,
    stop_in_a_checkpoint,
)

TRIALS = int(os.environ.get("MEMORY_STOP_TRIALS", "3"))

UNIT = Path(__file__).resolve().parents[2] / "systemd" / "creche-memory.service"


def _delay(trial: int) -> tuple[int, float]:
    # The 2026-11-09 kills that left a dead graph came 4 to 13 ms into a
    # checkpoint. Each trial stops the writer 0 to 30 ms into one.
    rng = random.Random(trial)

    return rng.randint(1, 3), rng.uniform(0.0, 0.03)


@pytest.mark.parametrize("trial", range(TRIALS))
def test_a_sigterm_in_a_checkpoint_exits_0_and_keeps_every_row(tmp_path: Path, trial: int) -> None:
    store = tmp_path / "store"
    run = stop_in_a_checkpoint(store, *_delay(trial))

    assert run.returncode == EX_OK, run.stderr[-3000:]
    assert run.lines == [], run.stderr[-3000:]
    # The exit ended Cognee's workers: nothing of the group is left to kill.
    assert not run.group_left
    rows = count_rows(store / "system" / "databases" / GRAPH_NAME, "F")
    # Every row that a checkpoint made durable is there. A statement is
    # whole or absent.
    assert rows >= run.committed
    assert rows % WRITE_ROWS == 0


@pytest.mark.parametrize("trial", range(TRIALS))
def test_a_kill_of_every_process_in_a_checkpoint_keeps_every_row(
    tmp_path: Path, trial: int
) -> None:
    store = tmp_path / "store"
    run = stop_in_a_checkpoint(store, *_delay(trial), stop=Stop.KILL_ALL)

    assert run.returncode == -signal.SIGKILL, run.stderr[-3000:]
    rows = count_rows(store / "system" / "databases" / GRAPH_NAME, "F")
    assert rows >= run.committed
    assert rows % WRITE_ROWS == 0


def test_a_sync_that_ignores_its_cancel_still_exits_0(tmp_path: Path) -> None:
    store = tmp_path / "store"
    run = stop_in_a_checkpoint(store, 1, 0.0, rows=STUBBORN_ROWS, ignores_the_cancel=True)

    assert run.ignored, run.stderr[-3000:]
    assert run.returncode == EX_OK, run.stderr[-3000:]
    assert run.lines == [
        f"creche-memory: a task of the store loop still ran {STOP_TIMEOUT_S:.0f} s after the "
        "stop. The service exits without it."
    ], run.stderr[-3000:]
    assert not run.group_left
    # The exit ended the worker in a statement of the sync, or between two.
    # The next open replays the WAL. A statement is whole or absent.
    rows = count_rows(store / "system" / "databases" / GRAPH_NAME, "F")
    assert rows >= run.committed
    assert rows % STUBBORN_ROWS == 0


def test_the_unit_signals_the_main_process_alone_and_gives_the_stop_30_s() -> None:
    lines = UNIT.read_text(encoding="utf-8").splitlines()

    assert "KillMode=mixed" in lines
    assert "TimeoutStopSec=30" in lines


def test_the_unit_throttles_at_8g_before_the_kill_at_12g_and_caps_its_swap() -> None:
    lines = UNIT.read_text(encoding="utf-8").splitlines()

    assert "MemoryHigh=8G" in lines
    assert "MemoryMax=12G" in lines
    assert "MemorySwapMax=2G" in lines


def test_a_daemon_thread_returns_the_result_or_the_error() -> None:
    def fails() -> int:
        raise ValueError("no")

    assert asyncio.run(in_daemon_thread(lambda: 7)) == 7
    with pytest.raises(ValueError, match="no"):
        asyncio.run(in_daemon_thread(fails))


#: A process that cancels a read of 60 s in a daemon thread, then exits.
_CANCELLED_READ = """
import asyncio, time
from creche_memory.search import StoreLoop
from creche_memory.sync import in_daemon_thread

loop = StoreLoop()
loop.start()
reading = loop.spawn(in_daemon_thread(lambda: time.sleep(60)))
time.sleep(0.2)
loop.stop()
print("cancelled", reading.cancelled(), flush=True)
"""


def test_the_exit_does_not_wait_for_a_cancelled_read() -> None:
    started = time.monotonic()
    done = subprocess.run(
        [sys.executable, "-c", _CANCELLED_READ], capture_output=True, text=True, timeout=30
    )

    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout == "cancelled True\n"
    assert time.monotonic() - started < 20
