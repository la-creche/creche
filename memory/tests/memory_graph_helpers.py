"""Real ladybug databases for the tests, in the states that the service meets.

The states come from runs of ladybug 0.19.0 that killed a writer in a
checkpoint (the pull request of `graph.py` has the runs). A garbage
`.wal.checkpoint` beside a closed database gives the error of 2026-11-04 on
every open. An empty `.shadow` with the two lock files is a state that the
next open recovers from and removes.

`fill_through_cognee` runs the check of the service, through Cognee's
engine, in a process of its own: Cognee reads its settings once, at its
first import."""

import asyncio
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import ladybug
from creche_memory.graph import GraphCheck, GraphState, graph_state

#: The name of Cognee's graph file under `<store>/system/databases`.
GRAPH_NAME = "cognee_graph_ladybug"

#: A small buffer pool: the default reserves most of the memory of the host.
BUFFER_POOL = 1 << 26

LOCKS = (".checkpoint.intent.lock", ".checkpoint.apply.lock")

#: The size of the frozen WAL of 2026-11-04.
FROZEN_WAL_BYTES = 27105


def make_graph(graph_file: Path) -> Path:
    """A real database with one table and 100 rows, closed cleanly."""
    graph_file.parent.mkdir(parents=True, exist_ok=True)
    db = ladybug.Database(str(graph_file), buffer_pool_size=BUFFER_POOL)
    conn = ladybug.Connection(db)
    conn.execute("CREATE NODE TABLE N(id INT64 PRIMARY KEY)")
    conn.execute("UNWIND range(1, 100) AS i CREATE (:N {id: i})")
    conn.close()
    db.close()

    return graph_file


def _beside(graph_file: Path, suffix: str, data: bytes = b"") -> None:
    graph_file.with_name(graph_file.name + suffix).write_bytes(data)


def kill_graph(graph_file: Path) -> None:
    """The files of 2026-11-04: a frozen WAL that fails its checksum, an
    empty shadow and the two checkpoint locks."""
    _beside(graph_file, ".wal.checkpoint", b"\x5a" * FROZEN_WAL_BYTES)
    _beside(graph_file, ".shadow")
    for lock in LOCKS:
        _beside(graph_file, lock)


def leave_recoverable(graph_file: Path) -> None:
    """The files of a checkpoint that stopped early: an empty shadow and the
    two locks. The next open removes them."""
    _beside(graph_file, ".shadow")
    for lock in LOCKS:
        _beside(graph_file, lock)


def count_rows(graph_file: Path, table: str) -> int:
    """The rows of `table`, by a clean open and close."""
    db = ladybug.Database(str(graph_file), buffer_pool_size=BUFFER_POOL)
    conn = ladybug.Connection(db)
    try:
        result = conn.execute(f"MATCH (n:{table}) RETURN count(*)")
        assert not isinstance(result, list)
        rows = result.get_all()
    finally:
        conn.close()
        db.close()

    return int(rows[0][0])


#: The rows of table `F` that `fill_through_cognee` writes.
FILL_ROWS = 200000

#: The largest graph file of `fill_through_cognee`.
FULL_DB_BYTES = 1 << 25

