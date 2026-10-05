"""The index builder as a process: a build, an update, and what the store holds.

Each scenario writes a corpus and runs `index-scope` to its end. Then it
reads what crossed a process boundary: the exit status, the report on
stdout, the store file and the record of the TEI stand-in.

The first ten scenarios have the names of `library/tests/test_library.py`.
That file holds the same rules with the program inside the test process and
an object in the place of TEI. The other scenarios hold what only a process
shows, or what no source line of the program says: the order of the files,
how a text file is read, the files of the index directory during a run.

The rules are the six rules and the store schema of `library/AGENTS.md`.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import standin_tei
from proc_library import (
    BIKES,
    BIKES_TEXT,
    CANARY,
    CODE_INDEX_PREFIX,
    DIMS,
    EXIT_OK,
    FILE_PARAGRAPHS,
    FIRST_WORD,
    INDEX_DEADLINE_S,
    MEETING,
    MEETING_TEXT,
    META_DIMS,
    META_MODEL,
    META_UPDATED,
    NOTES,
    REPO,
    SECOND_WORD,
    STORE_FILE,
    WORK_FILE,
    LibraryStack,
    Profile,
    Report,
    listed,
    report_of,
    vector_blob,
    write_corpus_of,
    write_note,
    write_pdf,
    write_repo,
    write_vault,
)
from proc_standins import TEI, TEI_FAIL_EMBED, TEI_MODEL, tune

#: The index of the code repository of this file, with the name of the host.
CODE_INDEX = f"{CODE_INDEX_PREFIX}{REPO}"

#: The statement of each table that a reader depends on, as SQLite keeps it
#: (`library/AGENTS.md`, "Store schema").
#:
#: CONTRACT-QUESTION: that section also gives `chunks_vec`, as a `vec0`
#: table. Contract 03 §7.3 rule 6 lets a reader take the vectors from
#: `chunks_emb` alone, so no reader depends on the form of `chunks_vec`.
#: Reading taken: the suite holds no form for that table. It holds the
#: statement that reads it. A change costs one entry here.
SCHEMA = {
    "meta": "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)",
    "files": "CREATE TABLE files(path TEXT PRIMARY KEY, hash TEXT, mtime REAL, indexed_at REAL)",
    "chunks": "CREATE TABLE chunks(id INTEGER PRIMARY KEY, path TEXT, ord INTEGER, text TEXT)",
    "chunks_fts": "CREATE VIRTUAL TABLE chunks_fts USING fts5(text)",
    "chunks_emb": "CREATE TABLE chunks_emb(id INTEGER PRIMARY KEY, embedding BLOB NOT NULL)",
}

#: The most characters of one chunk, and how many characters a piece of a
#: long paragraph takes from the piece before it. Both are constants of
#: `library/src/library/library.py`.
CHUNK_CHARS = 1000
OVERLAP_CHARS = 200

#: Six file names in the order of their paths. The order of the path texts
#: is another one: it puts `a/b.md` after `a.md`.
IN_PATH_ORDER = ("B.md", "a/b.md", "a b.md", "a-b/c.md", "a.md", "é.md")

#: Bytes of a text file, and the one chunk that the program must store for
#: them. A text file is UTF-8, each bad sequence becomes U+FFFD, and each
#: line end becomes one line feed. A byte order mark stays. U+001C is white
#: space at the end of a paragraph.
READ_AS = {
    "crlf.md": (b"alpha\r\nbravo\r\n\r\ncharlie", "alpha\nbravo\n\ncharlie"),
    "cr.md": (b"delta\recho\r\rfoxtrot", "delta\necho\n\nfoxtrot"),
    "bad.md": (
        b"bad \xff byte, cut \xe2\x82 sequence, lone \xed\xa0\x80 surrogate",
        "bad � byte, cut � sequence, lone ��� surrogate",
    ),
    "bom.md": (b"\xef\xbb\xbfmarked text", "﻿marked text"),
    "separator.md": (b"first part\x1c\n\nsecond part", "first part\n\nsecond part"),
}

#: A work file that a killed run left. It is no database.
STALE_WORK = b"what a killed run left in its work file"

#: A word of the PDF fixture, and a word that the stand-in is told to refuse.
PDF_WORD = "harvest"
REFUSED_WORD = "quince"

#: A model id that the stand-in gives before and after a change of model.
OLD_MODEL = "old-model"
NEW_MODEL = "new-model"

#: How many chunks the corpus of the batch scenario has in its second file.
REST_CHUNKS = 30


# --------------------------------------- the scenarios of `library/tests`


def test_build_and_query(library: LibraryStack) -> None:
    """A first run builds the store: both notes, by words and by vectors.

    The draft in `review-inbox` and the dot file are in no row (rule 3).
    """
    scope = library.vault()
    write_vault(scope)

    report = _run(library, scope)
    store = library.store()
    chunks = store.chunks()

    assert report.scope == str(scope.resolve())
    assert (report.indexed, report.unchanged, report.removed) == (2, 0, 0)
    assert report.chunks == len(chunks) == 2
    assert report.errors == ()
    assert store.meta()[META_MODEL] == standin_tei.DEFAULT_MODEL
    assert [chunk.path for chunk in chunks] == [_note(scope, BIKES), _note(scope, MEETING)]
    assert store.matches("bicycle") == [chunks[0].id]
    assert set(store.vec_vectors()) == {chunk.id for chunk in chunks}
    assert library.info_calls() == 1
    assert library.embedded() == [[CANARY], [BIKES_TEXT], [MEETING_TEXT]]


def test_incremental_only_reembeds_changed(library: LibraryStack) -> None:
    """Rule 1: the content hash decides. A file with the same bytes costs no embed call."""
    scope = library.vault()
    write_vault(scope)
    _run(library, scope)
    built = library.store().files()
    first_calls = len(library.embedded())

    again = _run(library, scope)
    calls_again = library.embedded()[first_calls:]
    moved = "The meeting moved to Thursday."
    write_note(scope / MEETING, moved)
    changed = _run(library, scope)
    calls_changed = library.embedded()[first_calls + len(calls_again) :]
    after = library.store().files()

    assert (again.indexed, again.unchanged) == (0, 2)
    assert calls_again == [[CANARY]]
    assert (changed.indexed, changed.unchanged) == (1, 1)
    assert calls_changed == [[CANARY], [moved]]
    assert after[_note(scope, BIKES)] == built[_note(scope, BIKES)]
    assert after[_note(scope, MEETING)].hash != built[_note(scope, MEETING)].hash
    assert library.store().texts_of(scope.resolve() / MEETING) == [moved]


def test_deletion_removes_rows(library: LibraryStack) -> None:
    scope = library.vault()
    write_vault(scope)
    _run(library, scope)
    (scope / BIKES).unlink()

    report = _run(library, scope)
    store = library.store()

    assert (report.removed, report.unchanged) == (1, 1)
    assert list(store.files()) == [_note(scope, MEETING)]
    assert {chunk.path for chunk in store.chunks()} == {_note(scope, MEETING)}
    assert store.matches("bicycle") == []


def test_bad_file_isolated(library: LibraryStack) -> None:
    """Rule 4: one bad file never ends the run. The report names it, and the status is 0."""
    scope = library.vault()
    write_vault(scope)
    (scope / "broken.pdf").write_bytes(b"not a real pdf")

    report = _run(library, scope)

    assert report.indexed == 2
    assert len(report.errors) == 1
    assert report.has_error_for(scope.resolve() / "broken.pdf")
    assert set(library.store().files()) == {_note(scope, BIKES), _note(scope, MEETING)}


def test_model_change_forces_full_rebuild(library: LibraryStack) -> None:
    """A store of another model is in another vector space. The program builds it again."""
    scope = library.vault()
    write_vault(scope)
    tune(library.tree, TEI, TEI_MODEL, OLD_MODEL)
    first = _run(library, scope)
    first_calls = len(library.embedded())
    tune(library.tree, TEI, TEI_MODEL, NEW_MODEL)

    second = _run(library, scope)

    assert not first.rebuilt
    assert second.rebuilt
    assert (second.indexed, second.unchanged, second.removed) == (2, 0, 0)
    assert library.embedded()[first_calls:] == [[CANARY], [BIKES_TEXT], [MEETING_TEXT]]
    assert library.store().meta()[META_MODEL] == NEW_MODEL


def test_plain_vectors_mirror_every_chunk(library: LibraryStack) -> None:
    """Rule 6: `chunks_emb` holds the vector of each chunk as float32, little-endian.

    The bridge reads the vectors from that table alone. The expected blob is
    the answer of the stand-in for the text of the chunk.
    """
    scope = library.vault()
    write_vault(scope)
    _run(library, scope)

    chunks = library.store().chunks()
    vectors = library.store().plain_vectors()

    assert chunks != []
    assert vectors == {chunk.id: vector_blob(chunk.text) for chunk in chunks}


def test_deletion_removes_plain_vectors(library: LibraryStack) -> None:
    scope = library.vault()
    write_vault(scope)
    _run(library, scope)
    (scope / BIKES).unlink()
    _run(library, scope)

    chunks = library.store().chunks()

    assert len(chunks) == 1
    assert set(library.store().plain_vectors()) == {chunk.id for chunk in chunks}


def test_store_from_before_plain_vectors_is_backfilled_without_reembedding(
    library: LibraryStack,
) -> None:
    """A store with no `chunks_emb` gets the table from the vectors that it has."""
    scope = library.vault()
    write_vault(scope)
    _run(library, scope)
    first_calls = len(library.embedded())
    library.store().drop_plain_vectors()

    report = _run(library, scope)
    plain = library.store().plain_vectors()

    assert (report.indexed, report.unchanged) == (0, 2)
    assert library.embedded()[first_calls:] == [[CANARY]]
    assert plain == {chunk.id: vector_blob(chunk.text) for chunk in library.store().chunks()}
    assert plain == library.store().vec_vectors()
    assert len(plain) == 2


def test_code_profile_indexes_source_and_skips_build_output(library: LibraryStack) -> None:
    """The unit of a code repository gives the word `code`, and an index named `code-<repo>`."""
    repo = library.code()
    write_repo(repo)

    report = _run(library, repo, CODE_INDEX, Profile.CODE)

    assert report.errors == ()
    assert report.indexed == 4
    assert _names(library, repo, CODE_INDEX) == {
        "README.md",
        "pyproject.toml",
        "src/app.py",
        "src/client.ts",
    }


def test_vault_profile_ignores_source_files(library: LibraryStack) -> None:
    """This scenario gives the word `vault`. Each other vault scenario gives no profile word."""
    scope = library.vault()
    write_vault(scope)
    write_note(scope / "script.py", "print('not prose')\n")

    report = _run(library, scope, profile=Profile.VAULT)

    assert report.indexed == 2
    assert _names(library, scope) == {BIKES, MEETING}


# ------------------------------------------------------ what a process shows


def test_the_chunk_ids_follow_the_path_order(library: LibraryStack) -> None:
    """The program reads the files in the order of their paths, part by part.

    A file system can keep the name with the accent in another Unicode form
    than the form of this file. So each expected path has the name that the
    directory listing gives.
    """
    scope = library.vault()

    for name in reversed(IN_PATH_ORDER):
        write_note(scope / name, f"The note with the name {name}.")

    _run(library, scope)
    chunks = library.store().chunks()

    assert [chunk.path for chunk in chunks] == [
        str(listed(scope.resolve(), name)) for name in IN_PATH_ORDER
    ]
    assert library.embedded()[1:] == [[f"The note with the name {name}."] for name in IN_PATH_ORDER]


def test_a_text_file_is_read_as_utf8_with_one_newline_form(library: LibraryStack) -> None:
    scope = library.vault()
    scope.mkdir()

    for name, (raw, _) in READ_AS.items():
        (scope / name).write_bytes(raw)

    report = _run(library, scope)
    store = library.store()

    assert report.errors == ()
    assert {name: store.texts_of(scope.resolve() / name) for name in READ_AS} == {
        name: [text] for name, (_, text) in READ_AS.items()
    }


def test_a_long_paragraph_is_cut_with_overlap(library: LibraryStack) -> None:
    """A paragraph over the chunk size is cut into pieces of the chunk size.

    Each piece starts with the last characters of the piece before it. The
    sizes are counts of characters: each fifth character of this text takes
    two bytes.
    """
    scope = library.vault()
    text = "".join(f"{index:04d}é" for index in range(500))
    step = CHUNK_CHARS - OVERLAP_CHARS
    pieces = [text[:CHUNK_CHARS], text[step : step + CHUNK_CHARS], text[2 * step :]]
    write_note(scope / "long.md", text)

    report = _run(library, scope)

    assert report.chunks == len(pieces)
    assert library.store().texts_of(scope.resolve() / "long.md") == pieces
    assert library.embedded()[1:] == [pieces]


def test_an_empty_file_has_a_files_row_and_no_chunk(library: LibraryStack) -> None:
    """The program asks TEI for no vector of a file with no text."""
    scope = library.vault()
    write_note(scope / "full.md", "One note with text.")
    (scope / "empty.md").write_bytes(b"")

    report = _run(library, scope)
    store = library.store()

    assert (report.indexed, report.chunks) == (2, 1)
    assert set(store.files()) == {_note(scope, "empty.md"), _note(scope, "full.md")}
    assert [chunk.path for chunk in store.chunks()] == [_note(scope, "full.md")]
    assert library.embedded() == [[CANARY], ["One note with text."]]


def test_the_store_has_the_schema_of_the_contract(library: LibraryStack) -> None:
    """`library/AGENTS.md`, "Store schema": the schema is the contract for each reader.

    Contract 03 §7.3 rule 6 lets a reader take the vectors from `chunks_emb`
    alone. So this scenario holds no form for `chunks_vec`. It holds that the
    statement of rule 6 gives one row for each chunk.
    """
    scope = library.vault()
    write_vault(scope)
    started = time.time()

    _run(library, scope)
    ended = time.time()
    store = library.store()
    meta = store.meta()

    assert {name: store.sql_of(name) for name in SCHEMA} == SCHEMA
    assert set(meta) == {META_MODEL, META_DIMS, META_UPDATED}
    assert meta[META_DIMS] == str(DIMS)
    assert started <= float(meta[META_UPDATED]) <= ended
    assert set(store.vec_vectors()) == {chunk.id for chunk in store.chunks()}


def test_the_index_directory_holds_only_the_store(library: LibraryStack) -> None:
    """A run that ends leaves one file: after the first build, and after an update."""
    scope = library.vault()
    write_vault(scope)

    _run(library, scope)
    after_build = os.listdir(library.index_dir())
    write_note(scope / MEETING, "The meeting moved to Thursday.")
    _run(library, scope)
    after_update = os.listdir(library.index_dir())

    assert after_build == [STORE_FILE]
    assert after_update == [STORE_FILE]


def test_the_store_changes_only_at_the_publish(library: LibraryStack) -> None:
    """Rule 5: a reader finds the old store or the new store, and never a part of a run.

    The program has the vectors of one changed note when the stand-in holds
    the call for the second one. The store file has its old bytes then. The
    end of the run gives the name to another file, so a reader with the old
    file open keeps the old store.
    """
    scope = library.vault()
    write_vault(scope)
    _run(library, scope)
    store = library.store()
    built = store.path.read_bytes()
    built_file = store.path.stat().st_ino

    child = library.start_held_update(scope, library.index_dir())
    held = store.path.read_bytes()
    library.release_hold()
    exit_code = child.wait(INDEX_DEADLINE_S)

    assert held == built
    assert exit_code == EXIT_OK
    assert store.matches(FIRST_WORD) != []
    assert store.matches(SECOND_WORD) != []
    assert store.path.stat().st_ino != built_file


def test_a_stale_work_file_is_replaced(library: LibraryStack) -> None:
    """A killed run leaves its work file. The next run does not read that file.

    The first run here has no store. The second one has a store.
    """
    scope = library.vault()
    write_vault(scope)
    work = library.index_dir() / WORK_FILE
    work.parent.mkdir(parents=True)
    work.write_bytes(STALE_WORK)

    first = _run(library, scope)
    work.write_bytes(STALE_WORK)
    write_note(scope / MEETING, f"The {FIRST_WORD} has a new pier.")
    second = _run(library, scope)

    assert first.indexed == 2
    assert (second.indexed, second.unchanged) == (1, 1)
    assert library.store().matches(FIRST_WORD) != []
    assert os.listdir(library.index_dir()) == [STORE_FILE]


def test_a_pdf_with_text_is_searchable(library: LibraryStack) -> None:
    """A vault holds PDF files. A word of a page is a word of the index.

    No scenario compares the whole text of a page: two PDF readers can give
    the text of one page with other spaces.
    """
    scope = library.vault()
    write_pdf(scope / "paper.pdf", f"The quarterly {PDF_WORD} report is ready.")

    report = _run(library, scope)

    assert report.errors == ()
    assert report.indexed == 1
    assert library.store().matches(PDF_WORD) != []


def test_one_embed_call_holds_32_texts_at_most(library: LibraryStack) -> None:
    """Rule 2: a call stays under the limit of TEI. Each file starts a call of its own."""
    scope = library.vault()
    write_corpus_of(scope, FILE_PARAGRAPHS + REST_CHUNKS)

    report = _run(library, scope)
    sizes = [len(texts) for texts in library.embedded()]

    assert max(sizes) <= standin_tei.MAX_BATCH
    assert sizes == [
        1,
        standin_tei.MAX_BATCH,
        FILE_PARAGRAPHS - standin_tei.MAX_BATCH,
        REST_CHUNKS,
    ]
    assert report.chunks == len(library.store().chunks()) == FILE_PARAGRAPHS + REST_CHUNKS


def test_a_file_that_tei_refuses_is_one_error(library: LibraryStack) -> None:
    """Rule 4, for a failed embed call: the run goes on, and the file gets no row."""
    scope = library.vault()
    write_vault(scope)
    _run(library, scope)
    write_note(scope / "refused.md", f"A note about one {REFUSED_WORD}.")
    tune(library.tree, TEI, TEI_FAIL_EMBED, REFUSED_WORD)

    report = _run(library, scope)
    store = library.store()

    assert (report.indexed, report.unchanged) == (0, 2)
    assert len(report.errors) == 1
    assert report.has_error_for(scope.resolve() / "refused.md")
    assert set(store.files()) == {_note(scope, BIKES), _note(scope, MEETING)}
    assert {chunk.path for chunk in store.chunks()} == {_note(scope, BIKES), _note(scope, MEETING)}


def test_a_symlink_to_a_directory_is_not_followed(library: LibraryStack) -> None:
    """A link in a corpus must not bring a directory from outside into the index."""
    scope = library.vault()
    outside = library.vault("elsewhere")
    write_note(scope / "kept.md", "A note of the scope.")
    write_note(outside / "outside.md", "A note of another directory.")
    (scope / "linked").symlink_to(outside, target_is_directory=True)

    report = _run(library, scope)

    assert report.indexed == 1
    assert set(library.store().files()) == {_note(scope, "kept.md")}


def test_the_stored_paths_are_resolved(library: LibraryStack) -> None:
    """A corpus that the command names through a link is stored under its real directory."""
    real = library.vault("real")
    alias = library.vault("alias")
    write_vault(real)
    alias.symlink_to(real, target_is_directory=True)

    report = _run(library, alias, "alias")
    store = library.store("alias")
    notes = {_note(real, BIKES), _note(real, MEETING)}

    assert report.scope == str(real.resolve())
    assert set(store.files()) == notes
    assert {chunk.path for chunk in store.chunks()} == notes


# -------------------------------------------------------------------- helpers


def _run(
    library: LibraryStack, scope: Path, index: str = NOTES, profile: Profile | None = None
) -> Report:
    """One run that the program completes: status 0 and a report."""
    done = library.run_index(scope, library.index_dir(index), profile)

    assert done.exit_code == EXIT_OK, done.stderr

    return report_of(done)


def _note(scope: Path, name: str) -> str:
    """The path that the store holds for one file of a corpus."""
    return str(scope.resolve() / name)


def _names(library: LibraryStack, scope: Path, index: str = NOTES) -> set[str]:
    """Each file of the store, as a path under its corpus."""
    root = scope.resolve()

    return {Path(path).relative_to(root).as_posix() for path in library.store(index).files()}
