"""`playpen/guest/creche_scanner_facade.py`, the facade of the scanner
service image (attachments design note, sections 4 and 5).

The tests read the program as a module. They hold its constants equal to
`rust/crates/wire/vectors/scan.json` and its paths equal to `guest.json`.
They check the line rules, the request rules, the clamd client against a
stand-in that speaks the clamd protocol, the name rules, and each layer
with real small files that the tests make.

Magika, Pillow and clamd are in the image only. A test that needs one of
them skips when it is absent, and says which. The program has no such
path: it always runs every layer. The scanner image has all three, so
each test runs there.
"""

from __future__ import annotations

import base64
import contextlib
import importlib.util
import io
import json
import shutil
import socket
import struct
import sys
import tempfile
import threading
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, Final

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
FACADE: Final = REPO_ROOT / "playpen" / "guest" / "creche_scanner_facade.py"
DOCKERFILE: Final = REPO_ROOT / "playpen" / "Dockerfile"
VECTORS: Final = REPO_ROOT / "rust" / "crates" / "wire" / "vectors"

#: The first line of the scanner service target.
TARGET: Final = "FROM ${PYTHON_IMAGE} AS service-scanner"

#: The EICAR test file, in two halves, so this file does not hold it.
EICAR: Final = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD" + b"-ANTIVIRUS-TEST-FILE!$H+H*"


