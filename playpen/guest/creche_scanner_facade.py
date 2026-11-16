#!/opt/scanner/bin/python -I
"""creche-scanner-facade: the facade of the scanner service image.

The scanner VM checks and rebuilds each file of a prompt before any family
VM gets it (attachments design note, sections 4 and 5). Guest init treats
this program as it treats the browser facade: it starts it as root at
`creche_wire::guest::FACADE_PROGRAM`, restarts it after an exit, and stops
it with the stop line of the local link. It splices each link of guest
port 1201 to `RELAY_SOCKET`, byte for byte.

    attendance --> vmm --vsock 1201--> guest init --RELAY_SOCKET--> this program
                                                                       |
        line 1 "scan"                                                  |
        line 2 {"name", "mime_type", "data"}   1 the kind of mime_type |
        reply  {"ok": {"files": [...]}}        2 Magika: the true type |
            or {"refused": {"reason"}}         3 clamd: known malware  |
                                               4 the rebuild, in a     |
                                                 child with limits  <--+

1. The program opens the local link at once, so guest init marks it
   `running`. It sends no line on it. The stop line `{"stop":{}}`, or the
   end of the link, ends the program, clamd and every child.
2. At its start the program starts `clamd` with a config of its own on
   the guest tmpfs. The signatures came with the image (freshclam at
   build time), and the VM has no network device. A link waits until
   clamd answers. A clamd that stops, or that does not answer in time,
   ends the program, and guest init starts it again.
3. Each relay link starts with one line that names the link: `scan`.
   Any other line closes the link with no reply. The next line is the
   request, and the program answers one line, then closes the link.
4. Each limit and each list is a constant here, equal to
   `rust/crates/wire/vectors/scan.json`. A playpen test holds them equal.

The program checks the bytes only. It does not look for prompt injection
(design note, section 5).
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import importlib
import io
import json
import math
import os
import pwd
import re
import resource
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import warnings
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, BinaryIO, Final, Protocol, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

#: `creche_wire::guest::LOCAL_LINK_SOCKET`. A playpen test holds them equal.
LOCAL_LINK_SOCKET: Final = "/run/creche/guest-init.sock"

#: `creche_wire::guest::RELAY_SOCKET`. A playpen test holds them equal.
RELAY_SOCKET: Final = "/tmp/creche-facade/acp.sock"

#: `creche_wire::guest::SESSION_ID_ENV`: guest init sets it.
SESSION_ID_ENV: Final = "CRECHE_SESSION_ID"

#: The exit status with no session id, as `creche-facade` has.
EXIT_NO_SESSION: Final = 17

#: The exit status when clamd stops or does not answer at the start.
EXIT_NO_CLAMD: Final = 1

#: The stop line of the local link, as guest init writes it.
STOP_LINE: Final[dict[str, dict[str, object]]] = {"stop": {}}

# --- The scan link: each value equals scan.json ------------------------------

#: The line that the vmm writes first on each link, LF not included.
KIND_LINE: Final = "scan"

#: The most bytes of one line in each direction, LF included.
#: CONTRACT-QUESTION: design section 4 does not say if the LF counts. The
#: reading taken: it counts, the stricter one. A change costs this one
#: constant here and its copy in each package of the link.
LINE_CAP: Final = 16777216

#: How long attendance waits for a reply. Also the wait for each read.
WAIT_S: Final = 300

#: The most bytes of one file, after the base64 decode.
MAX_FILE_BYTES: Final = 8388608

#: The most pages of one PDF.
MAX_PDF_PAGES: Final = 50

#: The most pixels of one image, and of one PDF page at `PDF_DPI`.
MAX_IMAGE_PIXELS: Final = 40000000

#: The most bytes of one file name, as UTF-8.
MAX_NAME_BYTES: Final = 255

KIND_IMAGE: Final = "image"
KIND_PDF: Final = "pdf"
KIND_TEXT: Final = "text"

#: The claimed types of each kind. A type in no kind is `not_allowed`.
KINDS: Final[dict[str, tuple[str, ...]]] = {
    KIND_IMAGE: ("image/png", "image/jpeg", "image/gif", "image/webp"),
    KIND_PDF: ("application/pdf",),
    KIND_TEXT: (
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
    ),
}

NOT_ALLOWED: Final = "not_allowed"
MISMATCH: Final = "mismatch"
MALWARE: Final = "malware"
TOO_LARGE: Final = "too_large"
UNREADABLE: Final = "unreadable"
NOT_UTF8: Final = "not_utf8"
BUSY: Final = "busy"
FAILED: Final = "failed"

#: Every reason of a refusal, in the order of scan.json.
REASONS: Final = (NOT_ALLOWED, MISMATCH, MALWARE, TOO_LARGE, UNREADABLE, NOT_UTF8, BUSY, FAILED)

# --- The links of this program ----------------------------------------------

#: The kind line, LF included, at most this many bytes, as the browser has.
LINK_LINE_CAP: Final = 16

#: How long a relay link may take to send its kind line.
LINK_LINE_WAIT: Final = 10.0

#: Open links. A link past the cap gets `busy` at once.
LINK_CAP: Final = 8

#: Scans that run at the same time. The others wait for a slot. One scan
#: holds the request, its children and their limits, and clamd holds about
#: 1.3 GiB of the 3 GiB of the VM.
SCAN_SLOTS: Final = 1

# --- Layer 1: Magika -----------------------------------------------------------

#: The Magika labels of each kind, other than text. A text file is any
#: label that Magika marks `is_text`.
IMAGE_LABELS: Final = ("png", "jpeg", "gif", "webp")
PDF_LABELS: Final = ("pdf",)

# --- Layer 2: clamd ------------------------------------------------------------

#: The binary of the Debian package `clamav-daemon`, and the user it runs as.
CLAMD: Final = "/usr/sbin/clamd"
CLAMD_USER: Final = "clamav"

#: The run dir of clamd on the guest tmpfs: its config and its socket.
CLAMD_DIR: Final = Path("/run/creche-scanner")
CLAMD_CONFIG: Final = CLAMD_DIR / "clamd.conf"
CLAMD_SOCKET: Final = CLAMD_DIR / "clamd.sock"

#: Where freshclam put the signatures at image build time.
CLAMD_DATABASE: Final = "/var/lib/clamav"

#: How long clamd may take to load its signatures at the start.
CLAMD_START_WAIT_S: Final = 300.0

#: How long one clamd command may take.
CLAMD_WAIT_S: Final = 120.0

#: The bytes of one INSTREAM chunk, and the most bytes of one reply.
CLAMD_CHUNK: Final = 65536
CLAMD_REPLY_CAP: Final = 4096

#: The reply of clean bytes, and the end of a reply that found a signature.
CLAMD_CLEAN: Final = b"stream: OK"
CLAMD_FOUND: Final = b" FOUND"

# --- Layer 3: the rebuild ---------------------------------------------------------

#: The address space of each child, and how long it may run.
CHILD_MEMORY_BYTES: Final = 1 << 30
CHILD_WAIT_S: Final = 120.0

#: The env of each child. No other name reaches it.
CHILD_ENV: Final = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}

#: The exit status of a child for each refusal. Any other status, a
#: signal and a timeout are `unreadable`.
CHILD_TOO_LARGE: Final = 10
CHILD_UNREADABLE: Final = 11

#: The subcommands of this program that run in a child.
CHILD_LIMIT: Final = "limit"
CHILD_IMAGE: Final = "image"
CHILD_PAGES: Final = "pages"

#: The Pillow formats that an image may decode from.
IMAGE_FORMATS: Final = ("PNG", "JPEG", "GIF", "WEBP")

#: The JPEG quality of a rebuilt JPEG.
JPEG_QUALITY: Final = 90

#: The rebuilt types, and the name ends that each keeps.
PNG_TYPE: Final = "image/png"
JPEG_TYPE: Final = "image/jpeg"
PDF_TYPE: Final = "application/pdf"
TEXT_TYPE: Final = "text/plain"
PNG_ENDS: Final = (".png",)
JPEG_ENDS: Final = (".jpg", ".jpeg")
TEXT_END: Final = ".txt"

#: The poppler tools, the render resolution, and the PDF point.
PDFINFO: Final = "/usr/bin/pdfinfo"
PDFTOPPM: Final = "/usr/bin/pdftoppm"
PDFTOTEXT: Final = "/usr/bin/pdftotext"
PDF_DPI: Final = 150
PDF_POINTS_PER_INCH: Final = 72

#: `Pages:          3`, a line of pdfinfo.
PAGES_RE: Final = re.compile(r"Pages:\s+(\d+)")

#: `Page    1 size: 612 x 792 pts (letter)`, a line of `pdfinfo -f -l`.
PAGE_SIZE_RE: Final = re.compile(r"Page\s+\d+ size:\s+([0-9.]+) x ([0-9.]+) pts.*")


def say(text: str) -> None:
    """Write one line to the console, which is the stderr of this program."""
    sys.stderr.write(f"scanner-facade: {text}\n")
    sys.stderr.flush()


class Refused(Exception):
    """A scan that ends with one reason of `REASONS`."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class File:
    """One file of the link: a request, or one rebuilt file of a reply."""

    name: str
    mime_type: str
    data: bytes


