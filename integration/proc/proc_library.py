"""The seventh topology: the index builder alone, beside the TEI stand-in.

    a test (plays a systemd timer)
      | runs `index-scope <corpus> <index directory> [vault|code]` to its end
      v
    library        one process for each run, started through `Service.LIBRARY`
      | reads <root>/vault/<name> or <root>/code/<name>
      | HTTP, loopback port: GET /info, POST /embed      the TEI stand-in
      | writes <root>/state/index/<name>/store.db, by one rename
      v
    a test reads store.db with the `sqlite3` module

No `attendance` runs here, and no sandbox. On the host the two index units
run the program inside a sandbox, through `sbx exec` and `sh -c`. The image
of that sandbox gives the program a `PATH` and the address of TEI. So a test
gives the program `PATH` and one address variable. `env_of` of
`proc_services.py` adds what only the default command needs.

The class is not on `Stack` of `proc_stack.py`, because that class starts
`attendance` and writes the tree of a family. The index builder reads
neither.

A test writes a corpus, runs the program, and reads four things back: the
exit status, the report on stdout, the store file, and the record of the
TEI stand-in.

The store schema is the one of `library/AGENTS.md`, section "Store schema".
A reader here opens the store read-only, so no reader changes a byte of it.

Two programs can write one store: the judged command and the reference,
which is the default command of the row. A run of the first one must leave
a store that the second one updates, and the reverse. `run_reference` runs
the reference, and `store_content` gives what one store holds, so a test
compares the stores of two index directories for one corpus.

A third program reads a store: the bridge of the playpen, with the SQLite
of Node. `read_as_bridge` runs `reader_store.mjs`, which holds the
statements of the bridge.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import sqlite3
import stat
import struct
import subprocess
import unicodedata
from collections.abc import Generator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, cast
from urllib.parse import quote

import sqlite_vec
import standin_tei
from proc_caregiver import wait_until
from proc_harness import LOOPBACK, Child, Finished, ProcError, Supervisor
from proc_services import Service, StartCommand, command_of, env_of, reference_of
from proc_standins import TEI, TEI_HOLD_EMBED, start_tei, tei_calls, tune, untune
from proc_tree import Tree

#: The two variables that give the program the address of TEI. The first one
#: is a whole URL. The second one is an address, and the program adds the
#: port of TEI to it.
TEI_URL_ENV: Final = "TEI_URL"
LAN_ADDRESS_ENV: Final = "AGENT_LAN_ADDRESS"

#: The port of TEI on the LAN address.
TEI_PORT: Final = 8085

#: The file that a reader opens in an index directory.
STORE_FILE: Final = "store.db"

#: The file that the program updates during a run (`library/AGENTS.md`, rule 5).
WORK_FILE: Final = "store.work.db"

#: The count of values in each stored vector (`library/AGENTS.md`, "Store
#: schema"). The default model of the stand-in gives that count.
DIMS: Final = standin_tei.DEFAULT_DIMS

#: The three keys of the table `meta`.
META_MODEL: Final = "embed_model"
META_DIMS: Final = "dims"
META_UPDATED: Final = "updated_at"

#: What the program records for a TEI that names no model.
UNKNOWN_MODEL: Final = "unknown"

#: The one text of the first embed call of each run. `library/AGENTS.md`,
#: "Run it": the program embeds a canary before it reads the corpus.
CANARY: Final = "canary"

#: The exit status of a run that the program completed, and of a command
#: line that it refused.
EXIT_OK: Final = 0
EXIT_USAGE: Final = 2

#: The vault scope and the code repository of a scenario with one corpus.
NOTES: Final = "notes"
REPO: Final = "demo"

#: Each directory of build output that `write_repo` makes: each name that
#: the `code` profile of `library/AGENTS.md` skips and that starts with no dot.
BUILD_OUTPUT: Final = ("node_modules", "target", "dist", "build", "__pycache__")

#: The index of a code repository has this prefix on the host.
CODE_INDEX_PREFIX: Final = "code-"

#: How long one run may take. A run of a scenario takes about one second on
#: a laptop. A loaded machine is many times slower.
INDEX_DEADLINE_S: Final = 120.0

#: The two notes of `write_vault`, in the order of their paths, with the
#: text of each one.
BIKES: Final = "bikes.md"
MEETING: Final = "meeting.md"
BIKES_TEXT: Final = "Bicycle storage rules were updated in March."
MEETING_TEXT: Final = "The meeting is on Tuesday in the lobby.\n\nBring your badge."

#: Another text for the second note, for a scenario that changes one file.
MOVED_TEXT: Final = "The meeting moved to Thursday."

#: What `start_held_update` writes. Each note gets one word that no other
#: text of a scenario holds. The second note also gets the text that makes
#: the stand-in hold its embed call.
FIRST_WORD: Final = "harbour"
SECOND_WORD: Final = "lantern"
HELD_TEXT: Final = "hold this call"

#: How many characters one paragraph of `write_corpus_of` has. The program
#: starts a new chunk when a paragraph would take a chunk over 1000
#: characters. Two such paragraphs are over that size, so each paragraph
#: gives one chunk.
PARAGRAPH_CHARS: Final = 600

#: How many paragraphs one file of `write_corpus_of` has. More than one embed
#: call can hold, so a full file needs two calls.
FILE_PARAGRAPHS: Final = 40

_PARAGRAPH_GAP: Final = "\n\n"

#: The first line of the report, as the program writes it today.
#:
#: CONTRACT-QUESTION: no contract gives the text of the report. Reading
#: taken: the line of `library/src/library/library.py`, whole. A person
#: reads it in the journal of the unit, and a port must write the same line.
#: A change costs this one pattern.
_REPORT_LINE: Final = re.compile(
    r"scope (?P<scope>.+): (?P<indexed>[0-9]+) indexed, (?P<unchanged>[0-9]+) unchanged, "
    r"(?P<removed>[0-9]+) removed, (?P<chunks>[0-9]+) chunks"
    r"(?P<rebuilt> \[FULL REBUILD: embed model changed\])?"
)

#: The start of a report line that names a file with an error.
_ERROR_START: Final = "  ERROR "

#: What a text of `write_pdf` may hold: no character that a PDF string escapes.
_PDF_TEXT: Final = re.compile(r"[A-Za-z0-9 .]+")

#: The name of a run of the reference in the report of a failed test.
REFERENCE: Final = "library-reference"

#: The program that reads a store as the bridge reads it, and its name in
#: the report of a failed test.
READER_SCRIPT: Final = "reader_store.mjs"
READER: Final = "store-reader"

#: How long the question to `node` about its SQLite module may take.
_NODE_PROBE_S: Final = 30.0

#: The mode bits that let a program write a file, or make a file in a directory.
_WRITE_BITS: Final = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH

#: The tables whose statement `store_content` holds. `chunks_vec` is not one
#: of them: contract 03 §7.3 rule 6 forbids a reader to use that table, so
#: no reader depends on its form.
CONTENT_TABLES: Final = ("meta", "files", "chunks", "chunks_fts", "chunks_emb")

#: The three queries of `store_content`, in the form that the bridge gives
#: FTS5: each word in quotes, and `OR` between two words. The first one
#: holds a word of each paragraph of `write_corpus_of`. The second one holds
#: two numbers that some of those paragraphs hold one time and some two
#: times. The third one adds a word of each note of `write_vault`.
CONTENT_QUERIES: Final = ('"paragraph"', '"0001" OR "0002"', '"doc" OR "meeting" OR "bicycle"')

#: How far the rank of one row may differ between two stores. One reader
#: computes both ranks, from counts that each writer keeps in its FTS5
#: tables. The order of the rows has no tolerance.
RANK_TOLERANCE: Final = 1e-9

#: How many characters of one row a line of `StoreContent.differences` shows.
_SHOWN_CHARS: Final = 160

#: How many chunks the two files of `write_corpus_of` in `write_mixed` give.
#: The first file is full, so it needs two embed calls.
MIXED_PARAGRAPHS: Final = FILE_PARAGRAPHS + 5

#: The file of `write_mixed` that no program can read: one error line of
#: the report, and no row of the store.
BROKEN_PDF: Final = "broken.pdf"

#: The link of `write_mixed` to a file outside the scope, the link to a
#: directory outside the scope, and the directory that holds both targets.
LINKED_NOTE: Final = "linked.md"
LINKED_DIR: Final = "linked"
_OUTSIDE_SUFFIX: Final = "-outside"

#: The files of `write_mixed` beside those of the other writers, as bytes.
#: The first six names are in the order of their paths, which is not the
#: order of their texts. Then: two forms of a line end, a byte order mark
#: with a bad UTF-8 sequence, the second suffix of the `vault` profile, a
#: suffix in upper case, a file with no text, and the broken PDF.
_MIXED_FILES: Final[dict[str, bytes]] = {
    "B.md": b"A note whose name starts in upper case.",
    "a/b.md": b"A note in a directory.",
    "a b.md": b"A note with a space in its name.",
    "a-b/c.md": b"A note in a directory with a hyphen.",
    "a.md": b"A note with a short name.",
    "é.md": "A note with an accent in its name, and one in its téxt.".encode(),
    "crlf.md": b"alpha\r\nbravo\r\n\r\ncharlie\rdelta\r\rfoxtrot",
    "bad.md": b"\xef\xbb\xbfmarked text, bad \xff byte, cut \xe2\x82 sequence\x1c\n\nsecond part",
    "plain.txt": b"A note with the suffix of a text file.",
    "UPPER.MD": b"A note with its suffix in upper case.",
    "empty.md": b"",
    BROKEN_PDF: b"not a real pdf",
}

#: The one paragraph of `long.md` in `write_mixed`: 2500 characters, so the
#: program cuts it into pieces. Each fifth character takes two bytes.
_LONG_PARAGRAPH: Final = "".join(f"{index:04d}é" for index in range(500))


class Profile(StrEnum):
    """The third word of the command: which files of a corpus the program reads."""

    VAULT = "vault"
    CODE = "code"


@dataclass(frozen=True, slots=True)
class FileRow:
    """One row of the table `files`."""

    path: str
    hash: str
    mtime: float
    indexed_at: float


@dataclass(frozen=True, slots=True)
class ChunkRow:
    """One row of the table `chunks`."""

    id: int
    path: str
    ord: int
    text: str


@dataclass(frozen=True, slots=True)
class Report:
    """What one run wrote on its stdout."""

    #: The corpus directory that the first line names.
    scope: str
    indexed: int
    unchanged: int
    removed: int
    chunks: int
    #: Whether the first line says that the program built the store again.
    rebuilt: bool
    #: Each line that names a file with an error, without its start.
    errors: tuple[str, ...]

    def has_error_for(self, path: Path) -> bool:
        """Whether one error line names this file."""
        return any(line.startswith(f"{path}: ") for line in self.errors)


@dataclass(frozen=True, slots=True)
class Store:
    """One store file, read as the next run and the bridge read it."""

    path: Path

    def meta(self) -> dict[str, str]:
        """Each row of `meta`, by its key."""
        with _reader(self.path) as conn:
            return dict(conn.execute("SELECT key, value FROM meta").fetchall())

    def files(self) -> dict[str, FileRow]:
        """Each row of `files`, by its path."""
        with _reader(self.path) as conn:
            rows = conn.execute("SELECT path, hash, mtime, indexed_at FROM files").fetchall()

        return {row[0]: FileRow(*row) for row in rows}

    def chunks(self) -> list[ChunkRow]:
        """Each row of `chunks`, in the order of the ids."""
        with _reader(self.path) as conn:
            rows = conn.execute("SELECT id, path, ord, text FROM chunks ORDER BY id").fetchall()

        return [ChunkRow(*row) for row in rows]

    def texts_of(self, path: Path) -> list[str]:
        """The chunks of one file, in the order of `ord`."""
        rows = sorted((row.ord, row.text) for row in self.chunks() if row.path == str(path))

        return [text for _, text in rows]

    def plain_vectors(self) -> dict[int, bytes]:
        """Each row of `chunks_emb`: the blob of one vector, by the id of its chunk."""
        with _reader(self.path) as conn:
            rows = conn.execute("SELECT id, embedding FROM chunks_emb").fetchall()

        return {chunk_id: bytes(blob) for chunk_id, blob in rows}

    def vec_vectors(self) -> dict[int, bytes]:
        """Each row of `chunks_vec`, by the id of its chunk.

        The connection has `sqlite-vec`, so the statement reads a `vec0`
        table and a plain table.
        """
        with _reader(self.path, Vec.LOADED) as conn:
            rows = conn.execute("SELECT rowid, embedding FROM chunks_vec").fetchall()

        return {chunk_id: bytes(blob) for chunk_id, blob in rows}

    def matches(self, word: str) -> list[int]:
        """The id of each chunk whose text holds one word, by FTS5."""
        with _reader(self.path) as conn:
            rows = conn.execute(
                "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY rowid",
                (f'"{word}"',),
            ).fetchall()

        return [row[0] for row in rows]

    def sql_of(self, name: str) -> str | None:
        """The statement that made one table, as SQLite keeps it. None for no such table."""
        with _reader(self.path) as conn:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
            ).fetchone()

        return None if row is None else row[0]

    def drop_plain_vectors(self) -> None:
        """Make this store a store from before `chunks_emb`: remove that table."""
        conn = sqlite3.connect(self.path)

        try:
            conn.execute("DROP TABLE chunks_emb")
            conn.commit()
        finally:
            conn.close()


class Vec(StrEnum):
    """Whether a reader connection has the `sqlite-vec` extension."""

    ABSENT = "absent"
    LOADED = "loaded"


@contextlib.contextmanager
def _reader(path: Path, vec: Vec = Vec.ABSENT) -> Generator[sqlite3.Connection]:
    """A read-only connection to one store. It never changes the file."""
    conn = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True)

    try:
        if vec is Vec.LOADED:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)

        yield conn
    finally:
        conn.close()


@contextlib.contextmanager
def _read_only(store: Path) -> Generator[None]:
    """Take the write permission from one store and from its directory, for one reader.

    The bridge reads an index over a mount that it cannot write. SQLite can
    then make no file beside the store. A store that needs such a file
    reads well in a directory of a test and fails on a host. A store in
    WAL mode is one: its reader makes two files. The permission comes back
    when the reader ends, so the teardown can remove the root.
    """
    modes = [(path, stat.S_IMODE(path.stat().st_mode)) for path in (store, store.parent)]

    try:
        for path, mode in modes:
            path.chmod(mode & ~_WRITE_BITS)

        yield
    finally:
        for path, mode in modes:
            path.chmod(mode)


@dataclass(frozen=True, slots=True)
class Hit:
    """One row that a query of `CONTENT_QUERIES` gives: a chunk and its rank."""

    rowid: int
    rank: float


@dataclass(frozen=True, slots=True)
class StoreContent:
    """What one store holds for a reader and for the next run of a writer.

    It holds no time of a run: not `updated_at` of `meta`, and not the
    column `indexed_at` of `files`. Two writers that read one corpus then
    give equal content. Each list is in the order of its key, because two
    SQLite versions can scan one table in another order.

    It holds no statement of `chunks_vec`. It holds the rows that one
    `SELECT` gives for that table on a connection with `sqlite-vec`.
    """

    #: The statement of each table of `CONTENT_TABLES`, as SQLite keeps it.
    #: None for a table that the store does not have.
    sql: tuple[tuple[str, str | None], ...]
    #: Each row of `meta` but `updated_at`: the key and the value.
    meta: tuple[tuple[str, str], ...]
    #: Each row of `files`: the path, the hash and the mtime, not rounded.
    files: tuple[tuple[str, str, float], ...]
    #: Each row of `chunks`.
    chunks: tuple[ChunkRow, ...]
    #: Each row of `chunks_fts`: the rowid and the text.
    words: tuple[tuple[int, str], ...]
    #: The rows of each query of `CONTENT_QUERIES`, the best rank first.
    hits: tuple[tuple[Hit, ...], ...]
    #: Each row of `chunks_emb`: the id and the blob.
    plain_vectors: tuple[tuple[int, bytes], ...]
    #: Each row of `chunks_vec`: the rowid and the blob.
    vec_vectors: tuple[tuple[int, bytes], ...]

    def differences(self, other: StoreContent) -> list[str]:
        """One line for each part that `other` holds in another way. Empty for equal content.

        Each part but the ranks must be equal. Two ranks of one row may
        differ by `RANK_TOLERANCE`.
        """
        parts: tuple[tuple[str, Sequence[object], Sequence[object]], ...] = (
            ("the table statements", self.sql, other.sql),
            ("meta", self.meta, other.meta),
            ("files", self.files, other.files),
            ("chunks", self.chunks, other.chunks),
            ("chunks_fts", self.words, other.words),
            ("chunks_emb", self.plain_vectors, other.plain_vectors),
            ("chunks_vec", self.vec_vectors, other.vec_vectors),
        )
        found = [_difference(name, here, there) for name, here, there in parts]
        ranked = zip(CONTENT_QUERIES, self.hits, other.hits, strict=True)
        found.extend(_rank_difference(query, here, there) for query, here, there in ranked)

        return [line for line in found if line is not None]


def store_content(path: Path) -> StoreContent:
    """What one store file holds. One read-only connection reads each part.

    A store with no `updated_at` that reads as a count of seconds is an
    error. So is a row of `files` whose `indexed_at` is no such count, and a
    vector that is no blob. The content holds none of the two times, so
    this function is the one place that looks at them.
    """
    with _reader(path, Vec.LOADED) as conn:
        made = dict(conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'"))
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        files = conn.execute(
            "SELECT path, hash, mtime, indexed_at FROM files ORDER BY path"
        ).fetchall()
        chunks = conn.execute("SELECT id, path, ord, text FROM chunks ORDER BY id").fetchall()
        words = conn.execute("SELECT rowid, text FROM chunks_fts ORDER BY rowid").fetchall()
        plain = conn.execute("SELECT id, embedding FROM chunks_emb ORDER BY id").fetchall()
        vec = conn.execute("SELECT rowid, embedding FROM chunks_vec").fetchall()
        hits = tuple(_hits_of(conn, query) for query in CONTENT_QUERIES)

    _need_seconds(meta.pop(META_UPDATED, None), f"`{META_UPDATED}` of `meta` in {path}")

    for row in files:
        _need_seconds(row[3], f"`indexed_at` of {row[0]} in {path}")

    return StoreContent(
        sql=tuple((name, made.get(name)) for name in CONTENT_TABLES),
        meta=tuple(sorted(meta.items())),
        files=tuple((row[0], row[1], row[2]) for row in files),
        chunks=tuple(ChunkRow(*row) for row in chunks),
        words=tuple((row[0], row[1]) for row in words),
        hits=hits,
        plain_vectors=_blobs(plain, f"`chunks_emb` of {path}"),
        vec_vectors=_blobs(sorted(vec), f"`chunks_vec` of {path}"),
    )


def _hits_of(conn: sqlite3.Connection, query: str) -> tuple[Hit, ...]:
    """The rows of one FTS5 query, the best rank first. The rowid orders two equal ranks."""
    rows = conn.execute(
        "SELECT rowid, rank FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY rank, rowid",
        (query,),
    ).fetchall()

    return tuple(Hit(*row) for row in rows)


def _need_seconds(value: object, what: str) -> None:
    """Refuse a time of a store that does not read as a count of seconds."""
    try:
        seconds = float(cast("float | str", value))
    except (TypeError, ValueError):
        seconds = math.nan

    if not math.isfinite(seconds):
        raise ProcError(f"{what} is {value!r}, which is no count of seconds")


def _blobs(rows: Sequence[tuple[int, object]], what: str) -> tuple[tuple[int, bytes], ...]:
    """The rows of one vector table. A vector that is no blob is an error."""
    blobs: list[tuple[int, bytes]] = []

    for key, value in rows:
        if not isinstance(value, bytes):
            raise ProcError(f"row {key} of {what} holds {_shown(value)}, which is no blob")

        blobs.append((key, value))

    return tuple(blobs)


def _difference(name: str, here: Sequence[object], there: Sequence[object]) -> str | None:
    """One line that says where two lists of rows differ, or None for equal lists."""
    if here == there:
        return None

    if len(here) != len(there):
        return f"{name}: {len(here)} rows here, {len(there)} rows there"

    pairs = enumerate(zip(here, there, strict=True))
    at = next(index for index, (mine, theirs) in pairs if mine != theirs)

    return f"{name}: row {at} is {_shown(here[at])} here and {_shown(there[at])} there"


def _rank_difference(query: str, here: Sequence[Hit], there: Sequence[Hit]) -> str | None:
    """One line for a query that two stores answer in another way, or None.

    The order of the rowids must be equal. The rank of each row must be
    equal inside `RANK_TOLERANCE`.
    """
    name = f"the rows of MATCH {query}"
    order = _difference(name, [hit.rowid for hit in here], [hit.rowid for hit in there])

    if order is not None:
        return order

    for mine, theirs in zip(here, there, strict=True):
        if not math.isclose(mine.rank, theirs.rank, rel_tol=0.0, abs_tol=RANK_TOLERANCE):
            return f"{name}: row {mine.rowid} has rank {mine.rank!r} here and {theirs.rank!r} there"

    return None


def _shown(row: object) -> str:
    """One row for a line of a report, cut to a length that a person can read."""
    text = repr(row)

    if len(text) <= _SHOWN_CHARS:
        return text

    return f"{text[:_SHOWN_CHARS]}... ({len(text)} characters)"


@dataclass(slots=True)
class LibraryStack:
    """The TEI stand-in and the directories of one test. A test runs the program itself."""

    tree: Tree
    supervisor: Supervisor
    tei: Child | None = None
    tei_port: int = 0

    def prepare(self) -> None:
        """Make the two corpus roots. Start nothing.

        The program makes its index directory itself, so no directory under
        `state/index` exists before the first run.
        """
        for directory in (self.vault_root, self.code_root):
            directory.mkdir(parents=True, exist_ok=True)

    def start_tei(self) -> None:
        """Start the TEI stand-in, and wait for it."""
        self.tei, self.tei_port = start_tei(self.tree, self.supervisor, _path_env())

    # ------------------------------------------------------------ the paths

    @property
    def vault_root(self) -> Path:
        return self.tree.root / "vault"

    @property
    def code_root(self) -> Path:
        return self.tree.root / "code"

    def vault(self, name: str = NOTES) -> Path:
        """The directory of one vault scope."""
        return self.vault_root / name

    def code(self, name: str = REPO) -> Path:
        """The directory of one code repository."""
        return self.code_root / name

    def index_dir(self, name: str = NOTES) -> Path:
        """The index directory of one corpus, outside the corpus."""
        return self.tree.state_root / "index" / name

    def store(self, name: str = NOTES) -> Store:
        """The store of one index."""
        return Store(self.index_dir(name) / STORE_FILE)

    # ----------------------------------------------------------- the program

    @property
    def tei_url(self) -> str:
        """Where the TEI stand-in listens."""
        return f"http://{LOOPBACK}:{self.tei_port}"

    def tei_env(self) -> dict[str, str]:
        """The one variable that names the TEI stand-in."""
        return {TEI_URL_ENV: self.tei_url}

    def run_index(
        self,
        scope: Path,
        index_dir: Path,
        profile: Profile | None = None,
        env: Mapping[str, str] | None = None,
    ) -> Finished:
        """Run the program on one corpus to its end.

        `env` is what the program gets beside `PATH`. None gives the URL of
        the TEI stand-in.
        """
        return self.run_words(index_words(scope, index_dir, profile), env)

    def run_reference(
        self,
        scope: Path,
        index_dir: Path,
        profile: Profile | None = None,
        env: Mapping[str, str] | None = None,
    ) -> Finished:
        """Run the reference on one corpus to its end: the default command of the row.

        The variable of the row does not change this command. The run gets
        what a run of `run_index` gets with the default command. Only a
        scenario that compares the two writers of a store calls this
        function (`integration/proc/AGENTS.md`, "The reference").
        """
        args = index_words(scope, index_dir, profile)
        words, whole = self._command(reference_of(Service.LIBRARY), args, env)

        return self.supervisor.run(REFERENCE, words, whole, self.tree.root, INDEX_DEADLINE_S)

    def start_index(
        self,
        scope: Path,
        index_dir: Path,
        profile: Profile | None = None,
        env: Mapping[str, str] | None = None,
    ) -> Child:
        """Start the program on one corpus, for a scenario that acts during the run."""
        args = index_words(scope, index_dir, profile)
        words, whole = self._command(command_of(Service.LIBRARY), args, env)

        return self.supervisor.spawn(Service.LIBRARY.value, words, whole, self.tree.root)

    def run_words(self, args: Sequence[str], env: Mapping[str, str] | None = None) -> Finished:
        """Run the program with the words of a test to its end."""
        words, whole = self._command(command_of(Service.LIBRARY), args, env)

        return self.supervisor.run(
            Service.LIBRARY.value, words, whole, self.tree.root, INDEX_DEADLINE_S
        )

    def start_held_update(self, scope: Path, index_dir: Path) -> Child:
        """Start an update of a built vault scope. Return when TEI holds its last call.

        The update changes the two notes of `write_vault`. The first note in
        path order gets `FIRST_WORD`. The second note gets `SECOND_WORD` and
        the text that the stand-in holds. So the program has the vectors of
        the first note when it waits, and the store must not show them.
        `release_hold` lets the call go.
        """
        write_note(scope / BIKES, f"The {FIRST_WORD} has new bicycle racks.")
        write_note(scope / MEETING, f"Please {HELD_TEXT}. The {SECOND_WORD} is in the lobby.")

        return self.start_held(scope, index_dir)

    def start_held(self, scope: Path, index_dir: Path) -> Child:
        """Start the program on a corpus with `HELD_TEXT` in one file. Return when TEI holds.

        The program then has the vectors of each changed file before that
        file in path order, and it waits for the answer of one embed call.
        `release_hold` lets the call go.
        """
        tune(self.tree, TEI, TEI_HOLD_EMBED, HELD_TEXT)
        child = self.start_index(scope, index_dir)

        def held() -> bool:
            if child.exit_code() is not None:
                raise ProcError(f"the run ended before the stand-in held a call\n{child.output()}")

            return any(HELD_TEXT in text for texts in self.embedded() for text in texts)

        wait_until(held, "the embed call that the stand-in holds", INDEX_DEADLINE_S)

        return child

    def release_hold(self) -> None:
        """Let the stand-in answer the call that it holds."""
        untune(self.tree, TEI, TEI_HOLD_EMBED)

    def read_as_bridge(self, store: Path, match: str, limit: int) -> dict[str, Any]:
        """What the statements of the bridge give for one store, through the SQLite of Node.

        `match` is an FTS5 query, and `limit` is the most rows that the
        query gives. The docstring of `reader_store.mjs` has the keys of the
        answer. The reader runs on a store that no program can write, in a
        directory that no program can write: `_read_only` says why.
        """
        words = ["node", str(reader_script()), str(store), match, str(limit)]

        with _read_only(store):
            done = self.supervisor.run(READER, words, _path_env(), self.tree.root, INDEX_DEADLINE_S)

        if done.exit_code != 0:
            raise ProcError(f"the reader of {store} exited {done.exit_code}\n{done.stderr}")

        answer: dict[str, Any] = json.loads(done.stdout)

        return answer

    def _command(
        self, command: StartCommand, args: Sequence[str], env: Mapping[str, str] | None
    ) -> tuple[list[str], dict[str, str]]:
        given = self.tei_env() if env is None else dict(env)

        return [*command.words, *args], _path_env() | env_of(command) | given

    # ------------------------------------------------ the record of the stand-in

    def embedded(self) -> list[list[str]]:
        """The texts of each embed call that the stand-in got, in arrival order.

        A body with more than the key `inputs` is an error. Another key can
        change what the real service answers.
        """
        asked = ("POST", standin_tei.EMBED_PATH)

        return [
            _inputs_of(call["body"])
            for call in tei_calls(self.tree)
            if (call["method"], call["path"]) == asked
        ]

    def info_calls(self) -> int:
        """How many times the program asked the stand-in for its model."""
        asked = ("GET", standin_tei.INFO_PATH)

        return sum((call["method"], call["path"]) == asked for call in tei_calls(self.tree))


def _inputs_of(body: object) -> list[str]:
    """The texts of one embed body. A body of another shape is an error."""
    if isinstance(body, dict):
        fields = cast("dict[str, object]", body)
        inputs = fields.get("inputs")

        if set(fields) == {"inputs"} and isinstance(inputs, list):
            return cast("list[str]", inputs)

    raise ProcError(f"an embed call of {TEI} has the body {body!r}")


def reader_script() -> Path:
    """The program that reads a store with the statements of the bridge."""
    return Path(__file__).resolve().parent / READER_SCRIPT


def node_has_sqlite() -> bool:
    """Whether the `node` of the run has the module `node:sqlite`. The reader needs it."""
    words = ["node", "--eval", "require('node:sqlite')"]

    try:
        probe = subprocess.run(
            words,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env=_path_env(),
            timeout=_NODE_PROBE_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False

    return probe.returncode == 0


def index_words(scope: Path, index_dir: Path, profile: Profile | None = None) -> list[str]:
    """The words that an index unit puts after the program.

    None gives no third word, as the unit of a vault scope does.
    `test_proc_table.py` holds these words against the two units.
    """
    words = [str(scope), str(index_dir)]

    return words if profile is None else [*words, profile.value]


def report_of(done: Finished) -> Report:
    """The report of one run. A run with no report line is an error."""
    lines = done.stdout.split("\n")
    first = _REPORT_LINE.fullmatch(lines[0])

    if first is None:
        raise ProcError(
            f"the run exited {done.exit_code} with no report line\n"
            f"--- stdout ---\n{done.stdout}\n--- stderr ---\n{done.stderr}"
        )

    return Report(
        scope=first["scope"],
        indexed=int(first["indexed"]),
        unchanged=int(first["unchanged"]),
        removed=int(first["removed"]),
        chunks=int(first["chunks"]),
        rebuilt=first["rebuilt"] is not None,
        errors=tuple(
            line.removeprefix(_ERROR_START) for line in lines[1:] if line.startswith(_ERROR_START)
        ),
    )


def vector_blob(text: str) -> bytes:
    """The blob that a store holds for one chunk: what the stand-in answers, as float32.

    The values are little-endian (`library/AGENTS.md`, rule 6).
    """
    return struct.pack(f"<{DIMS}f", *standin_tei.vector_of(text, DIMS))


# ------------------------------------------------------------ the corpus writers


def write_vault(scope: Path) -> None:
    """A vault scope with two notes, one draft in `review-inbox` and one dot file.

    The program reads the two notes and neither of the others.
    """
    write_note(scope / MEETING, MEETING_TEXT)
    write_note(scope / BIKES, BIKES_TEXT)
    write_note(scope / "review-inbox" / "draft.md", "unapproved draft, never indexed")
    write_note(scope / ".hidden.md", "hidden file, never indexed")


def write_repo(root: Path) -> None:
    """A code repository: four source files, one PDF, build output and a `.git` directory.

    With the `code` profile the program reads the four source files and
    nothing else.
    """
    write_note(root / "src" / "app.py", "def handler():\n    return 'ok'\n")
    write_note(root / "src" / "client.ts", "export const call = () => fetch('/v1');\n")
    write_note(root / "README.md", "The service exposes /v1 and returns ok.\n")
    write_note(root / "pyproject.toml", '[project]\nname = "demo"\n')
    (root / "notes.pdf").write_bytes(b"%PDF-1.4 not source")

    for junk in BUILD_OUTPUT:
        write_note(root / junk / "bundle.js", "// generated, never indexed\n")

    write_note(root / ".git" / "config.toml", "# vcs internals, never indexed\n")


def write_corpus_of(scope: Path, chunks: int) -> Path:
    """A vault scope that gives a known count of chunks. Returns its last file in path order.

    Each file holds paragraphs of `PARAGRAPH_CHARS` characters, and one
    paragraph gives one chunk. A file holds `FILE_PARAGRAPHS` paragraphs, and
    the last file holds the rest.
    """
    if chunks < 1:
        raise ValueError("a corpus has one chunk or more")

    last = scope

    for serial, first in enumerate(range(0, chunks, FILE_PARAGRAPHS)):
        last = scope / f"doc-{serial:04d}.md"
        count = min(FILE_PARAGRAPHS, chunks - first)
        write_note(last, _PARAGRAPH_GAP.join(paragraph(last.stem, index) for index in range(count)))

    return last


def write_mixed(scope: Path) -> None:
    """A vault scope with each kind of file that two writers of one store must read alike.

    It holds the files of `write_vault`, the files of `_MIXED_FILES`, one
    paragraph that is too long for one chunk, and two files of
    `write_corpus_of`. It holds a link to a file and a link to a directory,
    and both targets are outside the scope. A writer reads the file behind
    the first link and does not follow the second one.

    It holds no PDF with text. Two PDF readers can give the text of one
    page with other spaces, and two stores then differ for no fault of a
    writer. The one PDF here is broken, so each writer reports it.
    """
    outside = scope.with_name(f"{scope.name}{_OUTSIDE_SUFFIX}")
    write_vault(scope)
    write_corpus_of(scope, MIXED_PARAGRAPHS)
    write_note(scope / "long.md", _LONG_PARAGRAPH)

    for name, raw in _MIXED_FILES.items():
        (scope / name).parent.mkdir(parents=True, exist_ok=True)
        (scope / name).write_bytes(raw)

    write_note(outside / "target.md", "A note outside the scope, behind a link to a file.")
    write_note(outside / "inner" / "unseen.md", "A note behind a link to a directory.")
    (scope / LINKED_NOTE).symlink_to(outside / "target.md")
    (scope / LINKED_DIR).symlink_to(outside / "inner", target_is_directory=True)


def paragraph(stem: str, index: int) -> str:
    """One paragraph of `PARAGRAPH_CHARS` characters that names its file and its place."""
    head = f"{stem} paragraph {index:04d} "

    return head + "x" * (PARAGRAPH_CHARS - len(head))


def write_pdf(path: Path, text: str) -> None:
    """A PDF with one page that shows one line of text.

    The file has five objects and a correct cross-reference table. The page
    names its `/MediaBox` and one standard font, so each PDF reader has what
    it needs to find the text.
    """
    if _PDF_TEXT.fullmatch(text) is None:
        raise ValueError("the text of a PDF fixture holds only letters, digits, spaces and periods")

    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("ascii")
    page = (
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"
    )
    objects = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        page,
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Length %d >>\nstream\n%b\nendstream" % (len(content), content),
    )
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []

    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%b\nendobj\n" % (number, body)

    table_at = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)

    for offset in offsets:
        out += b"%010d 00000 n \n" % offset

    out += b"trailer\n<< /Size %d /Root 1 0 R >>\n" % (len(objects) + 1)
    out += b"startxref\n%d\n%%%%EOF\n" % table_at
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))


def listed(scope: Path, relative: str) -> Path:
    """One file of a corpus under the name that the directory listing gives it.

    A file system can keep a name in another Unicode form than the form that
    a test wrote. The program stores the name of the listing.
    """
    found = scope

    for part in Path(relative).parts:
        want = unicodedata.normalize("NFC", part)
        names = [name for name in os.listdir(found) if unicodedata.normalize("NFC", name) == want]

        if len(names) != 1:
            raise ProcError(f"{found} lists {len(names)} entries for {part!r}")

        found = found / names[0]

    return found


def write_note(path: Path, text: str) -> None:
    """One text file of a corpus, as UTF-8, in a directory that can be new."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _path_env() -> dict[str, str]:
    """The one variable of the run that reaches the program and the stand-in."""
    return {"PATH": os.environ.get("PATH", os.defpath)}
