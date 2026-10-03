"""Build one scope's hybrid index. Deterministic, not an agent.

Four rules, each a silent failure if wrong:
1. The HASH decides re-indexing, never mtime (sync tools preserve mtimes).
2. Embedding batches stay under the TEI client cap.
3. `review-inbox/` and dotted dirs are never indexed (approve-by-moving).
4. One bad file never kills the scope: per-file errors collect, the walk
   continues.
And a fifth: readers see the index only through an ATOMIC rename —
the store is updated on a work copy and published with os.replace, so a
reader in a sandbox (via virtiofs) never observes a half-written database.
And one for the reader: every vector is written twice, to `chunks_vec`
(sqlite-vec) and to `chunks_emb` as a plain little-endian float32 blob. The
sandbox's `index_search` reads with node:sqlite, which cannot load sqlite-vec.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import sqlite3
import struct
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

import sqlite_vec

log = logging.getLogger("library")


class Corpus(StrEnum):
    """What kind of tree is being indexed. The profile decides which files are
    worth embedding and which directories are build output."""

    VAULT = "vault"
    CODE = "code"


@dataclass(frozen=True)
class CorpusProfile:
    suffixes: frozenset[str]
    skip_dirs: frozenset[str]


#: Prose corpora: notes and scanned documents.
VAULT_PROFILE = CorpusProfile(
    suffixes=frozenset({".md", ".txt", ".pdf"}),
    skip_dirs=frozenset({".index", "review-inbox"}),
)
#: Source corpora. `_walk` already skips dot-directories (.git, .venv, .next),
#: so these are the build outputs that would otherwise swamp the index —
#: node_modules alone dwarfs the source it sits next to.
CODE_PROFILE = CorpusProfile(
    suffixes=frozenset(
        {
            ".py",
            ".ts",
            ".tsx",
            ".js",
            ".jsx",
            ".rs",
            ".go",
            ".sh",
            ".toml",
            ".yaml",
            ".yml",
            ".sql",
            ".md",
            ".txt",
        }
    ),
    skip_dirs=frozenset({".index", "node_modules", "target", "dist", "build", "__pycache__"}),
)
PROFILES = {Corpus.VAULT: VAULT_PROFILE, Corpus.CODE: CODE_PROFILE}
CHUNK_SIZE = 1000
CHUNK_OVERLAP = 200
EMBED_MAX_BATCH = 32
DIMS = 768


class Embedder(Protocol):
    @property
    def model(self) -> str: ...
    def embed(self, texts: list[str]) -> list[list[float]]: ...


@dataclass
class IndexReport:
    scope: str
    indexed: list[str] = field(default_factory=list[str])
    removed: list[str] = field(default_factory=list[str])
    unchanged: int = 0
    chunks: int = 0
    errors: dict[str, str] = field(default_factory=dict[str, str])
    rebuilt: bool = False

    def render(self) -> str:
        lines = [
            f"scope {self.scope}: {len(self.indexed)} indexed, {self.unchanged} unchanged, "
            f"{len(self.removed)} removed, {self.chunks} chunks"
            + (" [FULL REBUILD: embed model changed]" if self.rebuilt else "")
        ]
        lines.extend(f"  ERROR {path}: {err}" for path, err in self.errors.items())
        return "\n".join(lines)


def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Paragraph-preferring chunks of ~size chars with overlap."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(current) + len(paragraph) + 2 <= size:
            current = f"{current}\n\n{paragraph}" if current else paragraph
            continue
        if current:
            chunks.append(current)
            current = current[-overlap:] if overlap else ""
        while len(paragraph) > size:
            chunks.append(paragraph[:size])
            paragraph = paragraph[size - overlap :]
        current = f"{current}\n\n{paragraph}".strip() if current else paragraph
    if current:
        chunks.append(current)
    return chunks


def read_document(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        return "\n\n".join(page.extract_text() or "" for page in reader.pages)
    return path.read_text(encoding="utf-8", errors="replace")


def file_hash(path: Path) -> str:
    h = hashlib.blake2b(digest_size=16)
    with path.open("rb") as fh:
        while block := fh.read(1 << 16):
            h.update(block)
    return h.hexdigest()


def _open(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def _create_schema(conn: sqlite3.Connection, model: str) -> None:
    conn.executescript(
        f"""
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE files(path TEXT PRIMARY KEY, hash TEXT, mtime REAL, indexed_at REAL);
        CREATE TABLE chunks(id INTEGER PRIMARY KEY, path TEXT, ord INTEGER, text TEXT);
        CREATE VIRTUAL TABLE chunks_fts USING fts5(text);
        CREATE VIRTUAL TABLE chunks_vec USING vec0(embedding float[{DIMS}]);
        CREATE TABLE chunks_emb(id INTEGER PRIMARY KEY, embedding BLOB NOT NULL);
        """
    )
    conn.execute("INSERT INTO meta VALUES ('embed_model', ?)", (model,))
    conn.execute("INSERT INTO meta VALUES ('dims', ?)", (str(DIMS),))


def _float32(vector: list[float]) -> bytes:
    """`chunks_emb`'s blob: little-endian float32, whatever the host's order."""
    return struct.pack(f"<{len(vector)}f", *vector)


def _ensure_plain_vectors(conn: sqlite3.Connection) -> None:
    """Give a store that predates `chunks_emb` the table, filled from
    `chunks_vec`. The vectors are already there, so nothing is re-embedded."""
    found = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'chunks_emb'"
    ).fetchone()
    if found is not None:
        return

    conn.execute("CREATE TABLE chunks_emb(id INTEGER PRIMARY KEY, embedding BLOB NOT NULL)")
    conn.execute("INSERT INTO chunks_emb(id, embedding) SELECT rowid, embedding FROM chunks_vec")


def _walk(scope: Path, profile: CorpusProfile) -> list[Path]:
    found: list[Path] = []
    for root, dirs, names in os.walk(scope):
        dirs[:] = [d for d in dirs if d not in profile.skip_dirs and not d.startswith(".")]
        for name in names:
            if name.startswith("."):
                continue
            path = Path(root) / name
            if path.suffix.lower() in profile.suffixes:
                found.append(path)
    return sorted(found)


def _remove_file_rows(conn: sqlite3.Connection, path: str) -> None:
    ids = [row[0] for row in conn.execute("SELECT id FROM chunks WHERE path = ?", (path,))]
    for chunk_id in ids:
        conn.execute("DELETE FROM chunks_fts WHERE rowid = ?", (chunk_id,))
        conn.execute("DELETE FROM chunks_vec WHERE rowid = ?", (chunk_id,))
        conn.execute("DELETE FROM chunks_emb WHERE id = ?", (chunk_id,))
    conn.execute("DELETE FROM chunks WHERE path = ?", (path,))
    conn.execute("DELETE FROM files WHERE path = ?", (path,))


def index_scope(
    scope: Path,
    embedder: Embedder,
    index_dir: Path,
    *,
    profile: CorpusProfile = VAULT_PROFILE,
) -> IndexReport:
    """`index_dir` lives OUTSIDE the scope (/srv/agents/state/index/<scope>):
    indexes are derived state, and anything inside the vault syncs to every
    Obsidian device via the LiveSync bridge (which has no exclude filter)."""
    scope = scope.resolve()
    report = IndexReport(scope=str(scope))
    index_dir = index_dir.resolve()
    index_dir.mkdir(parents=True, exist_ok=True)
    store = index_dir / "store.db"
    work = index_dir / "store.work.db"
    work.unlink(missing_ok=True)

    if store.exists():
        shutil.copyfile(store, work)
        conn = _open(work)
        recorded = conn.execute("SELECT value FROM meta WHERE key = 'embed_model'").fetchone()
        if recorded is None or recorded[0] != embedder.model:
            # Model changed: the whole index is in the wrong vector space.
            conn.close()
            work.unlink()
            conn = _open(work)
            _create_schema(conn, embedder.model)
            report.rebuilt = True
        else:
            _ensure_plain_vectors(conn)
    else:
        conn = _open(work)
        _create_schema(conn, embedder.model)

    try:
        on_disk = _walk(scope, profile)
        disk_paths = {str(p) for p in on_disk}
        for row in conn.execute("SELECT path FROM files").fetchall():
            if row[0] not in disk_paths:
                _remove_file_rows(conn, row[0])
                report.removed.append(row[0])

        for path in on_disk:
            try:
                digest = file_hash(path)
                known = conn.execute(
                    "SELECT hash FROM files WHERE path = ?", (str(path),)
                ).fetchone()
                if known is not None and known[0] == digest:
                    report.unchanged += 1
                    continue
                text = read_document(path)
                chunks = chunk_text(text)
                vectors: list[list[float]] = []
                for start in range(0, len(chunks), EMBED_MAX_BATCH):
                    vectors.extend(embedder.embed(chunks[start : start + EMBED_MAX_BATCH]))
                _remove_file_rows(conn, str(path))
                for ord_, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True)):
                    cursor = conn.execute(
                        "INSERT INTO chunks(path, ord, text) VALUES (?, ?, ?)",
                        (str(path), ord_, chunk),
                    )
                    chunk_id = cursor.lastrowid
                    conn.execute(
                        "INSERT INTO chunks_fts(rowid, text) VALUES (?, ?)", (chunk_id, chunk)
                    )
                    conn.execute(
                        "INSERT INTO chunks_vec(rowid, embedding) VALUES (?, ?)",
                        (chunk_id, sqlite_vec.serialize_float32(vector)),
                    )
                    conn.execute(
                        "INSERT INTO chunks_emb(id, embedding) VALUES (?, ?)",
                        (chunk_id, _float32(vector)),
                    )
                conn.execute(
                    "INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?)",
                    (str(path), digest, path.stat().st_mtime, time.time()),
                )
                report.indexed.append(str(path))
                report.chunks += len(chunks)
            except Exception as exc:  # rule 4: one bad file never kills the scope
                report.errors[str(path)] = str(exc)[:200]
                log.warning("skipping %s: %s", path, exc)

        conn.execute("INSERT OR REPLACE INTO meta VALUES ('updated_at', ?)", (str(time.time()),))
        conn.commit()
    finally:
        conn.close()

    os.replace(work, store)  # atomic publish: readers see old or new, never partial
    return report
