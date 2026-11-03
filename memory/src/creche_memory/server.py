"""The socket server of the memory service.

The `memory` upstream connects to one Unix socket and writes one request
line. The server checks the uid of the peer, parses the line into one
`Request`, and writes one reply line. Then it closes the connection. A peer
with another uid gets no reply."""

import os
import socket
import socketserver
import stat
import struct
import sys
from pathlib import Path
from typing import assert_never

from pydantic import ValidationError

from .vault import Vault
from .wire import (
    CreateRequest,
    ListReply,
    ListRequest,
    ReadReply,
    ReadRequest,
    Refusal,
    RefusedReply,
    Request,
    RewriteRequest,
    SearchRequest,
)

#: The longest request line, newline included. The largest request is a
#: rewrite: 512 KiB of text, escaped as JSON.
MAX_LINE_BYTES = 640 * 1024

#: Seconds a peer has to send its request line.
LINE_TIMEOUT_S = 10

#: The mode of the socket file: the service user and the group of the folder.
SOCKET_MODE = 0o660

#: `struct ucred` of Linux: pid, uid, gid.
UCRED = struct.Struct("3i")

#: The start of `struct xucred` of macOS: version, then uid. The service runs
#: on Linux. This form lets the tests run on a macOS checkout.
XUCRED_HEAD = struct.Struct("2I")
XUCRED_SIZE = 76
SOL_LOCAL = 0

type Answer = ReadReply | ListReply | RefusedReply


def _refused(code: Refusal, reason: str) -> RefusedReply:
    return RefusedReply(ok=False, refused=code, reason=reason)


def peer_uid(sock: socket.socket) -> int:
    """The uid of the process at the other end of a Unix socket."""
    if sys.platform == "linux":
        _, uid, _ = UCRED.unpack(sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, UCRED.size))
        return uid

    if sys.platform == "darwin":
        raw = sock.getsockopt(SOL_LOCAL, socket.LOCAL_PEERCRED, XUCRED_SIZE)
        _, uid = XUCRED_HEAD.unpack_from(raw)
        return uid

    raise OSError(f"no peer credentials on {sys.platform}")


def answer(line: bytes, vault: Vault) -> Answer:
    """The reply to one request line."""
    try:
        request = Request.model_validate_json(line).root
    except ValidationError as error:
        return _refused(Refusal.BAD_REQUEST, f"{error.error_count()} field(s) refused")

    match request:
        case ReadRequest():
            return vault.read_note(request.path)
        case ListRequest():
            return vault.list_folder(request.folder)
        case SearchRequest() | CreateRequest() | RewriteRequest():
            return _refused(Refusal.NOT_READY, f"this service does not serve {request.op} yet")
        case _:
            assert_never(request)


def _read_line(handler: socketserver.StreamRequestHandler) -> bytes | RefusedReply:
    line = handler.rfile.readline(MAX_LINE_BYTES + 1)
    if len(line) > MAX_LINE_BYTES:
        return _refused(Refusal.TOO_LARGE, f"the request is over {MAX_LINE_BYTES} bytes")

    if not line.endswith(b"\n"):
        return _refused(Refusal.BAD_REQUEST, "the request ends with no newline")

    return line


class _Handler(socketserver.StreamRequestHandler):
    timeout = LINE_TIMEOUT_S

    def handle(self) -> None:
        server = self.server
        assert isinstance(server, MemoryServer)
        try:
            if peer_uid(self.connection) != server.peer:
                return

            line = _read_line(self)
            reply = line if isinstance(line, RefusedReply) else answer(line, server.vault)
            self.wfile.write(reply.model_dump_json().encode() + b"\n")
        except OSError:
            return


class MemoryServer(socketserver.ThreadingUnixStreamServer):
    """Serves one vault to one peer uid on one socket."""

    daemon_threads = True

    def __init__(self, path: Path, vault: Vault, peer: int) -> None:
        _remove_stale(path)
        self.vault = vault
        self.peer = peer
        super().__init__(str(path), _Handler)
        os.chmod(path, SOCKET_MODE)


def _remove_stale(path: Path) -> None:
    """Remove the socket of an earlier run. Any other file stops the start."""
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return

    if not stat.S_ISSOCK(mode):
        raise FileExistsError(f"{path} exists and is not a socket")

    path.unlink()
