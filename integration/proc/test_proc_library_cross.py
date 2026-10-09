"""One store, two writers: the index builder of the run and the reference.

The program is what the `library` row starts in this run: its default
command, or the value of its variable. The reference is the default
command of that row, also when the variable is set. A host can have an
index that one of the two built and that the other one updates: after a
change to another binary, and after the way back. So each scenario here
runs both on one corpus, with two index directories:

    state/index/reference   only the reference writes this store
    state/index/judged      the program writes this store, or updates it

Both stores hold the paths of the one corpus, so a correct pair of writers
gives equal content. `store_content` of `proc_library.py` is that content:
each row of each table without the time of a run. A scenario does each step
for both index directories before it changes the corpus, because a row of
`files` holds the mtime of its file.

No test of `library/tests` is the origin of these scenarios. With no
variable set, the program is the reference, and each scenario compares one
program with itself. That run proves that the comparison gives one result
for one corpus.

`integration/proc/AGENTS.md`, "The reference", has the rules for the one
place where this suite runs a default command beside the judged command.

The last three scenarios hold the comparison itself, and they run no
reference. With no variable set, a comparison that sees nothing passes
each scenario above. So one scenario changes a copy of a store in each
part and reads the line that the comparison gives for it.

CONTRACT-QUESTION: `library/AGENTS.md` gives the schema and six rules for
one writer. No contract names a second writer of one store. Reading taken,
the strict one: for one corpus, both writers give equal rows in each table,
equal chunk ids, equal ranks for the queries of `store_content`, equal
embed calls and an equal first line of the report. The scenarios compare
no time of a run, no form of `chunks_vec` and no sentence of an error line.
A change costs one part of `StoreContent` in `proc_library.py`.
"""

from __future__ import annotations

import math
import os
import shlex
import signal
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from proc_harness import Finished, ProcError
from proc_library import (
    BIKES,
    BROKEN_PDF,
    BY_REFERENCE,
    CANARY,
    CONTENT_QUERIES,
    EXIT_OK,
    FILE_PARAGRAPHS,
    HELD_TEXT,
    JUDGED,
    META_UPDATED,
    RANK_TOLERANCE,
    STORE_FILE,
    Hit,
    LibraryStack,
    StoreContent,
    change_store,
    copy_store,
    report_of,
    store_content,
    vector_blob,
    write_corpus_of,
    write_mixed,
    write_note,
    write_vault,
)
from proc_services import SERVICES, Service, command_of
from proc_standins import TEI, TEI_MODEL, tune

#: A model id that the stand-in gives after a change of model.
NEW_MODEL = "new-model"

#: A word of each paragraph of `write_corpus_of`, and two words that take
#: its place when a scenario changes a file.
PARAGRAPH_WORD = "paragraph"
FIRST_CHANGE = "passage"
SECOND_CHANGE = "section"

#: The two files of `write_corpus_of` in the corpus of `write_mixed`. The
#: first one is full, so its chunks need two embed calls.
FULL_DOC = "doc-0000.md"
REST_DOC = "doc-0001.md"

#: The files that the two steps of `_change` and `_change_again` add.
ADDED = "added.md"
ADDED_LATER = "later/added.md"

#: How many chunks the corpus of the backfill scenario has. One `vec0` table
#: keeps 1,024 vectors in one row of its storage, so this corpus needs two
#: such rows. The scenario removes one file, which leaves empty places in
#: the first row.
BACKFILL_CHUNKS = 1100
REMOVED_DOC = "doc-0003.md"

#: How many chunks the corpus of the killed-run scenario has. A killed run
#: can leave the journal of its transaction beside its work file. SQLite
#: gives the pages of that journal to the next program that opens a file
#: of that name, but only the pages that the killed run synced. It syncs a
#: journal when the changed pages no longer fit its page cache, which holds
#: about 2 MB. A store of this corpus has about 7 MB, and the killed run
#: changes each chunk but those of the last file.
KILLED_CHUNKS = 600

