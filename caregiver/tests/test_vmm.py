"""The client of the vmm host API: the drop of a family (decisions CH20,
CH21-10, CH25, CH33 rule 10, CH82 rule 5)."""

from __future__ import annotations

import json
import shutil
import socketserver
import tempfile
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import ClassVar

import httpx
import pytest
from caregiver.vmm import (
    MISMATCH_PAUSE_S,
    MISMATCH_TRIES,
    UNREACHABLE,
    UNREADABLE,
    VMM_PROTOCOL_MISMATCH,
    VMM_SOCKET_ENV,
    HttpVmmClient,
    VmmError,
    socket_from,
)
from creche_wire.routes import HOST_API_BODY_CAP, HOST_API_HEADER, HOST_API_PROTOCOL

from caregiver import vmm as vmm_module

#: The wire form of the drop of the family `chat`: the variant name is the
#: one key of the object (`DropTarget`).
CHAT_DROP = {"family": {"family": "chat"}}

#: The mismatch reply of a vmm of another protocol (`ProtocolMismatch`).
MISMATCH = {"code": "protocol_mismatch", "protocol": HOST_API_PROTOCOL + 1}


def _client(answer: httpx.MockTransport) -> HttpVmmClient:
    return HttpVmmClient(httpx.Client(transport=answer))


def _replying(status: int, body: bytes, seen: list[httpx.Request]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, content=body)

    return httpx.MockTransport(handle)


@pytest.fixture
def pauses(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Each pause between two tries, in seconds, with no wait."""
    taken: list[float] = []
    monkeypatch.setattr(vmm_module.time, "sleep", taken.append)
    return taken


def test_a_drop_posts_the_family_with_the_protocol_header() -> None:
    seen: list[httpx.Request] = []

    _client(_replying(200, b"{}", seen)).drop_family("chat")

    (request,) = seen
    assert request.method == "POST"
    assert request.url.path == "/drop"
    assert request.headers[HOST_API_HEADER] == str(HOST_API_PROTOCOL)
    assert request.headers["content-type"] == "application/json"
    assert json.loads(request.content) == CHAT_DROP


def test_a_route_error_is_a_vmm_error_with_its_code() -> None:
    client = _client(_replying(500, b'{"code":"internal"}', []))

    with pytest.raises(VmmError) as caught:
        client.drop_family("chat")

    assert caught.value.code == "internal"


def test_a_mismatch_on_each_try_is_the_named_fault(pauses: list[float]) -> None:
    """Decision CH25 rule 1: retry a few times, then report the fault. The
    numbers are those of attendance (decision CH33 rule 10)."""
    seen: list[httpx.Request] = []
    client = _client(_replying(412, json.dumps(MISMATCH).encode(), seen))

    with pytest.raises(VmmError) as caught:
        client.drop_family("chat")

    assert caught.value.code == VMM_PROTOCOL_MISMATCH
    assert str(HOST_API_PROTOCOL + 1) in str(caught.value)
    assert len(seen) == MISMATCH_TRIES
    assert pauses == [MISMATCH_PAUSE_S] * (MISMATCH_TRIES - 1)


def test_a_mismatch_then_an_answer_drops(pauses: list[float]) -> None:
    replies = iter(
        [
            httpx.Response(412, content=json.dumps(MISMATCH).encode()),
            httpx.Response(200, content=b"{}"),
        ]
    )
    client = _client(httpx.MockTransport(lambda _request: next(replies)))

    client.drop_family("chat")

    assert pauses == [MISMATCH_PAUSE_S]


def test_a_vmm_that_does_not_answer_is_unreachable() -> None:
    """Decision CH20: ENOENT, ECONNREFUSED and EOF are transient."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no socket", request=request)

    with pytest.raises(VmmError) as caught:
        _client(httpx.MockTransport(refuse)).drop_family("chat")

    assert caught.value.code == UNREACHABLE


@pytest.mark.parametrize(
    "status, body",
    [
        (200, b"not json"),
        (200, b'{"extra": 1}'),
        (500, b'{"code": "no-such-code"}'),
        (412, b'{"code": "protocol_mismatch"}'),
        (200, b" " * (HOST_API_BODY_CAP + 1)),
    ],
    ids=["not-json", "not-dropped", "unknown-code", "mismatch-with-no-protocol", "over-the-cap"],
)
def test_a_body_that_does_not_read_is_unreadable(
    status: int, body: bytes, pauses: list[float]
) -> None:
    with pytest.raises(VmmError) as caught:
        _client(_replying(status, body, [])).drop_family("chat")

    assert caught.value.code == UNREADABLE
    assert pauses == []


def test_the_socket_comes_from_the_env_key(tmp_path: Path) -> None:
    socket = tmp_path / "vmm.sock"

    assert socket_from({VMM_SOCKET_ENV: str(socket)}) == socket
    assert socket_from({VMM_SOCKET_ENV: ""}) is None
    assert socket_from({}) is None


class _DropHandler(BaseHTTPRequestHandler):
    """A vmm that answers `Dropped` to each request, and keeps each body."""

    bodies: ClassVar[list[bytes]] = []

    def do_POST(self) -> None:
        length = int(self.headers["Content-Length"])
        type(self).bodies.append(self.rfile.read(length))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, format: str, *args: object) -> None:
        del format, args


class _UnixHttpServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


@pytest.fixture
def vmm_socket() -> Iterator[Path]:
    """A stand-in vmm on a Unix socket of its own, in a short directory of
    its own: the path of a Unix socket has at most 104 bytes on macOS."""
    directory = Path(tempfile.mkdtemp(prefix="vmm-sock-"))
    socket = directory / "vmm.sock"
    _DropHandler.bodies.clear()
    server = _UnixHttpServer(str(socket), _DropHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield socket
    server.shutdown()
    server.server_close()
    shutil.rmtree(directory)


def test_the_client_dials_the_socket(vmm_socket: Path) -> None:
    HttpVmmClient.over_socket(vmm_socket).drop_family("chat")

    assert [json.loads(one) for one in _DropHandler.bodies] == [CHAT_DROP]
