"""The store of the index builder, read as the bridge of the playpen reads it.

The bridge reads a store with the SQLite of Node, on a read-only
connection (contract 03 §7.3 rule 6). Each other scenario of the `library`
topology reads a store with the `sqlite3` module of Python, which can be
another SQLite. So the first two scenarios here run `reader_store.mjs`: a
Node program that opens one store as the bridge opens it and that runs the
statements of the bridge.

The third scenario starts no program. It reads `reader_store.mjs` and
`playpen/bridge/index-store.ts` as text, as `test_proc_table.py` reads the
unit files. It holds the statements of the first file against those of the
second one, so the reader cannot drift from the bridge.

The second scenario runs the reference beside the program of the run.
`integration/proc/AGENTS.md`, "The reference", has the rule.
"""

from __future__ import annotations

import re
from dataclasses import asdict
from typing import Any

import pytest
import standin_tei
from proc_harness import Finished
from proc_library import (
    BIKES,
    BIKES_TEXT,
    DIMS,
    EXIT_OK,
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

#: The file of the bridge that reads a store.
BRIDGE = repo_root() / "playpen" / "bridge" / "index-store.ts"

#: The most rows that the bridge asks of one FTS5 query: `CANDIDATES` of
#: `playpen/bridge/search.ts`.
LIMIT = 20

#: A query that one note of `write_vault` answers, in the form that the
#: bridge gives FTS5: each word in quotes.
ONE_NOTE = '"bicycle"'

#: A query that some notes of `write_mixed` answer, and fewer than `LIMIT`.
SOME_NOTES = '"note" OR "meeting" OR "bicycle"'

#: The two index directories of the scenario with two writers.
BY_REFERENCE = "reference"
JUDGED = "judged"

#: How many bytes one stored vector has: one float32 for each value.
VECTOR_BYTES = DIMS * 4

#: A string literal of a JavaScript or TypeScript file that starts a clause
#: of an SQL statement. The quote is a double quote, or the mark of a
#: template.
_SQL_LITERAL = re.compile(r'(["`])((?:SELECT|FROM|WHERE) [^"`\n]*)\1')

#: The line that gives the plain vector table its name. The bridge puts the
#: name into one statement through a template.
_VECTOR_TABLE = re.compile(r'^const VECTOR_TABLE = "([a-z_]+)";$', re.MULTILINE)


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


def test_the_reader_statements_are_those_of_the_bridge() -> None:
    """Each statement of `reader_store.mjs` is a statement of the bridge, and the reverse.

    The bridge builds one statement from three string literals, and one
    from a template with the constant `VECTOR_TABLE`. So this test compares
    the literals and the constant, and no whole statement.
    """
    reader = reader_script().read_text(encoding="utf-8")
    bridge = BRIDGE.read_text(encoding="utf-8")

    assert _sql_literals(reader) != set()
    assert _sql_literals(reader) == _sql_literals(bridge)
    assert len(_VECTOR_TABLE.findall(reader)) == 1
    assert _VECTOR_TABLE.findall(reader) == _VECTOR_TABLE.findall(bridge)


# -------------------------------------------------------------------- helpers


def _completed(done: Finished) -> None:
    """One run of a writer that it completed: status 0."""
    assert done.exit_code == EXIT_OK, done.stderr


def _rows_of(library: LibraryStack, index: str) -> dict[str, Any]:
    """What the bridge statements give for one store, without the time of its last run.

    The rows of `meta` are in the order of their keys, because two SQLite
    versions can scan one table in another order.
    """
    read = library.read_as_bridge(library.store(index).path, SOME_NOTES, LIMIT)
    meta = sorted((row["key"], row["value"]) for row in read["meta"] if row["key"] != META_UPDATED)

    return read | {"meta": meta}


def _sql_literals(source: str) -> set[str]:
    """Each string literal of one source text that starts a clause of an SQL statement."""
    return {found[1] for found in _SQL_LITERAL.findall(source)}
