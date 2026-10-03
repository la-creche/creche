from __future__ import annotations

import sqlite3
import struct
from pathlib import Path

import sqlite_vec
from library.library import CODE_PROFILE, chunk_text, index_scope


class FakeEmbedder:
    def __init__(self, model: str = "BAAI/bge-base-en-v1.5") -> None:
        self.model = model
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        # deterministic: vector keyed off first char so tests can reason
        return [[float(ord(t[0]) % 7)] + [0.0] * 767 for t in texts]


def index_dir_for(scope: Path) -> Path:
    return scope.parent / "index" / scope.name


def open_store(scope: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(index_dir_for(scope) / "store.db")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def seed(scope: Path) -> None:
    scope.mkdir(parents=True, exist_ok=True)
    (scope / "meeting.md").write_text(
        "The meeting is on Tuesday in the lobby.\n\nBring your badge."
    )
    (scope / "bikes.md").write_text("Bicycle storage rules were updated in March.")
    (scope / "review-inbox").mkdir(exist_ok=True)
    (scope / "review-inbox" / "draft.md").write_text("unapproved draft, never indexed")
    (scope / ".hidden.md").write_text("hidden file, never indexed")


def test_build_and_query(tmp_path: Path) -> None:
    scope = tmp_path / "notes"
    seed(scope)
    embedder = FakeEmbedder()
    report = index_scope(scope, embedder, index_dir_for(scope))
    assert len(report.indexed) == 2 and not report.errors

    conn = open_store(scope)
    model = conn.execute("SELECT value FROM meta WHERE key='embed_model'").fetchone()[0]
    assert model == embedder.model
    paths = {r[0] for r in conn.execute("SELECT DISTINCT path FROM chunks")}
    assert all("meeting.md" in p or "bikes.md" in p for p in paths)
    assert not any("review-inbox" in p or ".hidden" in p for p in paths)
    hits = conn.execute(
        "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH '\"bicycle\"'"
    ).fetchall()
    assert hits
    n_vec = conn.execute("SELECT count(*) FROM chunks_vec").fetchone()[0]
    n_chunks = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
    assert n_vec == n_chunks > 0
    conn.close()


def test_incremental_only_reembeds_changed(tmp_path: Path) -> None:
    scope = tmp_path / "notes"
    seed(scope)
    embedder = FakeEmbedder()
    index_scope(scope, embedder, index_dir_for(scope))
    first_calls = len(embedder.calls)

    report = index_scope(scope, embedder, index_dir_for(scope))  # nothing changed
    assert report.unchanged == 2 and not report.indexed
    assert len(embedder.calls) == first_calls

    (scope / "meeting.md").write_text("The meeting moved to Thursday.")
    report = index_scope(scope, embedder, index_dir_for(scope))
    assert [Path(p).name for p in report.indexed] == ["meeting.md"]
    assert report.unchanged == 1


def test_deletion_removes_rows(tmp_path: Path) -> None:
    scope = tmp_path / "notes"
    seed(scope)
    embedder = FakeEmbedder()
    index_scope(scope, embedder, index_dir_for(scope))
    (scope / "bikes.md").unlink()
    report = index_scope(scope, embedder, index_dir_for(scope))
    assert any("bikes.md" in p for p in report.removed)
    conn = open_store(scope)
    assert not conn.execute("SELECT 1 FROM chunks WHERE path LIKE '%bikes%'").fetchall()
    conn.close()


def test_bad_file_isolated(tmp_path: Path) -> None:
    scope = tmp_path / "notes"
    seed(scope)
    (scope / "broken.pdf").write_bytes(b"not a real pdf")
    embedder = FakeEmbedder()
    report = index_scope(scope, embedder, index_dir_for(scope))
    assert any("broken.pdf" in p for p in report.errors)
    assert len(report.indexed) == 2  # the others made it


def test_model_change_forces_full_rebuild(tmp_path: Path) -> None:
    scope = tmp_path / "notes"
    seed(scope)
    index_scope(scope, FakeEmbedder(model="old-model"), index_dir_for(scope))
    report = index_scope(scope, FakeEmbedder(model="new-model"), index_dir_for(scope))
    assert report.rebuilt and len(report.indexed) == 2
    conn = open_store(scope)
    model = conn.execute("SELECT value FROM meta WHERE key='embed_model'").fetchone()[0]
    assert model == "new-model"
    conn.close()


def test_chunking_overlap_and_bounds() -> None:
    text = "\n\n".join(f"paragraph {i} " + "x" * 300 for i in range(10))
    chunks = chunk_text(text, size=1000, overlap=200)
    assert all(len(c) <= 1200 for c in chunks)
    assert len(chunks) >= 3


def seed_repo(root: Path) -> None:
    """A repo shaped like the real thing: source worth indexing, plus the
    build output that would swamp the index if the profile let it through."""
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "app.py").write_text("def handler():\n    return 'ok'\n")
    (root / "src" / "client.ts").write_text("export const call = () => fetch('/v1');\n")
    (root / "README.md").write_text("The service exposes /v1 and returns ok.\n")
    (root / "pyproject.toml").write_text('[project]\nname = "demo"\n')
    (root / "notes.pdf").write_bytes(b"%PDF-1.4 not source")
    for junk in ("node_modules", "dist", "__pycache__"):
        (root / junk).mkdir(exist_ok=True)
        (root / junk / "bundle.js").write_text("// generated, never indexed\n")
    (root / ".git").mkdir(exist_ok=True)
    (root / ".git" / "config.toml").write_text("# vcs internals, never indexed\n")