# --- The lines ---------------------------------------------------------------------


def link_kind(line: bytes) -> str | None:
    """The kind that the first line of a relay link names, or None.

    The line is exactly `scan`, ended by one LF. Anything else, a CR
    included, names no kind.
    """
    if len(line) > LINK_LINE_CAP or line != f"{KIND_LINE}\n".encode():
        return None

    return KIND_LINE


def is_stop(line: bytes) -> bool:
    """True for the stop line of the local link, `{"stop":{}}`."""
    try:
        return json.loads(line) == STOP_LINE
    except ValueError:
        return False


def read_kind_line(conn: socket.socket) -> bytes:
    """The first line of a link, at most `LINK_LINE_CAP` bytes.

    It reads one byte at a time, so no byte of the request leaves the
    socket.
    """
    conn.settimeout(LINK_LINE_WAIT)
    line = b""
    while len(line) < LINK_LINE_CAP and not line.endswith(b"\n"):
        byte = conn.recv(1)
        if not byte:
            break
        line += byte

    return line


def read_request(conn: socket.socket) -> bytes:
    """The request line, LF included, or `Refused(failed)`.

    A line with no LF before the end of the link, or one past `LINE_CAP`,
    is `failed`. Each read waits at most `WAIT_S`.
    """
    conn.settimeout(WAIT_S)
    line = bytearray()
    while not line.endswith(b"\n"):
        chunk = conn.recv(min(65536, LINE_CAP + 1 - len(line)))
        if not chunk:
            raise Refused(FAILED)
        line += chunk
        if len(line) > LINE_CAP:
            raise Refused(FAILED)
        if b"\n" in chunk and not line.endswith(b"\n"):
            raise Refused(FAILED)

    return bytes(line)


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """A JSON object with no key two times."""
    found = dict(pairs)
    if len(found) != len(pairs):
        raise ValueError("a key two times")
    return found


