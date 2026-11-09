"""Whether the graph database of the memory store can be opened.

Cognee keeps its knowledge graph in one ladybug database file. A worker
process that dies in a checkpoint can leave that file in a state that no
open recovers from. Then every sync and every search fails with the same
error, and only the operator can repair it (`memory/AGENTS.md`, the dead
graph).

The test is the open itself, not the names of the files beside the
database. A checkpoint that stops part way leaves files that the next open
replays and removes, so a leftover file alone is a normal state. Each file
that is there before the open goes into the report, for the operator.

The check never closes the engine, and its own query writes nothing. It
asks Cognee's cached engine `RETURN 1`. A checkpoint in the check can lose
rows or leave a file that does not open. A close after a failed checkpoint
can leave a file that does not open. The first check of a process builds
Cognee's engine, and that build runs three `CREATE ... IF NOT EXISTS`
statements. Their commit can write a `.wal` and can checkpoint a replayed
WAL (`memory/AGENTS.md`, known gaps).

An error of the open is a verdict only when ladybug raises it for the
file. An error of Cognee's worker process, a lock that another opener
still holds, or an error of the host such as a full disk says nothing
about the file. A restart can clear it, so the check passes it to the
caller."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from cognee_db_workers.harness import SubprocessTransportError

#: The files that ladybug 0.19.0 keeps beside the database while it writes
#: or checkpoints. A clean close leaves none of them.
SIDE_SUFFIXES = (
    ".wal",
    ".wal.checkpoint",
    ".shadow",
    ".checkpoint.intent.lock",
    ".checkpoint.apply.lock",
)

#: The text of ladybug's error for a lock that another opener holds, in
#: lower case. Cognee's worker matches the same text, retries the open, and
#: does not treat it as a broken file (`cognee_db_workers/kuzu_worker.py`).
LOCK_HELD_TEXT = "could not set lock on file"

#: The texts of host errors that ladybug puts in its `IO exception`, in
#: lower case. Each one is a state of the host, not of the file: a restart
#: after the operator frees space, fixes a mode or a flag, raises a quota,
#: or after a device fault ends, can open the file again.
HOST_ERROR_TEXTS = (
    "no space left on device",
    "disk quota exceeded",
    "permission denied",
    "operation not permitted",
    "read-only file system",
    "too many open files",
    "input/output error",
)

#: The types that ladybug usually raises for a file that does not open.
#: pybind gives a C++ `std::out_of_range` from the open as an `IndexError`.
#: Some dead files raise a `MemoryError` instead (`memory/AGENTS.md`, known
#: gaps). Host memory pressure raises it too, so it is not a verdict.
VERDICT_ERRORS = (RuntimeError, IndexError)

#: The longest text of the library's error that the report keeps.
MAX_ERROR_CHARS = 400


@dataclass(frozen=True)
class Usable:
    """The graph database opens."""


@dataclass(frozen=True)
class Dead:
    """The graph database does not open. Only the operator can repair it."""

    #: The database file.
    graph_file: Path
    #: The names of the files beside it, from `SIDE_SUFFIXES`.
    side_files: tuple[str, ...]
    #: The text of the open's error, on one line.
    error: str

    def report(self) -> str:
        """One journal line: the cause, then the operator's recovery."""
        side = ", ".join(self.side_files) or "none"
        databases = self.graph_file.parent

        return (
            f"The graph database {self.graph_file} does not open: {self.error.rstrip('.')}. "
            f"Files beside it: {side}. "
            f"To recover, set aside {databases}. "
            "Then start the unit. "
            "The first sync rebuilds the store from the vault."
        )


class NotAVerdict(Exception):
    """An error of the open that says nothing about the file: a lock that
    another opener holds, or an error of the host. Its text is ladybug's,
    on one line. It names a file of the store, and the lock text links to
    ladybug's docs. It holds no address of a service, so the journal line
    can print it."""


type GraphState = Usable | Dead

#: A call that opens the graph database and asks it one query.
type GraphQuery = Callable[[], Awaitable[object]]

#: The state of the graph database, at the time of the call.
type GraphCheck = Callable[[], Awaitable[GraphState]]


def side_files(graph_file: Path) -> tuple[str, ...]:
    """The names of the files from `SIDE_SUFFIXES` that are beside `graph_file`."""
    names = (graph_file.name + suffix for suffix in SIDE_SUFFIXES)

    return tuple(name for name in names if (graph_file.parent / name).exists())


def _one_line(text: str) -> str:
    return " ".join(text.split())[:MAX_ERROR_CHARS]


def _passes(error: Exception) -> bool:
    text = str(error).lower()

    return LOCK_HELD_TEXT in text or any(host in text for host in HOST_ERROR_TEXTS)


async def graph_state(graph_file: Path, query: GraphQuery) -> GraphState:
    """Run `query`. Ladybug usually raises a `RuntimeError` or an
    `IndexError` for a database that does not open, so either one from the
    query means a dead graph, with three exceptions. These three go to the
    caller, as does any other error:

    1. A `SubprocessTransportError`: Cognee's worker process did not start,
       died, or missed a deadline.
    2. A lock that another opener holds (`LOCK_HELD_TEXT`), as `NotAVerdict`.
    3. An error of the host, such as a full disk (`HOST_ERROR_TEXTS`), as
       `NotAVerdict`.

    The report names the files that are beside the database before the
    query, because Cognee's open can remove one of them."""
    beside = side_files(graph_file)
    try:
        await query()
    except SubprocessTransportError:
        raise
    except VERDICT_ERRORS as error:
        if _passes(error):
            raise NotAVerdict(_one_line(str(error))) from error

        return Dead(graph_file, beside, _one_line(str(error)))

    return Usable()


async def cognee_graph_state() -> GraphState:
    """The state of the graph database of Cognee, through Cognee's own engine.
    Call it only after `configure`. The engine opens the database once and
    keeps it open, so a sync, a search and each later check use the same
    open. The check does not close the engine."""
    from cognee.infrastructure.databases.graph import get_graph_config, get_graph_engine

    graph_file = Path(get_graph_config().graph_file_path)

    async def query() -> object:
        engine = await get_graph_engine()
        # The interface types `params` as a bare `dict`.
        return await engine.query("RETURN 1", {})  # pyright: ignore[reportUnknownMemberType]

    return await graph_state(graph_file, query)