def _has(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


HAS_PILLOW: Final = _has("PIL")
HAS_MAGIKA: Final = _has("magika")
HAS_POPPLER: Final = all(shutil.which(tool) for tool in ("pdfinfo", "pdftoppm", "pdftotext"))
CLAMD_SOCKET: Final = Path("/run/creche-scanner/clamd.sock")

needs_pillow = pytest.mark.skipif(not HAS_PILLOW, reason="Pillow is in the scanner image only")
needs_magika = pytest.mark.skipif(not HAS_MAGIKA, reason="magika is in the scanner image only")
needs_poppler = pytest.mark.skipif(not HAS_POPPLER, reason="poppler-utils is not installed")
needs_clamd = pytest.mark.skipif(
    not CLAMD_SOCKET.exists(), reason="no clamd answers on the socket of the scanner image"
)


def _facade() -> ModuleType:
    """The facade as a module, to read its constants and functions."""
    spec = importlib.util.spec_from_file_location("creche_scanner_facade", FACADE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # A dataclass of the module looks its module up by name.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


F: Final = _facade()


def _vector(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((VECTORS / name).read_text(encoding="utf-8"))
    return loaded


def _line(name: str, mime_type: str, data: bytes) -> bytes:
    body = {"name": name, "mime_type": mime_type, "data": base64.b64encode(data).decode()}
    return json.dumps(body).encode() + b"\n"


def _reply(line: bytes) -> dict[str, Any]:
    assert line.endswith(b"\n")
    assert line.count(b"\n") == 1
    loaded: dict[str, Any] = json.loads(line)
    return loaded


def _refusal(line: bytes) -> str:
    reason: str = _reply(line)["refused"]["reason"]
    return reason


def _files(line: bytes) -> list[tuple[str, str, bytes]]:
    files: list[dict[str, str]] = _reply(line)["ok"]["files"]
    return [(one["name"], one["mime_type"], base64.b64decode(one["data"])) for one in files]


def _absent_clamd(tmp_path: Path) -> Any:
    """A real clamd client on a socket where nothing listens."""
    return F.Clamd(tmp_path / "none.sock")


# --- The shared numbers ------------------------------------------------------------


def test_the_constants_are_the_ones_of_scan_json() -> None:
    """Each limit and each list equals the shared vector."""
    scan = _vector("scan.json")

    assert scan["kind_line"] == F.KIND_LINE
    assert scan["line_cap"] == F.LINE_CAP
    assert scan["wait_s"] == F.WAIT_S
    assert scan["max_file_bytes"] == F.MAX_FILE_BYTES
    assert scan["max_pdf_pages"] == F.MAX_PDF_PAGES
    assert scan["max_image_pixels"] == F.MAX_IMAGE_PIXELS
    assert scan["max_name_bytes"] == F.MAX_NAME_BYTES
    assert scan["kinds"] == {kind: list(types) for kind, types in F.KINDS.items()}
    assert scan["reasons"] == list(F.REASONS)


def test_the_paths_are_the_ones_of_guest_init() -> None:
    """The facade path, the local link, the relay and the session env."""
    guest = _vector("guest.json")

    assert guest["local_link_socket"] == F.LOCAL_LINK_SOCKET
    assert guest["relay_socket"] == F.RELAY_SOCKET
    assert guest["session_id_env"] == F.SESSION_ID_ENV
    copy = f"COPY playpen/guest/creche_scanner_facade.py {guest['facade_program']}"
    assert copy in _stage()


def test_the_wire_examples_parse_and_answer() -> None:
    """The request of scan.json parses, and the reply lines of the program
    have the shape of its two replies."""
    scan = _vector("scan.json")
    request = json.dumps(scan["request"]).encode() + b"\n"

    upload = F.parse_request(request)
    assert (upload.name, upload.mime_type, upload.data) == ("notes.txt", "text/plain", b"hello\n")
    assert _reply(F.ok_line([F.rebuild_text(upload)])) == scan["reply_ok"]
    assert _reply(F.refused_line(F.MISMATCH)) == scan["reply_refused"]


# --- The lines ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "kind"),
    [
        (b"scan\n", "scan"),
        (b"scan", None),
        (b"scan\r\n", None),
        (b"SCAN\n", None),
        (b"mcp\n", None),
        (b"\n", None),
        (b"s" * 16 + b"\n", None),
    ],
)
def test_only_the_scan_line_names_a_link(line: bytes, kind: str | None) -> None:
    assert F.link_kind(line) == kind


@pytest.mark.parametrize(
    ("line", "stop"),
    [(b'{"stop":{}}\n', True), (b'{"stop":{"now":1}}\n', False), (b"stop\n", False)],
)
def test_only_the_stop_line_stops(line: bytes, stop: bool) -> None:
    assert F.is_stop(line) is stop


def test_the_kind_line_read_leaves_the_request() -> None:
    """The bytes after the LF stay in the socket for the request read."""
    left, right = socket.socketpair()
    with left, right:
        left.sendall(b'scan\n{"name":1}\n')
        assert F.read_kind_line(right) == b"scan\n"
        assert F.read_request(right) == b'{"name":1}\n'


def test_a_request_with_no_lf_is_failed() -> None:
    left, right = socket.socketpair()
    with left, right:
        left.sendall(b'{"name":')
        left.shutdown(socket.SHUT_WR)
        with pytest.raises(F.Refused) as refusal:
            F.read_request(right)
        assert refusal.value.reason == F.FAILED


def test_a_request_past_the_line_cap_is_failed() -> None:
    """The read stops one byte past `LINE_CAP`, with no LF found."""
    left, right = socket.socketpair()
    sent = threading.Thread(target=_send_quietly, args=(left, b"x" * (F.LINE_CAP + 2)))
    with left, right:
        sent.start()
        with pytest.raises(F.Refused) as refusal:
            F.read_request(right)
        assert refusal.value.reason == F.FAILED
        right.close()
        sent.join(timeout=30)


def _send_quietly(conn: socket.socket, data: bytes) -> None:
    with contextlib.suppress(OSError):
        conn.sendall(data)


# --- The request ------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        b"not json\n",
        b"[]\n",
        b'{"name":"a","mime_type":"text/plain"}\n',
        b'{"name":"a","mime_type":"text/plain","data":"","more":1}\n',
        b'{"name":"a","name":"b","mime_type":"text/plain","data":""}\n',
        b'{"name":1,"mime_type":"text/plain","data":""}\n',
        b'{"name":"a","mime_type":"text/plain","data":"!!!"}\n',
        b'{"name":"a","mime_type":"text/plain","data":""}',
        b'{"name":"a/b","mime_type":"text/plain","data":""}\n',
        b'{"name":"..","mime_type":"text/plain","data":""}\n',
        b'{"name":"a\\u0007","mime_type":"text/plain","data":""}\n',
        b'{"name":"","mime_type":"text/plain","data":""}\n',
    ],
)
def test_a_bad_request_is_failed(line: bytes, tmp_path: Path) -> None:
    assert _refusal(F.scan_line(line, F.Typer(), _absent_clamd(tmp_path))) == F.FAILED