def valid_name(name: str) -> bool:
    """The name rule of design section 3.3.

    255 bytes or less as UTF-8, not empty, no `/`, no NUL, no control
    character, and not `.` or `..`.
    """
    if not name or name in {".", ".."} or "/" in name:
        return False

    if any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F for char in name):
        return False

    return len(name.encode()) <= MAX_NAME_BYTES


#: The most base64 characters of a file of `MAX_FILE_BYTES`.
MAX_DATA_CHARS: Final = 4 * math.ceil(MAX_FILE_BYTES / 3)


def parse_request(line: bytes) -> File:
    """The request of one line, or a refusal.

    The line is one JSON object with exactly `name`, `mime_type` and
    `data`, each a string. Any other line is `failed`. Data past
    `MAX_FILE_BYTES` is `too_large`.
    """
    if len(line) > LINE_CAP or not line.endswith(b"\n"):
        raise Refused(FAILED)

    try:
        value: object = json.loads(line, object_pairs_hook=_unique_keys)
    except ValueError as error:
        raise Refused(FAILED) from error

    if not isinstance(value, dict):
        raise Refused(FAILED)

    fields = cast("dict[str, object]", value)
    if set(fields) != {"name", "mime_type", "data"}:
        raise Refused(FAILED)

    name, mime_type, data = fields["name"], fields["mime_type"], fields["data"]
    if not isinstance(name, str) or not isinstance(mime_type, str) or not isinstance(data, str):
        raise Refused(FAILED)

    if not valid_name(name):
        raise Refused(FAILED)

    if len(data) > MAX_DATA_CHARS:
        raise Refused(TOO_LARGE)

    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as error:
        raise Refused(FAILED) from error

    if len(raw) > MAX_FILE_BYTES:
        raise Refused(TOO_LARGE)

    return File(name, mime_type, raw)