#: How long the program has to end after a kill.
STOP_DEADLINE_S = 30.0

#: The start of the line that the comparison gives for each query of
#: `CONTENT_QUERIES`.
ROWS_OF_EACH_QUERY = [f"the rows of MATCH {query}" for query in CONTENT_QUERIES]

#: The chunk that the comparison scenario changes: the second chunk of the
#: full file. Its text holds a word of each query of `CONTENT_QUERIES`.
SECOND_CHUNK = (FULL_DOC, 1)

#: The start of what `store_content` says of a time that it refuses.
UPDATED_REFUSED = "`updated_at` of `meta`"
INDEXED_REFUSED = "`indexed_at` of "

#: What the program of the variable writes into its record at each start.
STARTED = "started"


def test_the_program_and_the_reference_build_the_same_store(library: LibraryStack) -> None:
    """A first run of each program on one corpus gives the same rows in each table."""
    scope = library.vault()
    write_mixed(scope)

    _by_reference(library, scope, BY_REFERENCE)
    _by_program(library, scope, JUDGED)

    assert _differences(library) == []


def test_the_program_updates_a_store_of_the_reference(library: LibraryStack) -> None:
    """The program reads a store that the reference built, and it leaves what the reference leaves.

    The update changes one file, removes one and adds one.
    """
    scope = library.vault()
    write_mixed(scope)
    _reference_on_both(library, scope)
    _change(scope)

    _by_reference(library, scope, BY_REFERENCE)
    updated = report_of(_by_program(library, scope, JUDGED))

    assert (updated.indexed, updated.removed) == (2, 1)
    assert _differences(library) == []


def test_the_reference_updates_a_store_of_the_program(library: LibraryStack) -> None:
    """The way back: the reference reads a store that the program built."""
    scope = library.vault()
    write_mixed(scope)
    _by_reference(library, scope, BY_REFERENCE)
    _by_program(library, scope, JUDGED)
    _change(scope)

    _by_reference(library, scope, BY_REFERENCE)
    updated = report_of(_by_reference(library, scope, JUDGED))

    assert (updated.indexed, updated.removed) == (2, 1)
    assert _differences(library) == []


def test_the_two_writers_take_turns(library: LibraryStack) -> None:
    """Three runs on one store: the program, the reference, the program.

    The corpus changes between two runs. The second change removes the
    chunks with the highest ids before it adds chunks, so both writers must
    give a new chunk the same id.
    """
    scope = library.vault()
    write_mixed(scope)

    _by_reference(library, scope, BY_REFERENCE)
    _by_program(library, scope, JUDGED)
    after_build = _differences(library)
    _change(scope)
    _by_reference(library, scope, BY_REFERENCE)
    _by_reference(library, scope, JUDGED)
    after_first_change = _differences(library)
    _change_again(scope)
    _by_reference(library, scope, BY_REFERENCE)
    _by_program(library, scope, JUDGED)

    assert after_build == []
    assert after_first_change == []
    assert _differences(library) == []


def test_the_program_backfills_a_store_of_the_reference_without_reembedding(
    library: LibraryStack,
) -> None:
    """A store of the reference with no `chunks_emb` gets the table from the vectors that it has.

    The program must find each vector of a store that the reference wrote:
    more vectors than one storage row of a `vec0` table holds, and empty
    places where a removed file had its vectors.
    """
    scope = library.vault()
    write_corpus_of(scope, BACKFILL_CHUNKS)
    _reference_on_both(library, scope)
    (scope / REMOVED_DOC).unlink()
    _reference_on_both(library, scope)
    library.store(BY_REFERENCE).drop_plain_vectors()
    library.store(JUDGED).drop_plain_vectors()
    _by_reference(library, scope, BY_REFERENCE)
    first_calls = len(library.embedded())

    backfilled = report_of(_by_program(library, scope, JUDGED))
    vectors = store_content(library.store(JUDGED).path).plain_vectors

    assert library.embedded()[first_calls:] == [[CANARY]]
    assert (backfilled.indexed, backfilled.removed) == (0, 0)
    assert len(vectors) == BACKFILL_CHUNKS - FILE_PARAGRAPHS
    assert _differences(library) == []


