"""The socket server answers one line per connection, and only its peer uid."""

import json
import os
import shutil
import socket
import stat
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from creche_memory.server import MAX_LINE_BYTES, SOCKET_MODE, MemoryServer, answer
from creche_memory.vault import Vault
from creche_memory.writes import Writer

NOTE = "# Dentist\n\nThe one on Oak St.\n"


@pytest.fixture
def vault() -> Iterator[Path]:
    # A Unix socket path has a short limit, so the socket lives in a short
    # temp folder, not in pytest's tmp_path.
    root = Path(tempfile.mkdtemp(prefix="mem"))
    (root / "vault" / "profile").mkdir(parents=True)
    (root / "vault" / "profile" / "dentist.md").write_text(NOTE, encoding="utf-8")
    yield root
    shutil.rmtree(root)


def _serve(root: Path, peer: int) -> tuple[MemoryServer, Path]:
    path = root / "memory.sock"
    server = MemoryServer(path, Vault(root / "vault"), Writer(root / "vault"), peer)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    return server, path


def _call(path: Path, data: bytes) -> bytes:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(path))
        client.sendall(data)
        client.shutdown(socket.SHUT_WR)
        chunks: list[bytes] = []
        while chunk := client.recv(65536):
            chunks.append(chunk)

    return b"".join(chunks)


def _request(op: str, **fields: object) -> bytes:
    return json.dumps({"v": 1, "op": op, "caller": "chat", **fields}).encode() + b"\n"


def test_a_read_over_the_socket_returns_the_note(vault: Path) -> None:
    server, path = _serve(vault, os.getuid())
    try:
        reply = json.loads(_call(path, _request("read", path="profile/dentist.md")))
    finally:
        server.shutdown()
        server.server_close()

    assert reply["ok"] is True
    assert reply["text"] == NOTE
    assert stat.S_IMODE(path.stat().st_mode) == SOCKET_MODE


def test_another_uid_gets_no_reply(vault: Path) -> None:
    server, path = _serve(vault, os.getuid() + 1)
    try:
        reply = _call(path, _request("read", path="profile/dentist.md"))
    except ConnectionResetError:
        # Linux resets the connection, because the server closes it with
        # the request unread. A reset carries no reply either.
        reply = b""
    finally:
        server.shutdown()
        server.server_close()

    assert reply == b""


@pytest.mark.parametrize(
    ("data", "refused"),
    [
        (b"x" * (MAX_LINE_BYTES + 1), "too_large"),
        (_request("read", path="profile/dentist.md").rstrip(b"\n"), "bad_request"),
    ],
    ids=["a line over the cap", "a line with no newline"],
)
def test_a_bad_line_gets_a_refusal(vault: Path, data: bytes, refused: str) -> None:
    server, path = _serve(vault, os.getuid())
    try:
        reply = json.loads(_call(path, data))
    finally:
        server.shutdown()
        server.server_close()

    assert reply["refused"] == refused


def test_a_stale_socket_is_replaced_and_a_file_stops_the_start(vault: Path) -> None:
    server, path = _serve(vault, os.getuid())
    server.shutdown()
    server.server_close()

    again, _ = _serve(vault, os.getuid())
    again.shutdown()
    again.server_close()

    path.unlink()
    path.write_text("not a socket", encoding="utf-8")
    with pytest.raises(FileExistsError):
        MemoryServer(path, Vault(vault / "vault"), Writer(vault / "vault"), os.getuid())


@pytest.mark.parametrize(
    ("line", "refused"),
    [
        (b"{not json\n", "bad_request"),
        (_request("delete", path="a.md"), "bad_request"),
        (_request("search", query="dentist", k=3), "not_ready"),
        (_request("rewrite", path="a.md", text="x", if_match="new"), "not_allowed_here"),
    ],
    ids=["not JSON", "an unknown op", "search", "a rewrite outside the agent areas"],
)
def test_answer_refuses_what_it_does_not_serve(vault: Path, line: bytes, refused: str) -> None:
    reply = answer(line, Vault(vault / "vault"), Writer(vault / "vault"))

    assert json.loads(reply.model_dump_json())["refused"] == refused


def test_answer_lists_a_folder(vault: Path) -> None:
    reply = json.loads(
        answer(
            _request("list", folder="profile"), Vault(vault / "vault"), Writer(vault / "vault")
        ).model_dump_json()
    )

    assert [note["path"] for note in reply["notes"]] == ["profile/dentist.md"]
