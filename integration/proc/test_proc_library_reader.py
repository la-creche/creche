"""The store of the index builder, read as the bridge of the playpen reads it.

The bridge reads a store with the SQLite of Node, on a read-only
connection (contract 03 §7.3 rule 6). Each other scenario of the `library`
topology reads a store with the `sqlite3` module of Python, which can be
another SQLite. So the first two scenarios here run `reader_store.mjs`: a
Node program that opens one store as the bridge opens it and that runs the
statements of the bridge.

The third scenario holds the rule that the reader is the first program to
open a store: `read_as_bridge` refuses a store that is not alone in its
directory.

The last two scenarios start no program. Each one compares source text, as
`test_proc_table.py` does with the unit files. One holds the statements of
`reader_store.mjs` against those of `playpen/bridge/index-store.ts`, so the
reader cannot drift from the bridge. One holds the limit of a query here
against the constant of `playpen/bridge/search.ts`.

The second scenario runs the reference beside the program of the run.
`integration/proc/AGENTS.md`, "The reference", has the rule.
"""

from __future__ import annotations

import re
from dataclasses import asdict
from typing import Any

import pytest
import standin_tei
from proc_harness import Finished, ProcError
from proc_library import (
    BIKES,
    BIKES_TEXT,
    BY_REFERENCE,
    DIMS,
    EXIT_OK,
    JUDGED,
    META_DIMS,
    META_MODEL,
    META_UPDATED,
    LibraryStack,
    reader_script,
    vector_blob,
    write_mixed,
    write_vault,
)
from proc_tree import repo_root

#: The file of the bridge that reads a store, and the file that gives a
#: query its limit.
BRIDGE = repo_root() / "playpen" / "bridge" / "index-store.ts"
BRIDGE_SEARCH = repo_root() / "playpen" / "bridge" / "search.ts"

#: The most rows that the bridge asks of one FTS5 query: `CANDIDATES` of
#: `playpen/bridge/search.ts`. One test holds the two values equal.
LIMIT = 20

#: A query that one note of `write_vault` answers, in the form that the
#: bridge gives FTS5: each word in quotes.
ONE_NOTE = '"bicycle"'

#: A query that some notes of `write_mixed` answer, and fewer than `LIMIT`.
SOME_NOTES = '"note" OR "meeting" OR "bicycle"'

#: How many bytes one stored vector has: one float32 for each value.
VECTOR_BYTES = DIMS * 4

#: A file that a reader leaves beside a store in WAL mode.
LEFT_FILE = "store.db-shm"

#: A string literal of a JavaScript or TypeScript file that starts a clause
#: of an SQL statement. The quote is a double quote, or the mark of a
#: template. The pattern does not find a literal in single quotes, and it
#: does not find a statement that starts with another word. The count of
#: `PREPARE` covers both: each statement of the two files goes through it.
_SQL_LITERAL = re.compile(r'(["`])((?:SELECT|FROM|WHERE) [^"`\n]*)\1')

#: The call that makes one statement, and the call that runs a text with
#: no statement object. Neither file can use the second one.
PREPARE = ".prepare("
EXEC = ".exec("

#: The whole line that gives the plain vector table its name. The bridge
#: puts the name into one statement through a template.
_VECTOR_TABLE = re.compile(r'const VECTOR_TABLE = "([a-z_]+)";')

#: The options that a file gives the connection to a store.
_OPEN_OPTIONS = re.compile(r"new DatabaseSync\([A-Za-z.]+, (\{[^{}\n]*\})\)")

#: The whole line of `playpen/bridge/search.ts` that gives a query its limit.
_CANDIDATES = re.compile(r"const CANDIDATES = ([0-9]+);")


@pytest.mark.usefixtures("node_sqlite")
def test_the_bridge_statements_read_the_store(library: LibraryStack) -> None:
    """The SQLite of Node reads a store of the program with each statement of the bridge.

    The store gives the model and the count of values, one row for a word
    of one note, and one vector of 3072 bytes for each chunk.

    The reader is the first program that opens the store after the run. A
    reader before it could leave a file beside the store, and the reader of
    this scenario must need none.
    """
    scope = library.vault()
    write_vault(scope)
    _completed(library.run_index(scope, library.index_dir()))

    read = library.read_as_bridge(library.store().path, ONE_NOTE, LIMIT)
    chunks = library.store().chunks()
    meta = {row["key"]: row["value"] for row in read["meta"]}
    vectors = {row["id"]: bytes.fromhex(row["embedding"]["hex"]) for row in read["vectors"]}

    assert set(meta) == {META_MODEL, META_DIMS, META_UPDATED}
    assert (meta[META_MODEL], meta[META_DIMS]) == (standin_tei.DEFAULT_MODEL, str(DIMS))
    assert read["table"] is True
    assert read["lexical"] == [
        {"id": chunks[0].id, "path": str(scope.resolve() / BIKES), "ord": 0, "text": BIKES_TEXT}
    ]
    assert len(chunks) == 2
    assert vectors == {chunk.id: vector_blob(chunk.text) for chunk in chunks}
    assert {len(blob) for blob in vectors.values()} == {VECTOR_BYTES}
    assert read["chunks"] == [asdict(chunk) for chunk in chunks]