def _line(reply: dict[str, object]) -> bytes:
    return json.dumps(reply, separators=(",", ":")).encode() + b"\n"


def refused_line(reason: str) -> bytes:
    """The reply line of a refusal."""
    return _line({"refused": {"reason": reason}})


def ok_line(files: Sequence[File]) -> bytes:
    """The reply line of the rebuilt files. A line past `LINE_CAP` is
    `too_large`."""
    listed = [
        {
            "name": one.name,
            "mime_type": one.mime_type,
            "data": base64.b64encode(one.data).decode("ascii"),
        }
        for one in files
    ]
    line = _line({"ok": {"files": listed}})
    if len(line) > LINE_CAP:
        raise Refused(TOO_LARGE)

    return line


def kind_of(mime_type: str) -> str:
    """The kind of a claimed type, or `Refused(not_allowed)`.

    CONTRACT-QUESTION: the design note does not say how a type matches.
    The reading taken: exactly, so a parameter such as `; charset=utf-8`
    and another case name no kind. A change costs one normalize step here
    and in attendance.
    """
    for kind, types in KINDS.items():
        if mime_type in types:
            return kind

    raise Refused(NOT_ALLOWED)


# --- The libraries -------------------------------------------------------------
# Magika and Pillow are in the image only. Each loads by name, and these
# protocols name the few members that this program uses.


class _MagikaOutput(Protocol):
    @property
    def label(self) -> object: ...

    @property
    def is_text(self) -> bool: ...


class _MagikaResult(Protocol):
    @property
    def ok(self) -> bool: ...

    @property
    def output(self) -> _MagikaOutput: ...


class _MagikaEngine(Protocol):
    def identify_bytes(self, content: bytes) -> _MagikaResult: ...


class _Picture(Protocol):
    """The part of a `PIL.Image.Image` that the rebuild uses."""

    @property
    def format(self) -> str | None: ...

    @property
    def mode(self) -> str: ...

    @property
    def size(self) -> tuple[int, int]: ...

    @property
    def info(self) -> dict[str, object]: ...

    def seek(self, frame: int) -> None: ...

    def load(self) -> object: ...

    def convert(self, mode: str) -> _Picture: ...

    def tobytes(self) -> bytes: ...

    def save(self, fp: BinaryIO, format: str, **params: object) -> None: ...


def _library(name: str) -> ModuleType:
    """One module of the image, by name."""
    return importlib.import_module(name)


# --- Layer 1: the true type ---------------------------------------------------------


class Typer:
    """Magika, loaded once on first use. The model came in its wheel."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.engine: _MagikaEngine | None = None

    def load(self) -> _MagikaEngine:
        with self.lock:
            if self.engine is None:
                magika = cast("Callable[[], _MagikaEngine]", _library("magika").Magika)
                self.engine = magika()
            return self.engine

    def check(self, kind: str, data: bytes) -> None:
        """Refuses `mismatch` when Magika reads another kind in `data`."""
        result = self.load().identify_bytes(data)
        if not result.ok:
            raise Refused(FAILED)

        label = str(result.output.label)
        if kind == KIND_IMAGE and label in IMAGE_LABELS:
            return
        if kind == KIND_PDF and label in PDF_LABELS:
            return
        if kind == KIND_TEXT and result.output.is_text:
            return

        raise Refused(MISMATCH)


# --- Layer 2: known malware -----------------------------------------------------------


class Clamd:
    """A client of clamd on its Unix socket: `zPING` and `zINSTREAM`."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _command(self, command: bytes, body: bytes | None) -> bytes:
        """Sends one command and reads its reply up to the NUL.

        INSTREAM sends `body` as chunks, each with its length as 4 bytes
        big endian, then a length of zero.
        """
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(CLAMD_WAIT_S)
            conn.connect(str(self.path))
            conn.sendall(b"z" + command + b"\0")
            if body is not None:
                for start in range(0, len(body), CLAMD_CHUNK):
                    piece = body[start : start + CLAMD_CHUNK]
                    conn.sendall(struct.pack(">I", len(piece)) + piece)
                conn.sendall(struct.pack(">I", 0))

            reply = b""
            while b"\0" not in reply and len(reply) < CLAMD_REPLY_CAP:
                chunk = conn.recv(CLAMD_REPLY_CAP)
                if not chunk:
                    break
                reply += chunk

        return reply.split(b"\0", 1)[0]

    def ping(self) -> bool:
        """True when clamd answers `PONG`."""
        try:
            return self._command(b"PING", None) == b"PONG"
        except OSError:
            return False

    def check(self, data: bytes) -> None:
        """Refuses `malware` on a signature, `failed` on any other reply."""
        try:
            reply = self._command(b"INSTREAM", data)
        except OSError as error:
            raise Refused(FAILED) from error

        if reply == CLAMD_CLEAN:
            return
        if reply.startswith(b"stream: ") and reply.endswith(CLAMD_FOUND):
            raise Refused(MALWARE)

        raise Refused(FAILED)