def test_the_reference_runs_after_a_killed_run_of_the_program(library: LibraryStack) -> None:
    """A kill of the program leaves nothing that damages the next run of the reference.

    Each index unit ends a run that is too long with SIGKILL, and a host can
    go back to the reference after such a run. The killed run here changed
    each file of the corpus but the last one, in one transaction. A journal
    that it left beside its work file must not reach the store that the
    reference then builds.

    CONTRACT-QUESTION: no contract names the files of an index directory.
    Reading taken: after the run of the reference, the directory holds the
    store and no file of the killed run. A program that leaves a file that
    the reference does not remove leaves it for good. A change costs one
    assertion here.
    """
    scope = library.vault()
    last = write_corpus_of(scope, KILLED_CHUNKS)
    files = len(list(scope.iterdir()))
    _reference_on_both(library, scope)
    _change_each_file(scope, last)

    child = library.start_held(scope, library.index_dir(JUDGED))
    child.send(signal.SIGKILL)
    exit_code = child.wait(STOP_DEADLINE_S)
    library.release_hold()
    _by_reference(library, scope, BY_REFERENCE)
    after_kill = report_of(_by_reference(library, scope, JUDGED))

    assert exit_code == -signal.SIGKILL
    assert (after_kill.indexed, after_kill.unchanged) == (files, 0)
    assert os.listdir(library.index_dir(JUDGED)) == [STORE_FILE]
    assert _differences(library) == []


def test_the_two_programs_send_the_same_embed_calls(library: LibraryStack) -> None:
    """Both programs ask TEI for the same texts, in the same calls, in the same order.

    A call here is the JSON value of its body. The scenario compares the
    calls of a first run and the calls of an update.
    """
    scope = library.vault()
    write_mixed(scope)

    build = _calls_of_each(library, scope)
    _change(scope)
    update = _calls_of_each(library, scope)

    assert len(build.reference) > len(update.reference) > 1
    assert build.judged == build.reference
    assert update.judged == update.reference
    assert _differences(library) == []


def test_the_two_programs_print_the_same_report_line(library: LibraryStack) -> None:
    """The first line of stdout is equal for a build, for an update and for a new model.

    A person reads that line in the journal of the unit. Each program also
    reports the broken PDF of the corpus, on a line of its own. The
    sentence of that line belongs to the program, so no scenario compares it.
    """
    scope = library.vault()
    write_mixed(scope)
    broken = scope.resolve() / BROKEN_PDF

    build = _run_each(library, scope)
    _change(scope)
    update = _run_each(library, scope)
    tune(library.tree, TEI, TEI_MODEL, NEW_MODEL)
    rebuild = _run_each(library, scope)
    runs = (build, update, rebuild)
    reports = [report_of(done) for run in runs for done in (run.reference, run.judged)]

    assert [_first_line(run.judged) for run in runs] == [_first_line(run.reference) for run in runs]
    assert report_of(rebuild.reference).rebuilt
    assert all(report.has_error_for(broken) for report in reports)
    assert all(len(report.errors) == 1 for report in reports)
    assert _differences(library) == []