def emb_rows(conn: sqlite3.Connection) -> dict[int, tuple[float, ...]]:
    """`chunks_emb` decoded: little-endian float32, `dims` of them per row."""
    rows: dict[int, tuple[float, ...]] = {}
    for chunk_id, raw in conn.execute("SELECT id, embedding FROM chunks_emb"):
        blob = bytes(raw)
        rows[chunk_id] = struct.unpack(f"<{len(blob) // 4}f", blob)
    return rows


def test_plain_vectors_mirror_every_chunk(tmp_path: Path) -> None:
    # The sandbox reads vectors with node:sqlite, which cannot load sqlite-vec,
    # so every chunk's vector is also a plain float32 blob (library/README.md).
    scope = tmp_path / "notes"
    seed(scope)
    embedder = FakeEmbedder()
    index_scope(scope, embedder, index_dir_for(scope))

    conn = open_store(scope)
    rows = emb_rows(conn)
    texts = dict(conn.execute("SELECT id, text FROM chunks").fetchall())
    conn.close()

    assert set(rows) == set(texts)
    assert all(len(vector) == 768 for vector in rows.values())
    assert all(rows[i][0] == float(ord(texts[i][0]) % 7) for i in rows)


def test_deletion_removes_plain_vectors(tmp_path: Path) -> None:
    scope = tmp_path / "notes"
    seed(scope)
    embedder = FakeEmbedder()
    index_scope(scope, embedder, index_dir_for(scope))
    (scope / "bikes.md").unlink()
    index_scope(scope, embedder, index_dir_for(scope))

    conn = open_store(scope)
    chunk_ids = {row[0] for row in conn.execute("SELECT id FROM chunks")}
    assert set(emb_rows(conn)) == chunk_ids
    conn.close()


def test_store_from_before_plain_vectors_is_backfilled_without_reembedding(
    tmp_path: Path,
) -> None:
    # Every store on the host predates chunks_emb. The vectors are already in
    # chunks_vec, so the upgrade copies them and embeds nothing.
    scope = tmp_path / "notes"
    seed(scope)
    index_scope(scope, FakeEmbedder(), index_dir_for(scope))
    conn = open_store(scope)
    conn.execute("DROP TABLE chunks_emb")
    conn.commit()
    conn.close()

    embedder = FakeEmbedder()
    report = index_scope(scope, embedder, index_dir_for(scope))

    assert report.unchanged == 2 and not embedder.calls
    conn = open_store(scope)
    rows = emb_rows(conn)
    vec = {
        rowid: struct.unpack(f"<{len(bytes(raw)) // 4}f", bytes(raw))
        for rowid, raw in conn.execute("SELECT rowid, embedding FROM chunks_vec")
    }
    conn.close()
    assert rows == vec and rows


def test_code_profile_indexes_source_and_skips_build_output(tmp_path: Path) -> None:
    repo = tmp_path / "agent-control"
    seed_repo(repo)
    report = index_scope(repo, FakeEmbedder(), index_dir_for(repo), profile=CODE_PROFILE)

    indexed = {Path(p).name for p in report.indexed}
    assert indexed == {"app.py", "client.ts", "README.md", "pyproject.toml"}
    assert not report.errors
    # the profile is the whole defense against a 100k-file node_modules
    assert not any("node_modules" in p or "dist" in p for p in report.indexed)
    assert not any("__pycache__" in p or ".git" in p for p in report.indexed)
    # .pdf is prose-corpus only; source trees do not carry scanned documents
    assert not any(p.endswith(".pdf") for p in report.indexed)


def test_vault_profile_ignores_source_files(tmp_path: Path) -> None:
    scope = tmp_path / "notes"
    seed(scope)
    (scope / "script.py").write_text("print('not prose')\n")
    report = index_scope(scope, FakeEmbedder(), index_dir_for(scope))
    assert {Path(p).name for p in report.indexed} == {"meeting.md", "bikes.md"}