def clamd_config() -> str:
    """The config of clamd: the socket of this program and no log file."""
    return "".join(
        f"{key} {value}\n"
        for key, value in (
            ("LocalSocket", CLAMD_SOCKET),
            ("LocalSocketMode", "600"),
            ("User", CLAMD_USER),
            ("Foreground", "yes"),
            ("DatabaseDirectory", CLAMD_DATABASE),
            ("TemporaryDirectory", "/tmp"),
            ("StreamMaxLength", MAX_FILE_BYTES),
            ("MaxThreads", 2),
            ("SelfCheck", 0),
            ("ConcurrentDatabaseReload", "no"),
            ("LogSyslog", "no"),
        )
    )


def start_clamd() -> subprocess.Popen[bytes]:
    """Writes the config in a dir of the clamd user, then starts clamd."""
    user = pwd.getpwnam(CLAMD_USER)
    CLAMD_DIR.mkdir(mode=0o750, parents=True, exist_ok=True)
    os.chown(CLAMD_DIR, user.pw_uid, user.pw_gid)
    CLAMD_SOCKET.unlink(missing_ok=True)
    CLAMD_CONFIG.write_text(clamd_config(), encoding="utf-8")
    return subprocess.Popen(
        [CLAMD, f"--config-file={CLAMD_CONFIG}", "--foreground"],
        stdin=subprocess.DEVNULL,
        env=CHILD_ENV,
    )


# --- Layer 3: the rebuild ---------------------------------------------------------------