def test_the_reference_does_not_start_the_program_of_the_variable(
    library: LibraryStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the variable of the row set, the reference is still the default command.

    The variable names a program here that records each of its starts and
    then becomes the judged command of this run. One run of the reference
    leaves no record. One run of the program leaves one.
    """
    scope = library.vault()
    write_vault(scope)
    record = library.tree.root / "program-starts"
    monkeypatch.setenv(SERVICES[Service.LIBRARY].override, str(_recording_program(library, record)))

    _by_reference(library, scope, BY_REFERENCE)
    after_reference = record.exists()
    _by_program(library, scope, JUDGED)

    assert not after_reference
    assert record.read_text(encoding="utf-8").split() == [STARTED]
    assert _differences(library) == []


def test_the_comparison_sees_a_change_in_each_part(library: LibraryStack) -> None:
    """A copy of a store with one change gives one line for the part that holds the change.

    Each change here is in one part of `StoreContent`, in a copy of one
    store of the program. The comparison names that part and no other
    table. A copy with no change gives no line.

    The next mtime is the nearest float above the mtime of one file, so a
    comparison that rounds a time does not see it. FTS5 reads a word in
    upper case as the same word, so that change moves no rank.

    Two changes move the rows of the three queries. The row that leaves
    `chunks_fts` is a row of each query. Another weight of the rank changes
    each rank and no table.
    """
    scope = library.vault()
    write_mixed(scope)
    _by_program(library, scope, JUDGED)
    content = store_content(library.store(JUDGED).path)
    first = content.files[0]
    listed = next(row for row in content.chunks if (Path(row.path).name, row.ord) == SECOND_CHUNK)
    upper = (listed.text.upper(), listed.id)
    changes: dict[str, tuple[str, Sequence[object]]] = {
        "no change": ("SELECT 1", ()),
        "a row more in meta": ("INSERT INTO meta (key, value) VALUES ('writer', 'another')", ()),
        "another hash": ("UPDATE files SET hash = 'another' WHERE path = ?", (first[0],)),
        "the next mtime": (
            "UPDATE files SET mtime = ? WHERE path = ?",
            (math.nextafter(first[2], math.inf), first[0]),
        ),
        "another text": ("UPDATE chunks SET text = 'another' WHERE id = ?", (listed.id,)),
        "another ord": ("UPDATE chunks SET ord = ord + 1 WHERE id = ?", (listed.id,)),
        "the words in upper case": ("UPDATE chunks_fts SET text = ? WHERE rowid = ?", upper),
        "a row less in chunks_fts": ("DELETE FROM chunks_fts WHERE rowid = ?", (listed.id,)),
        "another vector": (
            "UPDATE chunks_emb SET embedding = ? WHERE id = ?",
            (vector_blob("another"), listed.id),
        ),
        "a row less in chunks_vec": ("DELETE FROM chunks_vec WHERE rowid = ?", (listed.id,)),
        "a column more": ("ALTER TABLE files ADD COLUMN note TEXT", ()),
        "another weight of the rank": (
            "INSERT INTO chunks_fts (chunks_fts, rank) VALUES ('rank', 'bm25(10.0)')",
            (),
        ),
    }

    seen = {
        name: _parts_of(content, library, serial, statement, values)
        for serial, (name, (statement, values)) in enumerate(changes.items())
    }

    assert seen == {
        "no change": [],
        "a row more in meta": ["meta"],
        "another hash": ["files"],
        "the next mtime": ["files"],
        "another text": ["chunks"],
        "another ord": ["chunks"],
        "the words in upper case": ["chunks_fts"],
        "a row less in chunks_fts": ["chunks_fts", *ROWS_OF_EACH_QUERY],
        "another vector": ["chunks_emb"],
        "a row less in chunks_vec": ["chunks_vec"],
        "a column more": ["the table statements"],
        "another weight of the rank": ROWS_OF_EACH_QUERY,
    }


def test_the_comparison_allows_a_rank_inside_the_tolerance() -> None:
    """Two ranks of one row can differ by `RANK_TOLERANCE`, and by no more.

    The order of the rows has no tolerance: two rows in the other order
    give a line, with equal ranks too.
    """
    rows = (Hit(rowid=1, rank=-2.0), Hit(rowid=2, rank=-1.0))
    content = _content_with(rows)
    inside = _content_with((Hit(rowid=1, rank=-2.0 + RANK_TOLERANCE / 2), rows[1]))
    outside = _content_with((Hit(rowid=1, rank=-2.0 + RANK_TOLERANCE * 2), rows[1]))
    turned = _content_with((replace(rows[1], rank=-2.0), replace(rows[0], rank=-1.0)))

    assert content.differences(inside) == []
    assert _parts(content.differences(outside)) == ROWS_OF_EACH_QUERY[:1]
    assert _parts(content.differences(turned)) == ROWS_OF_EACH_QUERY[:1]


def test_the_content_refuses_a_store_that_it_cannot_compare(library: LibraryStack) -> None:
    """`store_content` raises for a time or a vector of another kind, and for an absent table.

    The content holds no time of a run, so no comparison finds a time of
    another kind. The read of the store is the one place that can refuse it.
    """
    scope = library.vault()
    write_vault(scope)
    _by_program(library, scope, JUDGED)
    store = library.store(JUDGED).path
    set_updated = "UPDATE meta SET value = ? WHERE key = ?"
    refused: tuple[tuple[str, Sequence[object], str], ...] = (
        (set_updated, ("soon", META_UPDATED), UPDATED_REFUSED),
        (set_updated, ("1_0.5", META_UPDATED), UPDATED_REFUSED),
        (set_updated, (" 12.5", META_UPDATED), UPDATED_REFUSED),
        (set_updated, ("12", META_UPDATED), UPDATED_REFUSED),
        (set_updated, (b"12.5", META_UPDATED), UPDATED_REFUSED),
        ("DELETE FROM meta WHERE key = ?", (META_UPDATED,), UPDATED_REFUSED),
        ("UPDATE files SET indexed_at = ?", (None,), INDEXED_REFUSED),
        ("UPDATE files SET indexed_at = ?", ("soon",), INDEXED_REFUSED),
        ("UPDATE chunks_emb SET embedding = ?", ("text",), "`chunks_emb` of "),
        ("DROP TABLE chunks_emb", (), "does not read as a store"),
    )

    for serial, (statement, values, text) in enumerate(refused):
        copy = copy_store(store, library.index_dir(f"refused-{serial}"))
        change_store(copy, statement, values)

        with pytest.raises(ProcError, match=text):
            store_content(copy)

    assert store_content(store).chunks != ()


# -------------------------------------------------------------------- helpers


@dataclass(frozen=True, slots=True)
class _Each[T]:
    """What one step gave for each of the two programs."""

    reference: T
    judged: T


def _by_reference(library: LibraryStack, scope: Path, index: str) -> Finished:
    """One run of the reference that it completes: status 0."""
    done = library.run_reference(scope, library.index_dir(index))

    assert done.exit_code == EXIT_OK, done.stderr

    return done


def _by_program(library: LibraryStack, scope: Path, index: str) -> Finished:
    """One run of the program that it completes: status 0."""
    done = library.run_index(scope, library.index_dir(index))

    assert done.exit_code == EXIT_OK, done.stderr

    return done


def _reference_on_both(library: LibraryStack, scope: Path) -> None:
    """One run of the reference for each of the two index directories."""
    _by_reference(library, scope, BY_REFERENCE)
    _by_reference(library, scope, JUDGED)


def _run_each(library: LibraryStack, scope: Path) -> _Each[Finished]:
    """One run of the reference on its index, then one run of the program on the judged index."""
    reference = _by_reference(library, scope, BY_REFERENCE)

    return _Each(reference, _by_program(library, scope, JUDGED))


def _calls_of_each(library: LibraryStack, scope: Path) -> _Each[list[list[str]]]:
    """The embed calls of one run of each program, as the stand-in recorded them."""
    before = len(library.embedded())
    _by_reference(library, scope, BY_REFERENCE)
    between = len(library.embedded())
    _by_program(library, scope, JUDGED)
    calls = library.embedded()

    return _Each(calls[before:between], calls[between:])


def _differences(library: LibraryStack) -> list[str]:
    """How the store of the program differs from the store of the reference. Empty for equal.

    Two stores with no chunk are equal and prove nothing, so that is an error.
    """
    reference = store_content(library.store(BY_REFERENCE).path)
    judged = store_content(library.store(JUDGED).path)

    if not reference.chunks:
        raise ProcError(
            "the store of the reference holds no chunk, so the comparison proves nothing"
        )

    return reference.differences(judged)


def _first_line(done: Finished) -> str:
    """The first line of what one run wrote on its stdout."""
    return done.stdout.split("\n")[0]


def _recording_program(library: LibraryStack, record: Path) -> Path:
    """A program for the variable of the row: it records its start, then runs the judged command.

    The judged command is the one of this run: the default command, or the
    binary that the variable names. So the scenario judges the program that
    the run chose.
    """
    judged = shlex.join(command_of(Service.LIBRARY).words)
    path = library.tree.root / "recording-library"
    path.write_text(
        f'#!/bin/sh\necho {STARTED} >> {shlex.quote(str(record))}\nexec {judged} "$@"\n',
        encoding="utf-8",
    )
    path.chmod(0o755)

    return path


def _parts_of(
    content: StoreContent,
    library: LibraryStack,
    serial: int,
    statement: str,
    values: Sequence[object],
) -> list[str]:
    """The part that each line of the comparison names, for a copy of a store after one change."""
    copy = copy_store(library.store(JUDGED).path, library.index_dir(f"changed-{serial}"))
    change_store(copy, statement, values)

    return _parts(content.differences(store_content(copy)))


def _parts(lines: Sequence[str]) -> list[str]:
    """The start of each line of a comparison: the part that the line names."""
    return [line.partition(": ")[0] for line in lines]


def _content_with(rows: tuple[Hit, ...]) -> StoreContent:
    """The content of a store with no row, but the rows of the first query."""
    rest = tuple(() for _ in CONTENT_QUERIES[1:])

    return StoreContent(
        sql=(),
        meta=(),
        files=(),
        chunks=(),
        words=(),
        hits=(rows, *rest),
        plain_vectors=(),
        vec_vectors=(),
    )


def _change(scope: Path) -> None:
    """Change the corpus of `write_mixed`: one file with other text, one removed, one new.

    The changed file is the full one. Its chunks are in the middle of the
    ids, and a writer needs two embed calls for them.
    """
    _reword(scope / FULL_DOC, PARAGRAPH_WORD, FIRST_CHANGE)
    (scope / BIKES).unlink()
    write_note(scope / ADDED, "A note that the first change adds.")


def _change_again(scope: Path) -> None:
    """Change the corpus a second time, after `_change`.

    The full file changes again. Its chunks have the highest ids of the
    store then, so a writer removes those rows before it adds rows.
    """
    _reword(scope / FULL_DOC, FIRST_CHANGE, SECOND_CHANGE)
    _reword(scope / REST_DOC, PARAGRAPH_WORD, SECOND_CHANGE)
    (scope / ADDED).unlink()
    write_note(scope / ADDED_LATER, "A note that the second change adds.")


def _change_each_file(scope: Path, last: Path) -> None:
    """Give each file of a `write_corpus_of` corpus other text. The last file gets `HELD_TEXT`.

    The held text is the end of the last paragraph of the last file, so the
    stand-in holds the last embed call of the run.
    """
    for path in sorted(scope.iterdir()):
        _reword(path, PARAGRAPH_WORD, FIRST_CHANGE)

    write_note(last, f"{last.read_text(encoding='utf-8')} {HELD_TEXT}")


def _reword(path: Path, old: str, new: str) -> None:
    """Put one word in the place of another in each paragraph of one file."""
    text = path.read_text(encoding="utf-8")

    if old not in text:
        raise ProcError(f"{path} holds no {old!r}, so the change changes nothing")

    write_note(path, text.replace(old, new))
