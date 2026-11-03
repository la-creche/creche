"""The PDFs of the vault become documents: listed, read in a child, cut, kept and forgotten."""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from creche_memory import pdf as pdf_module
from creche_memory import pdftext
from creche_memory.pdf import (
    STRUCTURE_COLUMN,
    PdfTexts,
    extract_in_child,
    list_pdfs,
    pdf_rows,
)
from creche_memory.pdftext import MAX_PDF_CHARS
from creche_memory.sync import vault_stamp

#: Builds the real dlt source after `configure` and prints its tags and rows.
#: It runs in a process of its own, because Cognee reads the env only once.
SOURCE_PROBE = """
import json, sys
from pathlib import Path
from creche_memory.store import StoreSettings, configure

root = Path(sys.argv[1])
configure(StoreSettings(
    store_root=root / "store",
    llm_base_url="http://llm.test:4000",
    llm_key="sk-test-not-a-real-key",
    embedding_base_url="http://embed.test:8085",
    tokenizer_cache=root / "hf",
))

from cognee.tasks.ingestion import dlt_utils
from creche_memory.pdf import PdfTexts, pdf_source

source = pdf_source(root / "vault", ["house"], PdfTexts(lambda path: "Article 4: pets"))
print(json.dumps({
    "name": source.name,
    "tag": dlt_utils.document_source_tag(source),
    "scope": getattr(source, dlt_utils.PIPELINE_SCOPE_ATTR),
    "rows": list(source),
}))
"""


