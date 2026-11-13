"""Real ladybug databases for the tests, in the states that the service meets.

The states come from runs of ladybug 0.19.0 that killed a writer in a
checkpoint (the pull request of `graph.py` has the runs). A garbage
`.wal.checkpoint` beside a closed database gives the error of 2026-11-04 on
every open. An empty `.shadow` with the two lock files is a state that the
next open recovers from and removes."""

import asyncio
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