#: A child that runs the check of the service through Cognee's engine and
#: prints the state. A checkpoint through the same open engine then fills
#: the database past `KUZU_MAX_DB_SIZE`, and the child prints the state
#: again. Then it runs the close of the stop and prints what it did, and it
#: exits with no other close.
_FILL = f"""
import asyncio, os, sys
from pathlib import Path
from creche_memory.store import StoreSettings, configure

root = Path(sys.argv[1])
configure(StoreSettings(root, "http://127.0.0.1:9", "unused", "http://127.0.0.1:9", root / "hf"))
from creche_memory.graph import close_cognee_graph, cognee_graph_state

async def main():
    print(repr(await cognee_graph_state()), flush=True)
    from cognee.infrastructure.databases.graph import get_graph_engine
    engine = await get_graph_engine()
    # A checkpoint of the empty database first. Without it, the checkpoint
    # after the rows fits under the limit.
    await engine.query("CHECKPOINT", {{}})
    await engine.query("CREATE NODE TABLE F(id INT64 PRIMARY KEY, s STRING)", {{}})
    # The rows fit in memory. The checkpoint that writes them fails.
    row = "UNWIND range(1, {FILL_ROWS}) AS i CREATE (:F {{id: i, s: '" + "x" * 64 + "'}})"
    await engine.query(row, {{}})
    try:
        await engine.query("CHECKPOINT", {{}})
    except RuntimeError as error:
        print("full:", "Maximum database size" in str(error), flush=True)
    print(repr(await cognee_graph_state()), flush=True)
    # The close of the stop, after the failed checkpoint.
    print(repr(await close_cognee_graph()), flush=True)
    os._exit(0)

asyncio.run(main())
"""