@pytest.mark.parametrize(
    ("name", "valid"),
    [("notes.txt", True), ("é" * 127, True), ("é" * 128, False), ("a\x7f", False)],
)
def test_the_name_rule_counts_utf8_bytes(name: str, valid: bool) -> None:
    assert F.valid_name(name) is valid


def test_an_oversized_file_is_too_large(tmp_path: Path) -> None:
    """A file one byte past `MAX_FILE_BYTES` stops before any layer."""
    line = _line("big.txt", "text/plain", b"a" * (F.MAX_FILE_BYTES + 1))

    assert _refusal(F.scan_line(line, F.Typer(), _absent_clamd(tmp_path))) == F.TOO_LARGE


@pytest.mark.parametrize(
    "mime_type",
    ["application/zip", "video/mp4", "image/svg+xml", "text/plain; charset=utf-8", "Text/Plain"],
)
def test_an_unknown_type_is_not_allowed(mime_type: str, tmp_path: Path) -> None:
    line = _line("a.bin", mime_type, b"hello\n")

    assert _refusal(F.scan_line(line, F.Typer(), _absent_clamd(tmp_path))) == F.NOT_ALLOWED


def test_a_reply_past_the_line_cap_is_too_large() -> None:
    big = F.File("a.txt", "text/plain", b"a" * F.LINE_CAP)

    with pytest.raises(F.Refused) as refusal:
        F.ok_line([big])
    assert refusal.value.reason == F.TOO_LARGE


def test_a_rebuilt_file_past_the_file_cap_is_too_large() -> None:
    """A rebuilt file can grow past its input, a rebuilt PDF most of all.
    Each output file is `MAX_FILE_BYTES` or less."""
    fits = F.File("a.pdf", "application/pdf", b"a" * F.MAX_FILE_BYTES)
    grown = F.File("a.pdf.txt", "text/plain", b"a" * (F.MAX_FILE_BYTES + 1))

    assert F.check_sizes([fits]) == [fits]
    with pytest.raises(F.Refused) as refusal:
        F.check_sizes([fits, grown])
    assert refusal.value.reason == F.TOO_LARGE


# --- The names of rebuilt files ------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "ends", "want"),
    [
        ("photo.gif", (".png",), "photo.png"),
        ("photo.PNG", (".png",), "photo.PNG"),
        ("photo", (".png",), "photo.png"),
        (".gif", (".png",), ".gif.png"),
        ("scan.jpeg", (".jpg", ".jpeg"), "scan.jpeg"),
        ("scan.png", (".jpg", ".jpeg"), "scan.jpg"),
    ],
)
def test_a_rebuilt_image_gets_the_end_of_its_type(
    name: str, ends: tuple[str, ...], want: str
) -> None:
    assert F.renamed(name, ends) == want


def test_a_long_name_stays_in_the_name_cap() -> None:
    """`<name>.txt` of a 255-byte name cuts the stem, not the end."""
    name = "a" * (F.MAX_NAME_BYTES - 4) + ".pdf"

    text = F.with_end(name, F.TEXT_END)
    assert len(text.encode()) == F.MAX_NAME_BYTES
    assert text.endswith(".txt")
    assert F.valid_name(text)


# --- Layer 2: the clamd client --------------------------------------------------------


def _stand_in_clamd(path: Path, verdict: bytes) -> Iterator[list[bytes]]:
    """A listener on `path` that speaks INSTREAM and PING as clamd does.

    It keeps each stream it reads, and answers `verdict` to each.
    """
    seen: list[bytes] = []
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(4)

    def serve() -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                reader = conn.makefile("rb")
                command = b""
                while not command.endswith(b"\0"):
                    command += reader.read(1)
                if command == b"zPING\0":
                    conn.sendall(b"PONG\0")
                    continue
                body = b""
                while size := struct.unpack(">I", reader.read(4))[0]:
                    body += reader.read(size)
                seen.append(body)
                conn.sendall(verdict + b"\0")

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield seen
    server.close()


@pytest.fixture
def short_dir() -> Iterator[Path]:
    """A dir under /tmp: the path of a Unix socket has at most 104 bytes on
    macOS, and the `tmp_path` of pytest is longer there."""
    with tempfile.TemporaryDirectory(dir="/tmp") as name:
        yield Path(name)