def _pdf(text: str) -> bytes:
    """A one-page PDF whose page shows `text` in Helvetica."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"

    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset

    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )

    return out


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    for folder in ("finance/2026", "finance/.trash", "house/01 - Governance"):
        (root / folder).mkdir(parents=True)

    (root / "finance" / "statement.pdf").write_bytes(b"a")
    (root / "finance" / "2026" / "June.PDF").write_bytes(b"b")
    (root / "finance" / ".hidden.pdf").write_bytes(b"c")
    (root / "finance" / ".trash" / "old.pdf").write_bytes(b"d")
    (root / "finance" / "notes.md").write_text("a note", encoding="utf-8")
    (root / "finance" / "link.pdf").symlink_to(root / "finance" / "statement.pdf")
    (root / "house" / "01 - Governance" / "Bylaws.pdf").write_bytes(b"e")

    return root


class Extracts:
    """A stand-in for the child: the text of each file by name."""

    def __init__(self, texts: dict[str, str | None]) -> None:
        self.texts = texts
        self.calls: list[str] = []

    def __call__(self, path: Path) -> str | None:
        self.calls.append(path.name)
        return self.texts.get(path.name, "")


def test_the_listing_takes_pdfs_of_the_chosen_folders_only(vault: Path) -> None:
    assert list_pdfs(vault, ["finance"]) == ["finance/2026/June.PDF", "finance/statement.pdf"]
    assert list_pdfs(vault, ["finance", "house/01 - Governance"]) == [
        "finance/2026/June.PDF",
        "finance/statement.pdf",
        "house/01 - Governance/Bylaws.pdf",
    ]


def test_each_pdf_with_text_is_a_document_in_its_folders(vault: Path) -> None:
    texts = PdfTexts(Extracts({"Bylaws.pdf": "Article 4: pets", "statement.pdf": "Balance 12"}))
    state: dict[str, Any] = {}
    rows = list(pdf_rows(vault, ["finance", "house/01 - Governance"], state, texts))

    assert [row["id"] for row in rows] == [
        "obsidian:finance/statement.pdf",
        "obsidian:house/01 - Governance/Bylaws.pdf",
    ]
    assert rows[1]["title"] == "Bylaws"
    assert rows[1]["content"] == "Article 4: pets"
    assert rows[1]["_deleted"] is False
    assert rows[1][STRUCTURE_COLUMN] == {
        "ancestors": [
            {"kind": "folder", "id": "house/01 - Governance", "name": "01 - Governance"},
            {"kind": "folder", "id": "house", "name": "house"},
        ]
    }
    # June.PDF has no text: a scan the OCR job has not done yet.
    assert state["members"] == [row["id"] for row in rows]


def test_a_removed_pdf_is_forgotten(vault: Path) -> None:
    texts = PdfTexts(Extracts({"statement.pdf": "Balance 12", "June.PDF": "June"}))
    state: dict[str, Any] = {}
    list(pdf_rows(vault, ["finance"], state, texts))
    (vault / "finance" / "2026" / "June.PDF").unlink()

    rows = list(pdf_rows(vault, ["finance"], state, texts))

    assert rows[-1] == {"id": "obsidian:finance/2026/June.PDF", "_deleted": True}
    assert state["members"] == ["obsidian:finance/statement.pdf"]


def test_an_unreadable_pdf_keeps_what_the_store_holds(vault: Path) -> None:
    extracts = Extracts({"statement.pdf": "Balance 12", "June.PDF": "June"})
    state: dict[str, Any] = {}
    list(pdf_rows(vault, ["finance"], state, PdfTexts(extracts)))
    extracts.texts = {"statement.pdf": None, "June.PDF": "June"}

    rows = list(pdf_rows(vault, ["finance"], state, PdfTexts(extracts)))

    assert [row["id"] for row in rows] == ["obsidian:finance/2026/June.PDF"]
    assert state["members"] == ["obsidian:finance/2026/June.PDF", "obsidian:finance/statement.pdf"]


def test_a_pdf_is_read_again_only_when_it_changes(vault: Path) -> None:
    extracts = Extracts({"statement.pdf": "Balance 12"})
    texts = PdfTexts(extracts)
    texts.warm(vault, ["finance"])
    texts.warm(vault, ["finance"])
    assert sorted(extracts.calls) == ["June.PDF", "statement.pdf"]

    path = vault / "finance" / "statement.pdf"
    os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 1_000_000))
    texts.warm(vault, ["finance"])

    assert sorted(extracts.calls) == ["June.PDF", "statement.pdf", "statement.pdf"]


def test_a_long_pdf_is_cut_at_the_cap_and_counted(vault: Path) -> None:
    texts = PdfTexts(Extracts({"statement.pdf": "x" * (MAX_PDF_CHARS + 1)}))

    text = texts.text(vault, "finance/statement.pdf")

    assert text is not None
    assert len(text) == MAX_PDF_CHARS
    assert texts.cut == 1


def test_a_new_pdf_moves_the_vault_stamp(vault: Path) -> None:
    before = vault_stamp(vault, ["finance"])
    (vault / "finance" / "receipt.pdf").write_bytes(b"f")

    assert vault_stamp(vault, ["finance"]) != before


def test_the_child_reads_the_text_of_a_pdf(tmp_path: Path) -> None:
    path = tmp_path / "bylaws.pdf"
    path.write_bytes(_pdf("Article 4 allows two pets"))

    text = extract_in_child(path)

    assert text is not None
    assert "Article 4 allows two pets" in text


def test_the_child_fails_closed_on_a_broken_pdf(tmp_path: Path) -> None:
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"%PDF-1.4\nnot a pdf at all")

    assert extract_in_child(path) is None


def test_the_child_gets_no_env_and_an_isolated_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        seen.update(args=args, **kwargs)
        return subprocess.CompletedProcess(args, 0, b"text", b"")

    monkeypatch.setenv("MEMORY_LITELLM_KEY", "sk-secret")
    monkeypatch.setattr(pdf_module.subprocess, "run", run)
    path = tmp_path / "a.pdf"
    path.write_bytes(b"a")

    assert extract_in_child(path) == "text"
    assert seen["env"] == {}
    assert seen["args"][1:4] == ["-I", "-m", "creche_memory.pdftext"]


def test_a_pdf_over_the_size_cap_is_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the child ran")

    monkeypatch.setattr(pdf_module, "MAX_PDF_BYTES", 3)
    monkeypatch.setattr(pdf_module.subprocess, "run", run)
    path = tmp_path / "big.pdf"
    path.write_bytes(b"abcd")

    assert extract_in_child(path) is None


def test_the_child_stops_one_character_over_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "long.pdf"
    path.write_bytes(_pdf("a long line of text"))
    monkeypatch.setattr(pdftext, "MAX_PDF_CHARS", 5)

    assert len(pdftext.read_text(str(path))) == 6


def test_the_source_is_a_document_source_with_a_table_of_its_own(tmp_path: Path) -> None:
    (tmp_path / "vault" / "house").mkdir(parents=True)
    (tmp_path / "vault" / "house" / "Bylaws.pdf").write_bytes(b"a")
    done = subprocess.run(
        [sys.executable, "-c", SOURCE_PROBE, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=tmp_path,
    )
    assert done.returncode == 0, done.stderr[-2000:]

    seen = json.loads(done.stdout.strip().splitlines()[-1])

    assert seen["name"] == pdf_module.RESOURCE
    assert seen["tag"] == pdf_module.SOURCE_TAG
    assert seen["scope"] == pdf_module.RESOURCE
    assert [row["id"] for row in seen["rows"]] == ["obsidian:house/Bylaws.pdf"]
