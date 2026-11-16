"""The scan link (design note attachments §4) and its shared numbers.

`rust/crates/wire/vectors/scan.json` holds every number and every list of
the design note. Each constant here equals it.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest
from attendance.acp_door import MAX_FRAME_BYTES
from attendance.acp_link import MAX_ACP_LINE_BYTES
from attendance.scanner import (
    Reason,
    Rebuilt,
    Refused,
    ScanFile,
    Scanner,
    is_file_name,
    read_reply,
)
from fake_scanner import PDF_TEXT, ScanMode, serve_scanner

from attendance import scanner

SCAN_VECTORS = Path(__file__).parents[2] / "rust" / "crates" / "wire" / "vectors" / "scan.json"

#: A file of the vectors.
NOTES = ScanFile("notes.txt", "text/plain", b"hello\n")

#: A file larger than each socket buffer, so a link that closes at once
#: breaks the write.
LARGE = ScanFile("large.txt", "text/plain", b"a" * 4_194_304)

#: How long a test waits for a reply that never comes.
SHORT_WAIT_S = 0.3


def _vectors() -> dict[str, Any]:
    return json.loads(SCAN_VECTORS.read_text(encoding="utf-8"))


def test_the_constants_equal_scan_json() -> None:
    vectors = _vectors()

    assert vectors["line_cap"] == scanner.LINE_CAP
    assert vectors["wait_s"] == scanner.WAIT_S
    assert vectors["max_files"] == scanner.MAX_FILES
    assert vectors["max_file_bytes"] == scanner.MAX_FILE_BYTES
    assert vectors["max_message_bytes"] == scanner.MAX_MESSAGE_BYTES
    assert vectors["uri_scheme"] == scanner.URI_SCHEME
    assert vectors["max_name_bytes"] == scanner.MAX_NAME_BYTES
    assert frozenset(vectors["kinds"]["image"]) == scanner.IMAGE_TYPES
    assert frozenset(vectors["kinds"]["pdf"]) == scanner.PDF_TYPES
    assert frozenset(vectors["kinds"]["text"]) == scanner.TEXT_TYPES
    assert [one.value for one in Reason] == vectors["reasons"]


def test_the_acp_line_cap_equals_scan_json() -> None:
    assert _vectors()["acp_line_cap"] == MAX_ACP_LINE_BYTES
    assert MAX_FRAME_BYTES == MAX_ACP_LINE_BYTES


def test_the_vector_replies_read() -> None:
    vectors = _vectors()
    request = vectors["request"]
    file = ScanFile(request["name"], request["mime_type"], base64.b64decode(request["data"]))

    assert read_reply(json.dumps(vectors["reply_ok"])) == Rebuilt((file,))
    assert read_reply(json.dumps(vectors["reply_refused"])) == Refused(Reason.MISMATCH)


@pytest.mark.parametrize(
    "name",
    ["notes.txt", "a", "é" * 127, "x" * 255, "a b.png", ".hidden", "..."],
)
def test_a_good_name_passes(name: str) -> None:
    assert is_file_name(name)


@pytest.mark.parametrize(
    "name",
    ["", ".", "..", "a/b", "a\x00b", "a\x1fb", "a\x7fb", "a\x85b", "x" * 256, "é" * 128, "\ud800"],
    ids=[
        "empty",
        "dot",
        "dot-dot",
        "slash",
        "nul",
        "unit-separator",
        "delete",
        "c1-control",
        "256-bytes",
        "256-bytes-utf8",
        "half-pair",
    ],
)
def test_a_bad_name_is_refused(name: str) -> None:
    assert not is_file_name(name)


async def test_the_request_is_one_line_of_the_vector_shape() -> None:
    async with serve_scanner() as fake:
        result = await Scanner(fake.socket).scan(NOTES)

    assert fake.requests == [_vectors()["request"]]
    assert result == Rebuilt((NOTES,))


async def test_a_pdf_gives_two_files() -> None:
    pdf = ScanFile("doc.pdf", "application/pdf", b"%PDF-1.7 pages")

    async with serve_scanner() as fake:
        result = await Scanner(fake.socket).scan(pdf)

    assert result == Rebuilt((pdf, ScanFile("doc.pdf.txt", "text/plain", PDF_TEXT)))


@pytest.mark.parametrize("reason", list(Reason))
async def test_each_reason_of_a_refusal_reads(reason: Reason) -> None:
    async with serve_scanner() as fake:
        fake.mode, fake.reason = ScanMode.REFUSE, reason.value
        result = await Scanner(fake.socket).scan(NOTES)

    assert result == Refused(reason)


async def test_a_link_closed_at_once_is_busy() -> None:
    async with serve_scanner() as fake:
        fake.mode = ScanMode.CLOSE
        result = await Scanner(fake.socket).scan(LARGE)

    assert result == Refused(Reason.BUSY)


@pytest.mark.parametrize("file", [NOTES, LARGE], ids=["short", "large"])
async def test_the_busy_line_of_the_link_cap_is_busy(file: ScanFile) -> None:
    """The vmm writes the busy refusal on a link past its cap, then closes
    it before it reads the request."""
    async with serve_scanner() as fake:
        fake.mode = ScanMode.CAP_LINE
        result = await Scanner(fake.socket).scan(file)

    assert result == Refused(Reason.BUSY)


async def test_a_link_with_no_reply_failed() -> None:
    async with serve_scanner() as fake:
        fake.mode = ScanMode.SILENT
        result = await Scanner(fake.socket).scan(NOTES)

    assert result == Refused(Reason.FAILED)


async def test_a_reply_past_the_wait_failed() -> None:
    async with serve_scanner() as fake:
        fake.mode = ScanMode.HANG
        result = await Scanner(fake.socket, SHORT_WAIT_S).scan(NOTES)

    assert result == Refused(Reason.FAILED)


async def test_a_missing_socket_failed(tmp_path: Path) -> None:
    result = await Scanner(tmp_path / "none.sock").scan(NOTES)

    assert result == Refused(Reason.FAILED)


def _ok(*files: dict[str, Any]) -> dict[str, Any]:
    return {"ok": {"files": list(files)}}


_GOOD = {"name": "a.png", "mime_type": "image/png", "data": "AAAA"}
_PDF = {"name": "a.pdf", "mime_type": "application/pdf", "data": "AAAA"}
_PDF_TEXT = {"name": "a.pdf.txt", "mime_type": "text/plain", "data": "AAAA"}


@pytest.mark.parametrize(
    "reply",
    [
        b"not json",
        b"[]",
        b'{"ok": {"files": []}, "refused": {"reason": "malware"}}',
        b'{"other": {}}',
        b'{"refused": {"reason": "unknown"}}',
        b'{"refused": {"reason": "malware", "more": 1}}',
        b'{"refused": "malware"}',
        b'{"ok": {"files": [], "more": 1}}',
        json.dumps(_ok()).encode(),
        json.dumps(_ok(_GOOD, _GOOD, _GOOD)).encode(),
        json.dumps(_ok(_GOOD, _PDF_TEXT)).encode(),
        json.dumps(_ok(_PDF, {**_PDF_TEXT, "name": "b.pdf.txt"})).encode(),
        json.dumps(_ok(_PDF, {**_PDF_TEXT, "mime_type": "text/markdown"})).encode(),
        json.dumps(_ok(_PDF_TEXT, _PDF)).encode(),
        json.dumps(_ok({**_GOOD, "more": 1})).encode(),
        json.dumps(_ok({"name": "a.png", "mime_type": "image/png"})).encode(),
        json.dumps(_ok({**_GOOD, "name": "../a.png"})).encode(),
        json.dumps(_ok({**_GOOD, "name": 1})).encode(),
        json.dumps(_ok({**_GOOD, "mime_type": "video/mp4"})).encode(),
        json.dumps(_ok({**_GOOD, "data": "AAA"})).encode(),
        json.dumps(_ok({**_GOOD, "data": "AA AA"})).encode(),
        json.dumps(_ok({**_GOOD, "data": "AAAA===="})).encode(),
        json.dumps(_ok({**_GOOD, "data": 1})).encode(),
        b'{"refused": {"reason": "malware", "reason": "busy"}}',
        b"\xff\xfe",
        b"",
    ],
    ids=[
        "not-json",
        "array",
        "two-keys",
        "other-key",
        "unknown-reason",
        "refused-more",
        "refused-text",
        "ok-more",
        "no-file",
        "three-files",
        "image-and-text",
        "pdf-and-other-name",
        "pdf-and-other-type",
        "text-before-pdf",
        "file-more",
        "file-no-data",
        "file-bad-name",
        "file-name-number",
        "file-not-a-kind",
        "data-bad-padding",
        "data-space",
        "data-past-padding",
        "data-number",
        "key-twice",
        "not-utf8",
        "empty-line",
    ],
)
async def test_a_bad_reply_failed(reply: bytes) -> None:
    async with serve_scanner() as fake:
        fake.mode, fake.raw = ScanMode.RAW, reply + b"\n"
        result = await Scanner(fake.socket).scan(NOTES)

    assert result == Refused(Reason.FAILED)


async def test_a_reply_over_the_line_cap_failed() -> None:
    async with serve_scanner() as fake:
        fake.mode, fake.raw = ScanMode.RAW, b"a" * (scanner.LINE_CAP + 1) + b"\n"
        result = await Scanner(fake.socket).scan(NOTES)

    assert result == Refused(Reason.FAILED)


async def test_a_reply_with_no_line_end_failed() -> None:
    async with serve_scanner() as fake:
        fake.mode, fake.raw = ScanMode.RAW, json.dumps(_ok(_GOOD)).encode()
        result = await Scanner(fake.socket).scan(NOTES)

    assert result == Refused(Reason.FAILED)