@pytest.fixture
def clean_clamd(short_dir: Path) -> Iterator[tuple[Path, list[bytes]]]:
    path = short_dir / "clean.sock"
    yield from ((path, seen) for seen in _stand_in_clamd(path, b"stream: OK"))


@pytest.fixture
def found_clamd(short_dir: Path) -> Iterator[Path]:
    path = short_dir / "found.sock"
    yield from (path for _ in _stand_in_clamd(path, b"stream: Eicar-Test-Signature FOUND"))


def test_the_client_streams_every_byte_in_chunks(
    clean_clamd: tuple[Path, list[bytes]],
) -> None:
    """Three chunks and the end mark carry the bytes, and OK passes."""
    path, seen = clean_clamd
    data = bytes(range(256)) * (F.CLAMD_CHUNK // 128 + 1)
    client = F.Clamd(path)

    assert client.ping()
    client.check(data)
    assert seen == [data]


def test_a_signature_is_malware(found_clamd: Path) -> None:
    with pytest.raises(F.Refused) as refusal:
        F.Clamd(found_clamd).check(b"bytes")
    assert refusal.value.reason == F.MALWARE


def test_no_clamd_is_failed(tmp_path: Path) -> None:
    client = _absent_clamd(tmp_path)

    assert not client.ping()
    with pytest.raises(F.Refused) as refusal:
        client.check(b"bytes")
    assert refusal.value.reason == F.FAILED


def test_the_clamd_config_takes_a_whole_file() -> None:
    """clamd reads at most `StreamMaxLength` bytes of a stream."""
    config = F.clamd_config()

    assert f"StreamMaxLength {F.MAX_FILE_BYTES}\n" in config
    assert f"LocalSocket {F.CLAMD_SOCKET}\n" in config
    assert "User clamav\n" in config


# --- Layer 3: the text rebuild ---------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "reason"),
    [(b"caf\xe9\n", "not_utf8"), (b"a\0b\n", "not_utf8"), (b"\xef\xbb\xbfok\n", None)],
)
def test_text_must_be_strict_utf8(data: bytes, reason: str | None) -> None:
    upload = F.File("a.txt", "text/plain", data)
    if reason is None:
        assert F.rebuild_text(upload) == upload
        return

    with pytest.raises(F.Refused) as refusal:
        F.rebuild_text(upload)
    assert refusal.value.reason == reason


def test_the_pdf_text_drops_nul_and_needs_utf8() -> None:
    assert F.pdf_text(b"page\0 one\n") == b"page one\n"
    with pytest.raises(F.Refused) as refusal:
        F.pdf_text(b"\xff")
    assert refusal.value.reason == F.UNREADABLE


def test_the_pdfinfo_lines_give_the_pages_and_their_size() -> None:
    assert F.pdf_pages("Producer:  x\nPages:          3\nEncrypted: no\n") == 3
    sizes = "Page    1 size: 612 x 792 pts (letter)\nPage    2 size:  100 x 140 pts\n"
    F.check_page_sizes(sizes, 2)
    with pytest.raises(F.Refused) as refusal:
        F.check_page_sizes("Page    1 size: 14400 x 14400 pts\n", 1)
    assert refusal.value.reason == F.TOO_LARGE


# --- Layer 3: the image rebuild, with Pillow --------------------------------------------


def _image(fmt: str, size: tuple[int, int] = (8, 6), **params: Any) -> bytes:
    from PIL import Image

    picture = Image.new("RGB", size, (200, 30, 40))
    out = io.BytesIO()
    picture.save(out, fmt, **params)
    return out.getvalue()


def _exif_jpeg() -> bytes:
    from PIL import Image

    exif = Image.Exif()
    exif[0x0110] = "a camera"
    exif[0x0112] = 6
    return _image("JPEG", exif=exif.tobytes())


def _open(data: bytes) -> Any:
    from PIL import Image

    return Image.open(io.BytesIO(data))


@needs_pillow
def test_a_png_stays_png_with_no_text_chunk() -> None:
    from PIL import PngImagePlugin

    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", "a secret")
    upload = F.File("a.png", "image/png", _image("PNG", pnginfo=info))

    rebuilt = F.rebuild_image(upload)
    assert (rebuilt.name, rebuilt.mime_type) == ("a.png", "image/png")
    assert _open(rebuilt.data).format == "PNG"
    assert b"a secret" not in rebuilt.data


