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
"""

from __future__ import annotations

import contextlib
import os
import re
import sqlite3
import struct
import unicodedata
from collections.abc import Generator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, cast
from urllib.parse import quote

import sqlite_vec
import standin_tei
from proc_caregiver import wait_until
from proc_harness import LOOPBACK, Child, Finished, ProcError, Supervisor
from proc_services import Service, command_of, env_of
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

#: What `start_held_update` writes. Each note gets one word that no other
#: text of a scenario holds. The second note also gets the text that makes
#: the stand-in hold its embed call.
FIRST_WORD: Final = "harbour"
SECOND_WORD: Final = "lantern"
HELD_TEXT: Final = "hold this call"

#: How many characters one paragraph of `write_corpus_of` has. The program
#: cuts a text into chunks of 1000 characters at most, so two such
#: paragraphs never share a chunk.
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

    def start_index(
        self,
        scope: Path,
        index_dir: Path,
        profile: Profile | None = None,
        env: Mapping[str, str] | None = None,
    ) -> Child:
        """Start the program on one corpus, for a scenario that acts during the run."""
        words, whole = self._command(index_words(scope, index_dir, profile), env)

        return self.supervisor.spawn(Service.LIBRARY.value, words, whole, self.tree.root)

    def run_words(self, args: Sequence[str], env: Mapping[str, str] | None = None) -> Finished:
        """Run the program with the words of a test to its end."""
        words, whole = self._command(args, env)

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
        tune(self.tree, TEI, TEI_HOLD_EMBED, HELD_TEXT)
        child = self.start_index(scope, index_dir)
        wait_until(
            lambda: any(HELD_TEXT in text for texts in self.embedded() for text in texts),
            "the embed call that the stand-in holds",
            INDEX_DEADLINE_S,
        )

        return child

    def release_hold(self) -> None:
        """Let the stand-in answer the call that it holds."""
        untune(self.tree, TEI, TEI_HOLD_EMBED)

    def _command(
        self, args: Sequence[str], env: Mapping[str, str] | None
    ) -> tuple[list[str], dict[str, str]]:
        command = command_of(Service.LIBRARY)
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

    for junk in ("node_modules", "dist", "__pycache__"):
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
