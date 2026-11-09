"""Whether the graph database of the memory store can be opened.

Cognee keeps its knowledge graph in one ladybug database file. A worker
process that dies in a checkpoint can leave that file in a state that no
open recovers from. Then every sync and every search fails with the same
`RuntimeError`, and only the operator can repair it (`memory/AGENTS.md`,
the dead graph).

The test is the open itself, not the names of the files beside the
database. A checkpoint that stops part way leaves files that the next open
replays and removes, so a leftover file alone is a normal state. Each file
that is there goes into the report, for the operator.

An error of the open is a verdict only when ladybug raises it for the
file. An error of Cognee's worker process, or a lock that another opener
still holds, says nothing about the file. A restart can clear it, so the
check passes it to the caller."""

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


async def graph_state(graph_file: Path, query: GraphQuery) -> GraphState:
    """Run `query`. Ladybug raises a `RuntimeError` for a database that does
    not open, so a `RuntimeError` from the query means a dead graph, with
    two exceptions. These two go to the caller, as does any other error:

    1. A `SubprocessTransportError`: Cognee's worker process did not start,
       died, or missed a deadline.
    2. A lock that another opener holds (`LOCK_HELD_TEXT`)."""
    try:
        await query()
    except SubprocessTransportError:
        raise
    except RuntimeError as error:
        if LOCK_HELD_TEXT in str(error).lower():
            raise

        return Dead(graph_file, side_files(graph_file), _one_line(str(error)))

    return Usable()


async def cognee_graph_state() -> GraphState:
    """The state of the graph database of Cognee, through Cognee's own engine.
    Call it only after `configure`. The engine opens the database once and
    keeps it open, so a sync and a search use the same open."""
    from cognee.infrastructure.databases.graph import get_graph_config, get_graph_engine

    graph_file = Path(get_graph_config().graph_file_path)

    async def query() -> object:
        engine = await get_graph_engine()
        # The interface types `params` as a bare `dict`.
        return await engine.query("RETURN 1", {})  # pyright: ignore[reportUnknownMemberType]

    return await graph_state(graph_file, query)