def fill_through_cognee(store_root: Path) -> list[str]:
    """The lines that `_FILL` prints, for a store at `store_root`."""
    (store_root / "hf").mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "NO_COLOR": "1", "KUZU_MAX_DB_SIZE": str(FULL_DB_BYTES)}
    done = subprocess.run(
        [sys.executable, "-c", _FILL, str(store_root)],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert done.returncode == 0, done.stderr[-3000:]

    marks = ("Usable(", "Dead(", "full:", "Closed(", "LeftOpen(")

    return [line for line in done.stdout.splitlines() if line.startswith(marks)]


#: A child that opens the database, says so, and holds it until its stdin closes.
_HOLDER = """
import sys, ladybug
db = ladybug.Database(sys.argv[1], buffer_pool_size=int(sys.argv[2]))
ladybug.Connection(db).execute("RETURN 1")
print("open", flush=True)
sys.stdin.read()
"""


@contextmanager
def held_open(graph_file: Path) -> Iterator[None]:
    """Another process holds the database open, and so holds its lock."""
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(graph_file), str(BUFFER_POOL)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdin is not None
    assert holder.stdout is not None
    try:
        assert holder.stdout.readline() == "open\n"
        yield
    finally:
        holder.stdin.close()
        holder.wait(timeout=30)


def _open_and_close(graph_file: Path) -> None:
    db = ladybug.Database(str(graph_file), buffer_pool_size=BUFFER_POOL)
    conn = ladybug.Connection(db)
    conn.execute("RETURN 1")
    conn.close()
    db.close()


def ladybug_check(graph_file: Path) -> GraphCheck:
    """The check of `graph.py`, with an open by ladybug itself."""

    async def query() -> None:
        await asyncio.to_thread(_open_and_close, graph_file)

    async def check() -> GraphState:
        return await graph_state(graph_file, query)

    return check


#: The rows of one statement of `_WRITER`. One statement and its checkpoint
#: make a WAL of about 23 MB, far over one write of Cognee (2,000 rows).
WRITE_ROWS = 200000

#: The rows of one statement of the writer that ignores its first cancel.
#: Its statements are short, so it sends many in the time of the cancel.
STUBBORN_ROWS = 20000

#: A child that runs the stop of the service around a sync that writes. It
#: installs the signal handler of the service, checks the graph, and starts
#: a real `Syncer` on a `StoreLoop`. The sync writes `sys.argv[2]` rows
#: through Cognee's engine, then checkpoints, until the stop. Each line
#: says where the writer is. At SIGTERM, `stop_store` ends the store and
#: gives the exit status. With `sys.argv[3]` set to `ignore`, the sync
#: ignores its first cancel and writes on, as a task in a rollback can.
_WRITER = """
import asyncio, sys, threading
from pathlib import Path
from creche_memory.store import StoreSettings, configure

root = Path(sys.argv[1])
configure(StoreSettings(root, "http://127.0.0.1:9", "unused", "http://127.0.0.1:9", root / "hf"))
from creche_memory.cli import stop_signals, stop_store
from creche_memory.graph import cognee_graph_state
from creche_memory.search import StoreLoop
from creche_memory.sync import Syncer

vault = root / "vault"
(vault / "notes").mkdir(parents=True)
(vault / "notes" / "a.md").write_text("a", encoding="utf-8")

each = int(sys.argv[2])
ignores = sys.argv[3] == "ignore"

async def write(folders):
    from cognee.infrastructure.databases.graph import get_graph_engine
    engine = await get_graph_engine()
    await engine.query("CREATE NODE TABLE F(id INT64 PRIMARY KEY, s STRING)", {})
    done = 0
    ignored = False
    while True:
        # The ids move on before the await: a cancelled statement still
        # ends in the worker.
        first, done = done + 1, done + each
        try:
            rows = "UNWIND range(" + str(first) + ", " + str(done) + ") AS i "
            await engine.query(rows + "CREATE (:F {id: i, s: '" + "x" * 64 + "'})", {})
            print("checkpoint", flush=True)
            await engine.query("CHECKPOINT", {})
        except asyncio.CancelledError:
            if not ignores or ignored:
                raise
            ignored = True
            print("ignored the cancel", flush=True)
            continue
        print("committed", done, flush=True)

stop = threading.Event()
stop_signals(stop)
loop = StoreLoop()
loop.start()
print(repr(loop.spawn(cognee_graph_state()).result()), flush=True)
syncer = Syncer(vault, write, cognee_graph_state, poll_s=0.05, quiet_s=0.0)
syncing = loop.spawn(syncer.run(stop))
stop.wait()
syncer.wake()
sys.exit(stop_store(loop, syncing))
"""


@dataclass(frozen=True)
class StopInAWrite:
    """What one stop of `_WRITER` left."""

    returncode: int
    #: The lines of the service, `creche-memory: ...`.
    lines: list[str]
    #: The rows that a checkpoint had made durable before the SIGTERM.
    committed: int
    #: Whether a process of the child's group still ran after the child's exit.
    group_left: bool
    #: Whether the sync ignored its first cancel.
    ignored: bool
    stderr: str


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False

    return True


def stop_in_a_checkpoint(
    store_root: Path,
    nth: int,
    delay_s: float,
    *,
    rows: int = WRITE_ROWS,
    ignores_the_cancel: bool = False,
) -> StopInAWrite:
    """Start `_WRITER` on a new store, and send SIGTERM to its main process
    alone, as `KillMode=mixed` does, `delay_s` after it starts its `nth`
    checkpoint. Then wait for its exit. Each statement writes `rows` rows."""
    (store_root / "hf").mkdir(parents=True)
    mode = "ignore" if ignores_the_cancel else "obey"
    writer = subprocess.Popen(
        [sys.executable, "-c", _WRITER, str(store_root), str(rows), mode],
        env={**os.environ, "NO_COLOR": "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert writer.stdout is not None
    committed = 0
    started = 0
    try:
        for line in writer.stdout:
            if line.startswith("committed "):
                committed = int(line.split()[1])
            elif line == "checkpoint\n":
                started += 1
                if started == nth:
                    break
        assert started == nth, "the writer ended before its checkpoint"
        time.sleep(delay_s)
        writer.send_signal(signal.SIGTERM)
        out, err = writer.communicate(timeout=120)
        left = _group_alive(writer.pid)
    finally:
        # systemd kills what is left of the unit after the main process.
        if _group_alive(writer.pid):
            os.killpg(writer.pid, signal.SIGKILL)
        writer.wait(timeout=30)

    lines = [one for one in err.splitlines() if one.startswith("creche-memory: ")]

    ignored = "ignored the cancel\n" in out

    return StopInAWrite(writer.returncode, lines, committed, left, ignored, err)
