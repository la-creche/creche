"""A stop of the service never kills the graph database.

A SIGTERM in a write cancels the sync, waits for the statement that the
graph worker runs, checkpoints and closes the database. After a failed
checkpoint of the same open, the stop writes nothing, because a checkpoint
or a close then leaves a file that does not open.

`MEMORY_STOP_TRIALS` sets how many stops in a checkpoint the first test
runs. The gate runs the default. The pull request of the stop ran it with a
larger number."""

import asyncio
import os
import random
import subprocess
import sys
import time
from pathlib import Path

import ladybug
import pytest
from creche_memory.cli import EX_OK
from creche_memory.graph import Closed, LeftOpen, close_graph, side_files, unfinished_files
from creche_memory.sync import in_daemon_thread
from memory_graph_helpers import (
    BUFFER_POOL,
    GRAPH_NAME,
    WRITE_ROWS,
    count_rows,
    leave_recoverable,
    make_graph,
    stop_in_a_checkpoint,
)

TRIALS = int(os.environ.get("MEMORY_STOP_TRIALS", "3"))

UNIT = Path(__file__).resolve().parents[2] / "systemd" / "creche-memory.service"


@pytest.mark.parametrize("trial", range(TRIALS))
def test_a_sigterm_in_a_checkpoint_closes_the_graph_and_keeps_every_row(
    tmp_path: Path, trial: int
) -> None:
    store = tmp_path / "store"
    # The 2026-11-09 kills that left a dead graph came 4 to 13 ms into a
    # checkpoint. Each trial sends SIGTERM 0 to 30 ms into one.
    rng = random.Random(trial)
    run = stop_in_a_checkpoint(store, rng.randint(1, 3), rng.uniform(0.0, 0.03))

    assert run.returncode == EX_OK, run.stderr[-3000:]
    assert len(run.lines) == 1, run.stderr[-3000:]
    assert run.lines[0].startswith("creche-memory: The graph database closed in ")
    # The close ended the worker: nothing of the group is left to kill.
    assert not run.group_left
    graph_file = store / "system" / "databases" / GRAPH_NAME
    # A clean close leaves the database file alone.
    assert side_files(graph_file) == ()
    rows = count_rows(graph_file, "F")
    # Every row that a checkpoint made durable is there. A statement is
    # whole or absent.
    assert rows >= run.committed
    assert rows % WRITE_ROWS == 0


def _probe(conn: ladybug.Connection) -> object:
    return conn.execute("RETURN 1")


def test_the_stop_closes_a_graph_with_a_wal_and_leaves_one_file(tmp_path: Path) -> None:
    graph_file = make_graph(tmp_path / GRAPH_NAME)
    db = ladybug.Database(str(graph_file), buffer_pool_size=BUFFER_POOL)
    conn = ladybug.Connection(db)
    conn.execute("UNWIND range(101, 200) AS i CREATE (:N {id: i})")
    assert side_files(graph_file) == (f"{GRAPH_NAME}.wal",)

    async def probe() -> object:
        return _probe(conn)

    async def checkpoint() -> object:
        return conn.execute("CHECKPOINT")

    async def close() -> None:
        conn.close()
        db.close()

    closed = asyncio.run(close_graph(graph_file, probe, checkpoint, close))

    assert isinstance(closed, Closed)
    assert closed.report().startswith("The graph database closed in ")
    assert side_files(graph_file) == ()
    assert count_rows(graph_file, "N") == 200


def test_the_files_of_an_unfinished_checkpoint_keep_the_graph_open(tmp_path: Path) -> None:
    graph_file = make_graph(tmp_path / GRAPH_NAME)
    leave_recoverable(graph_file)
    calls: list[str] = []

    async def probe() -> None:
        calls.append("probe")

    async def checkpoint() -> None:
        calls.append("checkpoint")

    async def close() -> None:
        calls.append("close")

    left = asyncio.run(close_graph(graph_file, probe, checkpoint, close))

    assert isinstance(left, LeftOpen)
    assert calls == ["probe"]
    assert unfinished_files(graph_file) == (
        f"{GRAPH_NAME}.shadow",
        f"{GRAPH_NAME}.checkpoint.intent.lock",
        f"{GRAPH_NAME}.checkpoint.apply.lock",
    )
    assert left.report() == (
        "The graph database stays open until the service exits: a checkpoint did not end "
        f"({GRAPH_NAME}.shadow, {GRAPH_NAME}.checkpoint.intent.lock, "
        f"{GRAPH_NAME}.checkpoint.apply.lock). The next start replays its WAL."
    )


def test_a_wal_alone_is_not_an_unfinished_checkpoint(tmp_path: Path) -> None:
    graph_file = make_graph(tmp_path / GRAPH_NAME)
    graph_file.with_name(f"{GRAPH_NAME}.wal").write_bytes(b"")

    assert unfinished_files(graph_file) == ()


def test_a_failed_statement_or_checkpoint_keeps_the_graph_open(tmp_path: Path) -> None:
    graph_file = make_graph(tmp_path / GRAPH_NAME)
    db = ladybug.Database(str(graph_file), buffer_pool_size=BUFFER_POOL)
    conn = ladybug.Connection(db)
    closes: list[str] = []

    async def fails() -> object:
        # A real error of ladybug: the table does not exist.
        return conn.execute("MATCH (n:Missing) RETURN n")

    async def probe() -> object:
        return _probe(conn)

    async def close() -> None:
        closes.append("close")

    first = asyncio.run(close_graph(graph_file, fails, probe, close))
    second = asyncio.run(close_graph(graph_file, probe, fails, close))
    conn.close()
    db.close()

    assert first == LeftOpen("the last statement failed (RuntimeError)")
    assert second == LeftOpen("the checkpoint of the stop failed (RuntimeError)")
    assert closes == []


def test_the_unit_signals_the_main_process_alone_and_gives_the_stop_60_s() -> None:
    lines = UNIT.read_text(encoding="utf-8").splitlines()

    assert "KillMode=mixed" in lines
    assert "KillSignal=SIGTERM" in lines
    assert "TimeoutStopSec=60" in lines


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
