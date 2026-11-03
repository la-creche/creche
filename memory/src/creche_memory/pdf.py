"""The PDFs of the vault, as documents of the memory store.

Cognee's Obsidian connector reads only `.md` notes. This module gives the
store the text of each PDF in the same folders, as a second source beside the
notes. Each PDF is one document. Its id is `obsidian:<path>`, as a note's is,
so a search maps a passage back to the PDF the same way.

1. The text comes from pypdf in a child process (`pdftext.py`). The service
   never parses a PDF itself.
2. The store takes the first `MAX_PDF_CHARS` characters of each PDF.
3. A PDF with no text, such as a scan the OCR job has not processed yet, is
   not a document. Once it has text, the next sync adds it.
4. A PDF whose text cannot be read keeps what the store already holds.
5. A removed PDF is forgotten.
6. The text of each PDF is kept while its size and time stay the same, so a
   sync reads only new and changed PDFs."""

import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from .pdftext import MAX_PDF_CHARS
from .wire import PDF_SUFFIX

#: The id prefix of a document. The same as the Obsidian connector's, so the
#: search maps a PDF passage to its path as it maps a note's.
DOCUMENT_ID_PREFIX = "obsidian:"

#: The staging table and the sync state of the PDFs. A name of its own:
#: Cognee scopes its cleanup by source and table, so the PDF source never
#: forgets a note, and the notes never forget a PDF.
RESOURCE = "vault_pdf"

#: The document source tag. The notes carry the same one.
SOURCE_TAG = "obsidian"

#: The column of a row's place in the folder tree. Cognee's own name
#: (`cognee.tasks.ingestion.dlt_utils.STRUCTURE_COLUMN`), kept here so this
#: module does not import Cognee before `configure` runs.
STRUCTURE_COLUMN = "cognee_structure"

#: The wall-time seconds of one child. The child's CPU limit ends first.
EXTRACT_TIMEOUT_S = 120.0

#: A PDF larger than this is not read.
MAX_PDF_BYTES = 64 * 1024 * 1024

#: The text of one PDF, or `None` when it cannot be read.
type Extract = Callable[[Path], str | None]


class PdfListingError(RuntimeError):
    """A folder of the vault could not be listed. Nothing is synced, so no
    PDF looks removed. The message holds no path."""


def list_pdfs(root: Path, folders: list[str]) -> list[str]:
    """The vault-relative paths of the PDFs in `folders` and their subfolders.

    The same rules as the Obsidian connector's notes: regular files only, no
    symlink, nothing whose name starts with a dot, no symlinked folder. The
    suffix is matched in any case."""

    def refuse(error: OSError) -> None:
        raise PdfListingError(f"a vault folder could not be listed ({type(error).__name__})")

    found: list[str] = []
    for folder in folders:
        for directory, subdirs, files in os.walk(root / folder, onerror=refuse):
            subdirs[:] = [name for name in subdirs if not name.startswith(".")]
            for name in files:
                path = Path(directory, name)
                if (
                    name.lower().endswith(PDF_SUFFIX)
                    and not name.startswith(".")
                    and not path.is_symlink()
                    and path.is_file()
                ):
                    found.append(path.relative_to(root).as_posix())

    return sorted(set(found))


def extract_in_child(path: Path) -> str | None:
    """The text of one PDF from `pdftext.py` in a child process.

    `-I` and an empty env: the child gets no key, no `PYTHONPATH` and no
    working folder on its import path."""
    try:
        if path.stat().st_size > MAX_PDF_BYTES:
            return None

        done = subprocess.run(
            [sys.executable, "-I", "-m", "creche_memory.pdftext", str(path)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env={},
            timeout=EXTRACT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    if done.returncode != 0:
        return None

    return done.stdout.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class _Seen:
    size: int
    mtime_ns: int
    text: str | None


@dataclass
class PdfTexts:
    """The text of each PDF, read once while its size and time stay the same."""

    extract: Extract = extract_in_child
    cut: int = 0
    _seen: dict[str, _Seen] = field(default_factory=lambda: {})

    def text(self, root: Path, rel: str) -> str | None:
        """The text of one PDF, cut at `MAX_PDF_CHARS`. `None`: unreadable."""
        try:
            info = (root / rel).lstat()
        except OSError:
            return None

        seen = self._seen.get(rel)
        if seen is not None and (seen.size, seen.mtime_ns) == (info.st_size, info.st_mtime_ns):
            return seen.text

        text = self.extract(root / rel)
        if text is not None and len(text) > MAX_PDF_CHARS:
            text = text[:MAX_PDF_CHARS]
            self.cut += 1

        self._seen[rel] = _Seen(info.st_size, info.st_mtime_ns, text)

        return text

    def warm(self, root: Path, folders: list[str]) -> None:
        """Read every new or changed PDF now. The sync calls it in a worker
        thread, so the store loop keeps answering searches."""
        for rel in list_pdfs(root, folders):
            self.text(root, rel)


def _row(rel: str, text: str) -> dict[str, Any]:
    folder = PurePosixPath(rel).parent
    ancestors = [
        {"kind": "folder", "id": parent.as_posix(), "name": parent.name}
        for parent in [folder, *folder.parents]
        if parent.parts
    ]
    row: dict[str, Any] = {
        "id": f"{DOCUMENT_ID_PREFIX}{rel}",
        "title": PurePosixPath(rel).stem,
        "content": text,
        "url": "",
        "_deleted": False,
    }
    if ancestors:
        row[STRUCTURE_COLUMN] = {"ancestors": ancestors}

    return row


def pdf_rows(
    root: Path, folders: list[str], state: dict[str, Any], texts: PdfTexts
) -> Iterator[dict[str, Any]]:
    """A row for each PDF with text, then a tombstone for each PDF that left.

    `state` is the sync state of the source. It holds the ids that the store
    holds, and it is written after the last row."""
    listed = list_pdfs(root, folders)
    previous = {str(one) for one in state.get("members", [])}
    present: set[str] = set()
    kept: set[str] = set()
    for rel in listed:
        doc_id = f"{DOCUMENT_ID_PREFIX}{rel}"
        text = texts.text(root, rel)
        if text is None:
            if doc_id in previous:
                kept.add(doc_id)
            continue

        if not text.strip():
            continue

        yield _row(rel, text)
        present.add(doc_id)

    for doc_id in sorted(previous - present - kept):
        yield {"id": doc_id, "_deleted": True}

    state.update(v=1, members=sorted(present | kept))


def pdf_source(root: Path, folders: list[str], texts: PdfTexts) -> Any:
    """The PDFs as a dlt resource for `cognee.remember`, built as the
    Obsidian connector builds its own. Call it after `configure`."""
    import dlt
    from cognee.tasks.ingestion import dlt_utils

    if dlt_utils.STRUCTURE_COLUMN != STRUCTURE_COLUMN:
        raise RuntimeError("Cognee renamed its structure column")

    @dlt.resource(
        name=RESOURCE,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def vault_pdfs() -> Iterator[dict[str, Any]]:
        yield from pdf_rows(root, folders, dlt.current.resource_state(), texts)

    resource = vault_pdfs()
    setattr(resource, dlt_utils.DOCUMENT_SOURCE_ATTR, SOURCE_TAG)
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, RESOURCE)

    return resource
