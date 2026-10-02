"""The intake's connection cap.

A LAN peer costs one THREAD rather than the accept loop, and
`ThreadingHTTPServer` caps neither the thread count nor the connection
count. Each thread dies at `CONNECTION_TIMEOUT_S`, so without a cap the
ceiling is the peer's connection rate times twenty seconds, in the root
process.

The cap is a dozen lines, and this file shows both halves: the cap holds,
and a peer that only opens connections cannot close the port for good.
"""

from __future__ import annotations

import contextlib
import socket
import ssl
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest
from agent_release.intake import service
from agent_release.intake.service import MAX_LIVE_CONNECTIONS, Intake
from agent_release.intake.store import SecretStore
from agent_release.intake.token import Tokens

pytestmark = pytest.mark.slow

LOOPBACK: Final = "127.0.0.1"
HTTP_NOT_FOUND: Final = 404

#: Short, so a wedged connection clears inside the test rather than after
#: the production twenty seconds.
WEDGE_TIMEOUT_S: Final = 3.0
SETTLE_S: Final = 0.4

#: How many times one flood probe retries a connect the kernel refused or
#: reset because the listen backlog was full. See `_flood_once`.
FLOOD_RETRIES: Final = 5
BACKLOG_BACKOFF_S: Final = 0.05

#: One byte of a TLS record header. Enough to start a handshake and never
#: enough to finish one, which is what holds a worker thread.
ONE_TLS_BYTE: Final = b"\x16"


def _seal_nothing(plaintext: bytes) -> bytes | None:
    del plaintext

    return None


def _certificate(tmp_path: Path) -> tuple[str, str]:
    import shutil
    import subprocess

    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl is not on this machine, so no TLS listener can be built here")

    cert, key = tmp_path / "intake.crt", tmp_path / "intake.key"
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            "/CN=agent-intake",
            "-addext",
            f"subjectAltName=IP:{LOOPBACK}",
        ],
        capture_output=True,
        timeout=60,
        check=True,
    )

    return str(cert), str(key)


@pytest.fixture
def running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    monkeypatch.setattr(service, "CONNECTION_TIMEOUT_S", WEDGE_TIMEOUT_S)
    secrets = tmp_path / "secrets"
    secrets.mkdir(mode=0o700)
    intake = Intake(
        tokens=Tokens(time.time),
        store=SecretStore(secrets, _seal_nothing, owner_uid=tmp_path.stat().st_uid),
    )
    certfile, keyfile = _certificate(tmp_path)
    server = service.serve(intake, certfile, keyfile, LOOPBACK, 0)
    thread = threading.Thread(target=server.serve_forever, name="r7h-intake", daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=WEDGE_TIMEOUT_S)


def _client(port: int) -> ssl.SSLSocket:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    raw = socket.create_connection((LOOPBACK, port), timeout=WEDGE_TIMEOUT_S)

    return context.wrap_socket(raw)


def _send_wedge_byte(peer: socket.socket) -> None:
    """One byte of a TLS hello, to a server that may already have hung up.

    Under `pytest -n auto` the accept loop lags, and the server's handshake
    timeout or its cap can close a wedge before this loop reaches it. Then
    `sendall` raises BrokenPipeError or ConnectionResetError out of the
    SET-UP, and the assertion this test exists for is never reached. Measured
    2026-09-20: one failure in one `-n auto` run on the merged tree, 0 in 3
    runs of this file alone. A wedge the server already closed costs root
    nothing, which is what the cap is for, so the send may fail quietly.
    """
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        peer.sendall(ONE_TLS_BYTE)


def _connect_wedge(port: int) -> socket.socket:
    """One plain TCP connect, retried past a refused or reset one.

    The same fact `_flood_once` records, met one step earlier: under
    `pytest -n auto` the accept loop lags, the listen backlog fills, and macOS
    answers a pending `connect()` with RST rather than queuing it. That raised
    ConnectionResetError out of the SET-UP of the cap test while other
    suites run on the same machine, and 0 failures in 5 runs of the whole
    gate on an idle one. The property under test is unchanged: every wedge still
    connects, and the extra one must still cost root no thread.
    """
    for attempt in range(FLOOD_RETRIES):
        try:
            return socket.create_connection((LOOPBACK, port), timeout=WEDGE_TIMEOUT_S)
        except OSError:
            if attempt == FLOOD_RETRIES - 1:
                raise

            time.sleep(BACKLOG_BACKOFF_S)

    raise AssertionError("unreachable: the last attempt returns or raises")


def test_the_cap_bounds_what_one_peer_costs_root(running: int) -> None:
    """Without the cap every one of these connections gets its own thread
    in the root process and holds it for the whole timeout."""
    wedges = [_connect_wedge(running) for _ in range(MAX_LIVE_CONNECTIONS)]
    try:
        for one in wedges:
            _send_wedge_byte(one)

        time.sleep(SETTLE_S)
        live = threading.active_count()
        extra = _connect_wedge(running)
        try:
            _send_wedge_byte(extra)
            time.sleep(SETTLE_S)

            # The cap is what holds: the extra connection cost no thread.
            assert threading.active_count() <= live
        finally:
            extra.close()
    finally:
        for one in wedges:
            one.close()


def _flood_once(port: int) -> None:
    """One probe of the flood, retried past a refused or reset connect.

    The cap closes anything over `MAX_LIVE_CONNECTIONS` live, and this loop
    opens three times that with no pacing. Under `pytest -n auto` the accept
    loop lags behind, the listen backlog fills, and macOS answers a pending
    `connect()` with RST rather than queuing it — which raised out of the
    flood LOOP, so the assertion this test exists for was never reached.
    Measured 2026-09-20: 2 failures in 4 `-n auto` runs, 0 in 3 runs of this
    file alone, always at the `create_connection` below and never at the good
    request afterwards. The property under test is unchanged: the flood still
    happens, and the port must still answer when it is over.
    """
    for attempt in range(FLOOD_RETRIES):
        try:
            probe = socket.create_connection((LOOPBACK, port), timeout=WEDGE_TIMEOUT_S)
        except OSError:
            if attempt == FLOOD_RETRIES - 1:
                raise

            time.sleep(BACKLOG_BACKOFF_S)
            continue

        try:
            probe.sendall(b"GET / HTTP/1.1\r\n\r\n")
        except OSError:
            # The cap closed it before the write landed, which is the cap
            # doing its job. The connection still counted as one probe.
            pass
        finally:
            probe.close()

        return


def test_a_flood_cannot_close_the_port_for_good(running: int) -> None:
    """The other half. A cap that leaked a slot per failed handshake would
    be worse than the wedge it replaced: the port would answer nothing
    afterwards, and the operator's one paste is what the port exists for."""
    for _ in range(MAX_LIVE_CONNECTIONS * 3):
        _flood_once(running)

    time.sleep(SETTLE_S)
    connection = _client(running)
    try:
        connection.sendall(b"GET /secret/no-such-token/ HTTP/1.1\r\nHost: x\r\n\r\n")
        answer = connection.recv(64)
    finally:
        connection.close()

    assert str(HTTP_NOT_FOUND).encode() in answer, answer