@needs_pillow
def test_a_jpeg_stays_jpeg_turned_and_with_no_exif() -> None:
    """Orientation 6 turns the 8x6 image to 6x8, and no EXIF stays."""
    upload = F.File("a.jpg", "image/jpeg", _exif_jpeg())

    rebuilt = F.rebuild_image(upload)
    picture = _open(rebuilt.data)
    assert (rebuilt.name, rebuilt.mime_type) == ("a.jpg", "image/jpeg")
    assert picture.format == "JPEG"
    assert picture.size == (6, 8)
    assert "exif" not in picture.info
    assert b"a camera" not in rebuilt.data


@needs_pillow
def test_a_gif_becomes_png() -> None:
    upload = F.File("anim.gif", "image/gif", _image("GIF"))

    rebuilt = F.rebuild_image(upload)
    assert (rebuilt.name, rebuilt.mime_type) == ("anim.png", "image/png")
    assert _open(rebuilt.data).format == "PNG"


@needs_pillow
def test_an_image_past_the_pixel_cap_is_too_large() -> None:
    """A PNG of `MAX_IMAGE_PIXELS` + 1 pixels: one row more than the cap."""
    from PIL import Image

    width = 10000
    picture = Image.new("1", (width, F.MAX_IMAGE_PIXELS // width + 1))
    out = io.BytesIO()
    picture.save(out, "PNG")

    with pytest.raises(F.Refused) as refusal:
        F.rebuild_image(F.File("big.png", "image/png", out.getvalue()))
    assert refusal.value.reason == F.TOO_LARGE


@needs_pillow
def test_a_broken_image_is_unreadable() -> None:
    data = _image("PNG")[:40]

    with pytest.raises(F.Refused) as refusal:
        F.rebuild_image(F.File("a.png", "image/png", data))
    assert refusal.value.reason == F.UNREADABLE


# --- Layer 3: the PDF rebuild, with Pillow and poppler ------------------------------------


def _pdf(pages: int) -> bytes:
    from PIL import Image

    sheets = [Image.new("RGB", (100, 140), (255, 255, 255)) for _ in range(pages)]
    out = io.BytesIO()
    sheets[0].save(out, "PDF", save_all=True, append_images=sheets[1:])
    return out.getvalue()


@needs_pillow
@needs_poppler
def test_a_pdf_is_made_again_from_pixels_with_its_text() -> None:
    rebuilt = F.rebuild_pdf(F.File("doc.pdf", "application/pdf", _pdf(2)))

    assert [(one.name, one.mime_type) for one in rebuilt] == [
        ("doc.pdf", "application/pdf"),
        ("doc.pdf.txt", "text/plain"),
    ]
    assert rebuilt[0].data.startswith(b"%PDF-")
    rebuilt[1].data.decode("utf-8")


@needs_pillow
@needs_poppler
def test_a_pdf_past_the_page_cap_is_too_large() -> None:
    upload = F.File("doc.pdf", "application/pdf", _pdf(F.MAX_PDF_PAGES + 1))

    with pytest.raises(F.Refused) as refusal:
        F.rebuild_pdf(upload)
    assert refusal.value.reason == F.TOO_LARGE


# --- Layer 1: Magika ----------------------------------------------------------------------


@needs_magika
@needs_pillow
def test_a_png_claimed_as_pdf_is_mismatch(tmp_path: Path) -> None:
    """Magika reads a PNG. The scan stops before clamd."""
    line = _line("a.pdf", "application/pdf", _image("PNG", (64, 64)))

    assert _refusal(F.scan_line(line, F.Typer(), _absent_clamd(tmp_path))) == F.MISMATCH


@needs_magika
@needs_pillow
def test_a_png_claimed_as_text_is_mismatch(tmp_path: Path) -> None:
    line = _line("a.txt", "text/plain", _image("PNG", (64, 64)))

    assert _refusal(F.scan_line(line, F.Typer(), _absent_clamd(tmp_path))) == F.MISMATCH


# --- The whole scan, with Magika, clamd and Pillow -----------------------------------------


@pytest.fixture
def scan() -> Callable[[str, str, bytes], bytes]:
    typer = F.Typer()
    clamd = F.Clamd(CLAMD_SOCKET)
    return lambda name, mime_type, data: F.scan_line(_line(name, mime_type, data), typer, clamd)


@needs_clamd
@needs_magika
@needs_pillow
def test_a_clean_png_comes_back_rebuilt(scan: Callable[[str, str, bytes], bytes]) -> None:
    ((name, mime_type, data),) = _files(scan("a.png", "image/png", _image("PNG", (64, 64))))

    assert (name, mime_type) == ("a.png", "image/png")
    assert _open(data).size == (64, 64)


@needs_clamd
@needs_magika
@needs_pillow
def test_a_jpeg_comes_back_as_jpeg(scan: Callable[[str, str, bytes], bytes]) -> None:
    ((name, mime_type, _),) = _files(scan("a.jpg", "image/jpeg", _image("JPEG", (64, 64))))

    assert (name, mime_type) == ("a.jpg", "image/jpeg")


@needs_clamd
@needs_magika
@needs_pillow
def test_a_gif_comes_back_as_png(scan: Callable[[str, str, bytes], bytes]) -> None:
    ((name, mime_type, data),) = _files(scan("a.gif", "image/gif", _image("GIF", (64, 64))))

    assert (name, mime_type) == ("a.png", "image/png")
    assert _open(data).format == "PNG"


@needs_clamd
@needs_magika
def test_a_text_file_comes_back_unchanged(scan: Callable[[str, str, bytes], bytes]) -> None:
    text = b"def main() -> None:\n    print('hello')\n" * 8

    assert _files(scan("main.py", "text/x-python", text)) == [("main.py", "text/x-python", text)]


@needs_clamd
@needs_magika
def test_a_latin1_text_file_is_not_utf8(scan: Callable[[str, str, bytes], bytes]) -> None:
    text = "Le café est très bon, merci beaucoup.\n".encode("latin-1") * 8

    assert _refusal(scan("notes.txt", "text/plain", text)) == F.NOT_UTF8


@needs_clamd
@needs_magika
def test_the_eicar_file_is_malware(scan: Callable[[str, str, bytes], bytes]) -> None:
    assert _refusal(scan("eicar.txt", "text/plain", EICAR)) == F.MALWARE


# --- The Dockerfile ----------------------------------------------------------------------


def _stage() -> str:
    """The scanner stage, from its FROM line to the next one."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    body = text.split(TARGET, 1)[1]
    return body.split("\nFROM ", 1)[0]


def test_the_scanner_target_ships_only_here() -> None:
    """The target holds ClamAV, its signatures, poppler, the pinned wheels
    and the facade, and its build proves each one. No flavor copies the
    facade."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    stage = _stage()

    assert text.count(TARGET) == 1
    assert "clamav clamav-daemon poppler-utils" in stage
    assert "&& freshclam --stdout" in stage
    assert "--require-hashes" in stage
    assert "pip uninstall -y pip" in stage
    assert '[ "$status" -eq 17 ]' in stage
    assert "clamscan --no-summary /tmp/eicar" in stage
    assert FACADE.read_text(encoding="utf-8").startswith("#!/opt/scanner/bin/python -I\n")
    for flavor in text.split(TARGET, 1)[0].split("\nFROM ")[1:]:
        assert "creche_scanner_facade" not in flavor


def test_the_scanner_writes_the_env_file_in_its_last_step() -> None:
    """An ENV line after the write would set a name the file does not hold."""
    steps = [line for line in _stage().replace("\\\n", " ").splitlines()[1:] if line.strip()]
    steps = [line for line in steps if not line.startswith("#")]

    assert steps[-1].startswith(
        "RUN --mount=type=bind,source=playpen/bin/creche-env-write,target=/run/creche-env-write "
    )
    assert steps[-1].endswith("> /etc/creche/env")
    assert "ENV " not in _stage()


def test_the_wheel_pins_are_one_list() -> None:
    """Each wheel has one exact version and one sha256, and magika and
    Pillow are in the list."""
    pins = [line for line in _stage().splitlines() if "--hash=sha256:" in line]
    names = [line.strip().strip("'").split("==")[0] for line in pins]

    assert {"magika", "pillow"} <= set(names)
    assert len(names) == len(set(names))
    for line in pins:
        assert line.count("==") == 1
        assert len(line.split("--hash=sha256:")[1].split("'")[0]) == 64
