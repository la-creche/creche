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
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
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
#: again. Then it exits with no close, as a stop of the service does.
_FILL = f"""
import asyncio, os, sys
from pathlib import Path
from creche_memory.store import StoreSettings, configure

root = Path(sys.argv[1])
configure(StoreSettings(root, "http://127.0.0.1:9", "unused", "http://127.0.0.1:9", root / "hf"))
from creche_memory.graph import cognee_graph_state

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

    marks = ("Usable(", "Dead(", "full:")

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