def run_child(argv: Sequence[str], data: bytes) -> bytes:
    """Runs one rebuild step in a child under `CHILD_MEMORY_BYTES`.

    The child is this program, which sets the limit and then runs the
    step: `limit <argv>` for a tool, or a subcommand of its own. `data`
    goes to its stdin, and its stdout comes back. A status of
    `CHILD_TOO_LARGE` is `too_large`. Each other failure is `unreadable`.
    """
    try:
        done = subprocess.run(
            [sys.executable, "-I", os.path.abspath(__file__), *argv],
            input=data,
            capture_output=True,
            env=CHILD_ENV,
            timeout=CHILD_WAIT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise Refused(UNREADABLE) from error

    if done.returncode == CHILD_TOO_LARGE:
        raise Refused(TOO_LARGE)
    if done.returncode != 0:
        raise Refused(UNREADABLE)

    return done.stdout


def renamed(name: str, ends: Sequence[str]) -> str:
    """`name` when it ends in one of `ends`, case aside, else `name` with
    its last extension replaced by `ends[0]`. The stem is cut to keep the
    name at `MAX_NAME_BYTES`. Example: `photo.gif` -> `photo.png`."""
    if name.lower().endswith(tuple(ends)):
        return name

    stem, dot, _ = name.rpartition(".")
    return with_end(stem if dot and stem else name, ends[0])


def with_end(stem: str, end: str) -> str:
    """`stem` + `end`, with the stem cut to fit `MAX_NAME_BYTES`."""
    while stem and len((stem + end).encode()) > MAX_NAME_BYTES:
        stem = stem[:-1]

    return stem + end


def rebuild_image(upload: File) -> File:
    """Decodes the first frame and encodes it again with no metadata.

    A JPEG stays JPEG. Each other image becomes PNG.
    """
    out = run_child([CHILD_IMAGE], upload.data)
    tag, _, data = out.partition(b"\n")
    if tag == b"JPEG":
        return File(renamed(upload.name, JPEG_ENDS), JPEG_TYPE, data)
    if tag == b"PNG":
        return File(renamed(upload.name, PNG_ENDS), PNG_TYPE, data)

    raise Refused(UNREADABLE)


def _page_pixels(width_pts: float, height_pts: float) -> int:
    scale = PDF_DPI / PDF_POINTS_PER_INCH
    return math.ceil(width_pts * scale) * math.ceil(height_pts * scale)


def pdf_pages(info: str) -> int:
    """The page count of `pdfinfo` output, or `unreadable`."""
    for line in info.splitlines():
        found = PAGES_RE.fullmatch(line.strip())
        if found:
            return int(found.group(1))

    raise Refused(UNREADABLE)


def check_page_sizes(info: str, pages: int) -> None:
    """Refuses `too_large` when a page at `PDF_DPI` has more than
    `MAX_IMAGE_PIXELS`. `info` is the output of `pdfinfo -f 1 -l <pages>`."""
    sizes = [PAGE_SIZE_RE.fullmatch(line.strip()) for line in info.splitlines()]
    found = [size for size in sizes if size]
    if len(found) != pages:
        raise Refused(UNREADABLE)

    for size in found:
        if _page_pixels(float(size.group(1)), float(size.group(2))) > MAX_IMAGE_PIXELS:
            raise Refused(TOO_LARGE)


def pdf_text(raw: bytes) -> bytes:
    """The text of `pdftotext` with no NUL byte, or `unreadable` when it is
    not strict UTF-8."""
    text = raw.replace(b"\0", b"")
    try:
        text.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise Refused(UNREADABLE) from error

    return text


def rebuild_pdf(upload: File) -> list[File]:
    """A new PDF from the page pixels at `PDF_DPI`, and its text as
    `<name>.txt`."""
    with tempfile.TemporaryDirectory(prefix="creche-scan-") as work:
        source = Path(work) / "in.pdf"
        source.write_bytes(upload.data)
        info = run_child([CHILD_LIMIT, PDFINFO, "-enc", "UTF-8", str(source)], b"")
        pages = pdf_pages(info.decode("utf-8", errors="replace"))
        if pages > MAX_PDF_PAGES:
            raise Refused(TOO_LARGE)
        if pages < 1:
            raise Refused(UNREADABLE)

        last = str(pages)
        sizes = run_child([CHILD_LIMIT, PDFINFO, "-f", "1", "-l", last, str(source)], b"")
        check_page_sizes(sizes.decode("utf-8", errors="replace"), pages)

        prefix = Path(work) / "page"
        render = [PDFTOPPM, "-r", str(PDF_DPI), "-png", "-f", "1", "-l", last]
        run_child([CHILD_LIMIT, *render, str(source), str(prefix)], b"")
        images = sorted(str(path) for path in Path(work).glob("page-*.png"))
        if len(images) != pages:
            raise Refused(UNREADABLE)

        rebuilt = run_child([CHILD_PAGES, *images], b"")
        text = run_child(
            [CHILD_LIMIT, PDFTOTEXT, "-enc", "UTF-8", "-l", last, str(source), "-"], b""
        )

    return [
        File(upload.name, PDF_TYPE, rebuilt),
        File(with_end(upload.name, TEXT_END), TEXT_TYPE, pdf_text(text)),
    ]


def rebuild_text(upload: File) -> File:
    """The same bytes when they are strict UTF-8 with no NUL byte, else
    `not_utf8`."""
    if b"\0" in upload.data:
        raise Refused(NOT_UTF8)

    try:
        upload.data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise Refused(NOT_UTF8) from error

    return upload


def rebuild(kind: str, upload: File) -> list[File]:
    if kind == KIND_IMAGE:
        return check_sizes([rebuild_image(upload)])
    if kind == KIND_PDF:
        return check_sizes(rebuild_pdf(upload))

    return check_sizes([rebuild_text(upload)])


def check_sizes(files: list[File]) -> list[File]:
    """The rebuilt files, or `too_large` when one is past `MAX_FILE_BYTES`.

    A rebuilt file can be larger than its input. Example: a PDF made again
    from its page pixels at `PDF_DPI`.
    """
    if any(len(one.data) > MAX_FILE_BYTES for one in files):
        raise Refused(TOO_LARGE)

    return files


# --- The scan ----------------------------------------------------------------------


def scan_line(line: bytes, typer: Typer, clamd: Clamd) -> bytes:
    """The reply line of one request line: the steps of design section 5.

    1. The request, and the kind of its claimed type.
    2. Magika reads the bytes (`mismatch`).
    3. clamd scans the bytes (`malware`).
    4. The rebuild of the kind. Each rebuilt file is `MAX_FILE_BYTES` or
       less.
    """
    try:
        upload = parse_request(line)
        kind = kind_of(upload.mime_type)
        typer.check(kind, upload.data)
        clamd.check(upload.data)
        return ok_line(rebuild(kind, upload))
    except Refused as refusal:
        return refused_line(refusal.reason)


# --- The children ------------------------------------------------------------------


def _pillow() -> ModuleType:
    """`PIL.Image` with the pixel cap of scan.json. An image past the cap
    raises, so the child refuses it as `too_large`."""
    image = _library("PIL.Image")
    image.__dict__["MAX_IMAGE_PIXELS"] = MAX_IMAGE_PIXELS
    warnings.simplefilter("error", image.DecompressionBombWarning)
    return image


def _limit_memory() -> None:
    resource.setrlimit(resource.RLIMIT_AS, (CHILD_MEMORY_BYTES, CHILD_MEMORY_BYTES))


def _fresh(picture: _Picture, mode: str, image: ModuleType) -> _Picture:
    """A new image of the pixels of `picture` in `mode`, with no info."""
    pixels = picture.convert(mode)
    frombytes = cast("_FromBytes", image.frombytes)
    return frombytes(mode, pixels.size, pixels.tobytes())


class _FromBytes(Protocol):
    def __call__(self, mode: str, size: tuple[int, int], data: bytes) -> _Picture: ...


def _open_picture(image: ModuleType, data: BinaryIO, formats: Sequence[str]) -> _Picture:
    opener = cast("_Opener", image.open)
    return opener(data, formats=list(formats))


class _Opener(Protocol):
    def __call__(self, fp: BinaryIO, formats: list[str]) -> _Picture: ...


def _alpha(picture: _Picture) -> bool:
    return picture.mode in {"RGBA", "LA", "PA"} or "transparency" in picture.info


def child_image(data: bytes) -> bytes:
    """The child step of an image: `PNG` or `JPEG`, LF, then the bytes.

    It decodes only the four formats, refuses past `MAX_IMAGE_PIXELS`,
    takes the first frame and turns it by its EXIF orientation.
    """
    image = _pillow()
    ops = _library("PIL.ImageOps")
    picture = _open_picture(image, io.BytesIO(data), IMAGE_FORMATS)
    width, height = picture.size
    if width * height > MAX_IMAGE_PIXELS:
        raise Refused(TOO_LARGE)

    source_format = picture.format
    picture.seek(0)
    picture.load()
    turned = cast("_Picture", ops.exif_transpose(picture))
    out = io.BytesIO()
    if source_format == "JPEG":
        mode = "L" if turned.mode == "L" else "RGB"
        _fresh(turned, mode, image).save(out, "JPEG", quality=JPEG_QUALITY)
        return b"JPEG\n" + out.getvalue()

    mode = "RGBA" if _alpha(turned) else "L" if turned.mode == "L" else "RGB"
    _fresh(turned, mode, image).save(out, "PNG")
    return b"PNG\n" + out.getvalue()


def child_pages(paths: Sequence[str]) -> bytes:
    """The child step of a PDF: one PDF of the page images in `paths`."""
    image = _pillow()
    pages: list[_Picture] = []
    for path in paths:
        with open(path, "rb") as handle:
            picture = _open_picture(image, handle, ("PNG",))
            picture.load()
        pages.append(_fresh(picture, "RGB", image))

    out = io.BytesIO()
    first, *rest = pages
    first.save(out, "PDF", save_all=True, append_images=rest, resolution=float(PDF_DPI))
    return out.getvalue()


def child_main(argv: list[str]) -> int:
    """Runs one child step under the memory limit.

    `limit <argv>` runs a tool. `image` rebuilds stdin. `pages <paths>`
    makes one PDF. A refusal exits with its status, and each other error
    is `unreadable`.
    """
    _limit_memory()
    command, args = argv[0], argv[1:]
    if command == CHILD_LIMIT and args:
        os.execv(args[0], args)

    try:
        if command == CHILD_IMAGE:
            out = child_image(sys.stdin.buffer.read())
        elif command == CHILD_PAGES and args:
            out = child_pages(args)
        else:
            return CHILD_UNREADABLE
    except Refused as refusal:
        return CHILD_TOO_LARGE if refusal.reason == TOO_LARGE else CHILD_UNREADABLE
    except Exception as error:
        name = type(error).__name__
        return CHILD_TOO_LARGE if name.startswith("DecompressionBomb") else CHILD_UNREADABLE

    sys.stdout.buffer.write(out)
    sys.stdout.buffer.flush()
    return 0


# --- The facade --------------------------------------------------------------------


class Facade:
    """The links, the engines and clamd of one start of the program."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.open = 0
        self.slots = threading.Semaphore(SCAN_SLOTS)
        self.ready = threading.Event()
        self.stopping = threading.Event()
        self.typer = Typer()
        self.clamd = Clamd(CLAMD_SOCKET)

    def take(self) -> bool:
        """Counts one more open link, or False past the cap."""
        with self.lock:
            if self.open >= LINK_CAP:
                return False
            self.open += 1
            return True

    def give(self) -> None:
        with self.lock:
            self.open -= 1

    def serve(self, conn: socket.socket, number: int) -> None:
        """Serves one relay link: the kind line, one request, one reply."""
        with conn:
            try:
                kind = link_kind(read_kind_line(conn))
            except OSError as error:
                say(f"link {number} read failed: {error}")
                return

            if kind is None:
                say(f"link {number} refused: no kind line")
                return

            if not self.take():
                say(f"link {number} refused: {BUSY}")
                with contextlib.suppress(OSError):
                    conn.sendall(refused_line(BUSY))
                return

            try:
                reply = self.answer(conn)
                conn.sendall(reply)
                say(f"link {number} answered {len(reply)} bytes")
            except OSError as error:
                say(f"link {number} failed: {error}")
            finally:
                self.give()

    def answer(self, conn: socket.socket) -> bytes:
        """The reply line of the request on `conn`, once clamd answers."""
        if not self.ready.wait(CLAMD_START_WAIT_S):
            return refused_line(FAILED)

        with self.slots:
            try:
                line = read_request(conn)
            except Refused as refusal:
                return refused_line(refusal.reason)

            return scan_line(line, self.typer, self.clamd)

    def run_clamd(self, child: subprocess.Popen[bytes]) -> None:
        """Waits until clamd answers, then ends the program when it stops.
        Guest init then starts the program again. Magika loads meanwhile."""
        self.typer.load()
        waited = 0.0
        while not self.clamd.ping():
            if child.poll() is not None or waited >= CLAMD_START_WAIT_S:
                say("clamd does not answer")
                child.kill()
                os._exit(EXIT_NO_CLAMD)
            self.stopping.wait(1.0)
            waited += 1.0

        self.ready.set()
        say("clamd answers")
        child.wait()
        if not self.stopping.is_set():
            say(f"clamd stopped with status {child.returncode}")
            os._exit(EXIT_NO_CLAMD)


def listen_relay() -> socket.socket:
    """Listens on `RELAY_SOCKET`, mode 0600 in a 0700 directory."""
    path = Path(RELAY_SOCKET)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(RELAY_SOCKET)
    path.chmod(0o600)
    server.listen(LINK_CAP)
    return server


def accept_links(server: socket.socket, facade: Facade) -> None:
    """Serves each relay link on a thread of its own."""
    number = 0
    while True:
        conn, _ = server.accept()
        number += 1
        threading.Thread(target=facade.serve, args=(conn, number), daemon=True).start()


def main(argv: list[str]) -> int:
    if argv[1:]:
        return child_main(argv[1:])

    if not os.environ.get(SESSION_ID_ENV):
        say(f"no {SESSION_ID_ENV}: this program runs under guest init only")
        return EXIT_NO_SESSION

    facade = Facade()
    server = listen_relay()
    local = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    local.connect(LOCAL_LINK_SOCKET)
    clamd = start_clamd()
    threading.Thread(target=facade.run_clamd, args=(clamd,), daemon=True).start()
    threading.Thread(target=accept_links, args=(server, facade), daemon=True).start()
    say("up")

    # The local link carries only the stop line. Its end, or the stop,
    # ends the program and clamd. Guest init then stops each child with
    # the group of the program.
    reader = local.makefile("rb")
    for line in reader:
        if is_stop(line):
            say("stop")
            break

    facade.stopping.set()
    clamd.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