@pytest.mark.usefixtures("node_sqlite")
def test_the_bridge_statements_give_the_same_rows_for_both_writers(library: LibraryStack) -> None:
    """The bridge gets equal rows from a store of the reference and from a store of the program.

    The rows of the FTS5 query are in the order of their rank, so both
    stores must give one order. The reader is the first program that opens
    each store after its run.
    """
    scope = library.vault()
    write_mixed(scope)
    _completed(library.run_reference(scope, library.index_dir(BY_REFERENCE)))
    _completed(library.run_index(scope, library.index_dir(JUDGED)))

    reference = _rows_of(library, BY_REFERENCE)
    judged = _rows_of(library, JUDGED)

    assert 1 < len(reference["lexical"]) < LIMIT
    assert len(reference["vectors"]) == len(library.store(BY_REFERENCE).chunks()) > 0
    assert judged == reference


def test_the_reader_refuses_a_store_that_is_not_alone(library: LibraryStack) -> None:
    """`read_as_bridge` raises when the index directory holds a file beside the store.

    A reader before the reader of the bridge can leave such a file. The
    reader of the bridge then finds what a mount of a sandbox does not
    have, and it can read a store that needs the file. The refusal comes
    before the reader program starts, so this scenario needs no Node.
    """
    scope = library.vault()
    write_vault(scope)
    _completed(library.run_index(scope, library.index_dir()))
    (library.index_dir() / LEFT_FILE).write_bytes(b"")

    with pytest.raises(ProcError, match=LEFT_FILE):
        library.read_as_bridge(library.store().path, ONE_NOTE, LIMIT)


def test_the_reader_statements_are_those_of_the_bridge() -> None:
    """Each statement of `reader_store.mjs` is a statement of the bridge, and the reverse.

    Two statements of the bridge are not one literal. One is the sum of
    three literals. One is a template that takes the name of a table from
    `VECTOR_TABLE`. So this test compares each literal and that constant,
    and it compares no whole statement.

    The pattern of a literal does not find each statement that a file can
    hold. So the two files also make the same count of statements, neither
    one runs a text with no statement, and both open the store with the
    same options.
    """
    reader = reader_script().read_text(encoding="utf-8")
    bridge = BRIDGE.read_text(encoding="utf-8")

    assert _sql_literals(reader) != set()
    assert _sql_literals(reader) == _sql_literals(bridge)
    assert len(_whole_lines(_VECTOR_TABLE, reader)) == 1
    assert _whole_lines(_VECTOR_TABLE, reader) == _whole_lines(_VECTOR_TABLE, bridge)
    assert reader.count(PREPARE) == bridge.count(PREPARE) > 0
    assert (reader.count(EXEC), bridge.count(EXEC)) == (0, 0)
    assert len(_OPEN_OPTIONS.findall(reader)) == 1
    assert _OPEN_OPTIONS.findall(reader) == _OPEN_OPTIONS.findall(bridge)


def test_the_limit_of_a_query_is_that_of_the_bridge() -> None:
    """`LIMIT` of this file is the count of rows that the bridge asks of one query."""
    search = BRIDGE_SEARCH.read_text(encoding="utf-8")

    assert _whole_lines(_CANDIDATES, search) == [str(LIMIT)]


# -------------------------------------------------------------------- helpers


def _completed(done: Finished) -> None:
    """One run of a writer that it completed: status 0."""
    assert done.exit_code == EXIT_OK, done.stderr


def _rows_of(library: LibraryStack, index: str) -> dict[str, Any]:
    """What the bridge statements give for one store, without the time of its last run.

    The rows of `meta` are in the order of their keys here. A scan gives
    them in the order in which a writer added them, and no reader depends
    on that order.
    """
    read = library.read_as_bridge(library.store(index).path, SOME_NOTES, LIMIT)
    meta = sorted((row["key"], row["value"]) for row in read["meta"] if row["key"] != META_UPDATED)

    return read | {"meta": meta}


def _sql_literals(source: str) -> set[str]:
    """Each string literal of one source text that starts a clause of an SQL statement."""
    return {found[1] for found in _SQL_LITERAL.findall(source)}


def _whole_lines(pattern: re.Pattern[str], source: str) -> list[str]:
    """The first group of one pattern, for each line of a source text that the pattern is."""
    lines = (pattern.fullmatch(line) for line in source.splitlines())

    return [found[1] for found in lines if found is not None]
