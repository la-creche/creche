"""The client of the scan link (design note attachments §4).

The vmm serves the scanner socket. It writes the kind line `scan` on each
guest link and joins the link to the facade of the scanner VM. attendance
opens one link for each file:

    attendance --{"name", "mime_type", "data"}\\n--> scanner socket (vmm)
               <--{"ok": {"files": [...]}}\\n------  scanner VM
               <--{"refused": {"reason": ...}}\\n--

Each byte of the reply is a claim of another process. The reply is one
line of `LINE_CAP` bytes or less, strict JSON of one shape. Each other
reply, a link that ends with no reply and a reply after `WAIT_S` give the
reason `failed`. A link that the vmm closes before it takes the whole
request gives `busy`: the vmm accepts a link past its cap and closes it at
once. The scan never raises: it returns a result.

`test_scanner.py` holds each constant equal to
`rust/crates/wire/vectors/scan.json`.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from .acp_link import Malformed, loads_strict
from .atomic import as_array, as_object
from .lines import LineSplitter

#: The cap of one line of the link, in each direction.
LINE_CAP: Final = 16_777_216

#: How long attendance waits for the reply of one file, in seconds.
WAIT_S: Final = 300

#: The caps of the files of one message.
MAX_FILES: Final = 4
MAX_FILE_BYTES: Final = 8_388_608
MAX_MESSAGE_BYTES: Final = 10_485_760

#: The `uri` of a file block is this scheme and the file name.
URI_SCHEME: Final = "attachment:"
MAX_NAME_BYTES: Final = 255

#: The claimed types of each kind.
IMAGE_TYPES: Final = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
PDF_TYPES: Final = frozenset({"application/pdf"})
TEXT_TYPES: Final = frozenset(
    {
        "text/plain",
        "text/markdown",
        "text/csv",
        "text/html",
        "text/css",
        "text/javascript",
        "text/x-python",
        "text/x-rust",
        "text/x-go",
        "text/x-c",
        "text/x-c++",
        "text/x-java",
        "text/x-typescript",
        "text/x-shellscript",
        "application/json",
        "application/xml",
        "application/yaml",
        "application/toml",
        "application/javascript",
        "application/typescript",
        "application/x-sh",
        "application/sql",
    }
)
ALL_TYPES: Final = IMAGE_TYPES | PDF_TYPES | TEXT_TYPES

#: The most rebuilt files of one file: a PDF and its `<name>.txt` (design
#: note attachments §5 step 4).
MAX_REBUILT_FILES: Final = 2

#: The name end and the type of the text file of a rebuilt PDF.
PDF_TEXT_SUFFIX: Final = ".txt"
PDF_TEXT_TYPE: Final = "text/plain"

_READ_CHUNK_BYTES: Final = 65_536
_LF: Final = b"\n"
_DOT_NAMES: Final = frozenset({".", ".."})
_SLASH: Final = "/"
_CONTROL_CATEGORY: Final = "Cc"

_REQUEST_KEYS: Final = ("name", "mime_type", "data")
_OK_KEY: Final = "ok"
_REFUSED_KEY: Final = "refused"


class Reason(StrEnum):
    """Why the scan gave no rebuilt file (design note attachments §4)."""

    NOT_ALLOWED = "not_allowed"
    MISMATCH = "mismatch"
    MALWARE = "malware"
    TOO_LARGE = "too_large"
    UNREADABLE = "unreadable"
    NOT_UTF8 = "not_utf8"
    BUSY = "busy"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ScanFile:
    """One file: its name, its claimed type and its bytes."""

    name: str
    mime_type: str
    data: bytes


@dataclass(frozen=True, slots=True)
class Rebuilt:
    """The files that the scanner made from one file."""

    files: tuple[ScanFile, ...]


@dataclass(frozen=True, slots=True)
class Refused:
    """The scan refused the file."""

    reason: Reason


type ScanResult = Rebuilt | Refused


def is_file_name(name: str) -> bool:
    """The name rule (design note attachments §3 rule 3): 1 to
    `MAX_NAME_BYTES` bytes of UTF-8, no `/`, no NUL, no control character,
    and not `.` or `..`. NUL is a control character."""
    try:
        size = len(name.encode("utf-8"))
    except UnicodeEncodeError:
        return False

    if not 0 < size <= MAX_NAME_BYTES or name in _DOT_NAMES or _SLASH in name:
        return False

    return not any(unicodedata.category(mark) == _CONTROL_CATEGORY for mark in name)


def decode_base64(text: str) -> bytes | None:
    """The bytes of strict base64 text, or None."""
    try:
        return binascii.a2b_base64(text, strict_mode=True)
    except ValueError:
        # `binascii.Error` is a `ValueError`. A text that is not ASCII
        # raises a plain one.
        return None


def least_decoded_bytes(text: str) -> int:
    """The fewest bytes that base64 text of this length decodes to. A caller
    refuses a text over its cap with this count, before the decode."""
    return max(len(text) // 4 * 3 - 2, 0)


class Scanner:
    """The scan link of one scanner socket."""

    def __init__(self, socket: Path, wait_s: float = WAIT_S) -> None:
        self._socket = socket
        self._wait_s = wait_s

    async def scan(self, file: ScanFile) -> ScanResult:
        """Scan one file over one new link. Past `wait_s` the link closes,
        and the reason is `failed`."""
        try:
            async with asyncio.timeout(self._wait_s):
                return await self._exchange(file)
        except TimeoutError:
            return Refused(Reason.FAILED)

    async def _exchange(self, file: ScanFile) -> ScanResult:
        try:
            reader, writer = await asyncio.open_unix_connection(self._socket)
        except OSError:
            return Refused(Reason.FAILED)

        try:
            written = await _send(writer, _request_line(file))
            reply = await _reply_line(reader)
        finally:
            # The reply is in, or the link failed. No byte is left to send.
            writer.transport.abort()

            with contextlib.suppress(OSError):
                await writer.wait_closed()

        if reply is None:
            return Refused(Reason.FAILED if written else Reason.BUSY)

        return read_reply(reply)


def _request_line(file: ScanFile) -> bytes:
    data = base64.b64encode(file.data).decode("ascii")
    request = dict(zip(_REQUEST_KEYS, (file.name, file.mime_type, data), strict=True))
    return json.dumps(request, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + _LF


async def _send(writer: asyncio.StreamWriter, line: bytes) -> bool:
    """Write the request. False when the link ended before it took the
    whole line."""
    try:
        writer.write(line)
        await writer.drain()
    except OSError:
        return False

    return True


async def _reply_line(reader: asyncio.StreamReader) -> str | None:
    """The first line of the reply, without its LF. None for a link that
    ends first, and for a line over `LINE_CAP` or not UTF-8."""
    splitter = LineSplitter(LINE_CAP)

    while True:
        try:
            chunk = await reader.read(_READ_CHUNK_BYTES)
        except OSError:
            return None

        if not chunk:
            return None

        lines = splitter.feed(chunk)

        if lines:
            return lines[0].text


def read_reply(text: str) -> ScanResult:
    """The result of one reply line. Each other shape is `failed`."""
    try:
        reply = as_object(loads_strict(text))
    except Malformed:
        return Refused(Reason.FAILED)

    if reply is None or len(reply) != 1:
        return Refused(Reason.FAILED)

    if _REFUSED_KEY in reply:
        return _refused(reply[_REFUSED_KEY])

    if _OK_KEY in reply:
        return _rebuilt(reply[_OK_KEY])

    return Refused(Reason.FAILED)


def _refused(value: object) -> ScanResult:
    refused = as_object(value)
    reason = refused.get("reason") if refused is not None and len(refused) == 1 else None

    if not isinstance(reason, str) or reason not in Reason:
        return Refused(Reason.FAILED)

    return Refused(Reason(reason))


def _rebuilt(value: object) -> ScanResult:
    ok = as_object(value)
    listed = as_array(ok.get("files")) if ok is not None and len(ok) == 1 else None

    if listed is None or not 0 < len(listed) <= MAX_REBUILT_FILES:
        return Refused(Reason.FAILED)

    files = [_rebuilt_file(one) for one in listed]
    kept = tuple(one for one in files if one is not None)

    if len(kept) != len(files) or not _is_one_file(kept):
        return Refused(Reason.FAILED)

    return Rebuilt(kept)


def _is_one_file(files: tuple[ScanFile, ...]) -> bool:
    """True when `files` count as one file: one file, or a PDF and its
    `<name>.txt` as `text/plain` (design note attachments §5 step 4). So the
    rebuilt files of a message are `MAX_FILES` or less, as the facade counts
    them."""
    if len(files) == 1:
        return True

    pdf, text = files
    return pdf.mime_type in PDF_TYPES and text == ScanFile(
        pdf.name + PDF_TEXT_SUFFIX, PDF_TEXT_TYPE, text.data
    )


def _rebuilt_file(value: object) -> ScanFile | None:
    """One rebuilt file: exactly the three keys, a name of the name rule, a
    type of `kinds` and strict base64."""
    fields: dict[str, Any] | None = as_object(value)

    if fields is None or set(fields) != set(_REQUEST_KEYS):
        return None

    name, mime_type, text = (fields[key] for key in _REQUEST_KEYS)

    if not isinstance(name, str) or not is_file_name(name):
        return None

    if not isinstance(mime_type, str) or mime_type not in ALL_TYPES or not isinstance(text, str):
        return None

    # The line cap bounds the text, so the decode is bounded too.
    data = decode_base64(text)
    return None if data is None else ScanFile(name, mime_type, data)
