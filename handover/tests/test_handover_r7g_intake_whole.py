"""§4.3 end to end: gap, token, link, paste, seal, gap closed, request filed.

This is the one test that walks the whole of the root half with nothing
stubbed between the parts. It binds a REAL TLS listener on loopback, drives
it with a real HTTPS client, and reads what landed on disk afterwards. Two
things are fakes, because a test may not touch the real one: the phone (a
recorder, so the test can read the link out of the push) and `sops` (a
stub, because a real seal is `test_handover_r7g_sealer_sops.py` and belongs
there).

Nothing runs as root. The listener binds `127.0.0.1` on an ephemeral port,
the directories are `tmp_path`, and `owner_uid` is this test's own uid.
Production binds the host's LAN address, port 8380, and checks `owner_uid=0`, which is the
same code with two different numbers.

The last assertion is the one §4.3 exists for: **the value is present in no
file this test can find except the sealed one**, and in no log line, and in
no URL.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import ssl
import subprocess
from pathlib import Path
from typing import Final
from urllib.parse import urlencode, urlsplit

import pytest
from caregiver.mcp_release import McpPaths, open_gaps, request_servers
from handover.intake.gaps import GapDirectory
from handover.intake.notify import make_push
from handover.intake.run import Listener, Watch, Wiring, one_pass
from handover.intake.service import CONTENT_SECURITY_POLICY, Intake
from handover.intake.store import SecretStore
from handover.intake.token import Tokens

pytestmark = pytest.mark.slow

SERVER: Final = "weather"
NAME: Final = "weather_token"
VALUE: Final = "sk-live-0000-a-real-looking-upstream-credential"
SEALED: Final = b"-----BEGIN AGE ENCRYPTED FILE-----\nsealed bytes\n"
LOOPBACK: Final = "127.0.0.1"
CLIENT_TIMEOUT_S: Final = 10.0


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Phone:
    """The fake phone. It keeps every push whole, which is how the test
    learns the token: nothing else in this system ever answers with one."""

    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    def __call__(self, body: dict[str, object]) -> bool:
        self.sent.append(body)

        return True

    def last_link(self) -> str:
        fields = self.sent[-1]["fields"]
        assert isinstance(fields, dict)

        return str(fields["link"])


def _seal(plaintext: bytes) -> bytes | None:
    """The fake `sops`. It proves the plaintext reached exactly one place
    by recording nothing and answering fixed bytes."""
    assert plaintext == VALUE.encode("utf-8")

    return SEALED


def _certificate(tmp_path: Path) -> tuple[str, str]:
    """A throwaway self-signed certificate for `127.0.0.1`.

    `openssl` and not a library, because the intake package may not import
    a certificate builder and this test must not add a dependency to it.
    The real host's certificate is made the same way, by hand, once.
    """
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


def _client(port: int) -> http.client.HTTPSConnection:
    """A browser that accepts a self-signed certificate, which is what
    the operator's phone does on its first visit until they trust the certificate
    once. The test is about the intake, not about the PKI."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    return http.client.HTTPSConnection(LOOPBACK, port, context=context, timeout=CLIENT_TIMEOUT_S)


def _every_byte_under(root: Path) -> list[tuple[Path, bytes]]:
    return [(path, path.read_bytes()) for path in root.rglob("*") if path.is_file()]


def test_one_file_one_secret_and_one_tap(tmp_path: Path) -> None:
    gaps_dir = tmp_path / "secret-gaps"
    gaps_dir.mkdir()
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir(mode=0o700)
    installed = tmp_path / "opt-mcp"
    installed.mkdir()
    spool = tmp_path / "releases"
    (spool / "requests").mkdir(parents=True)
    (spool / "done").mkdir(parents=True)

    uid = os.getuid()
    paths = McpPaths(
        requests=spool / "requests",
        done=spool / "done",
        secrets=secrets_dir,
        gaps=gaps_dir,
        installed_root=installed,
        marker=tmp_path / "mcp-request.json",
    )

    # 1. caregiver sees a declared server with a secret that has no value.
    assert open_gaps(paths, {SERVER: (NAME,)}, 1.0) == (NAME,)
    assert (gaps_dir / f"{NAME}.json").exists()

    # 2. Root drains the gap, mints one token, pushes one link, binds.
    clock, phone = _Clock(), _Phone()
    certfile, keyfile = _certificate(tmp_path)
    tokens = Tokens(clock)
    directory = GapDirectory(gaps_dir, uid)
    store = SecretStore(secrets_dir, _seal, owner_uid=uid)
    intake = Intake(tokens=tokens, store=store, close_gap=directory.close_named)
    listener = Listener(intake, certfile, keyfile, LOOPBACK, 0)
    wiring = Wiring(
        gaps=directory,
        store=store,
        tokens=tokens,
        push=make_push("https://hook.invalid", "bearer", f"https://{LOOPBACK}:0", phone),
        watch=Watch(clock),
        listener=listener,
        say=lambda line: None,
    )
    try:
        assert one_pass(wiring) == (1, 0)
        assert listener.bound is True
        port = listener.port
        assert port > 0

        # 3. The operator taps the link. The token is in its path.
        token = urlsplit(phone.last_link()).path.strip("/").split("/")[-1]
        connection = _client(port)
        connection.request("GET", f"/secret/{token}/")
        form = connection.getresponse()
        page = form.read().decode("utf-8")

        assert form.status == 200
        assert form.getheader("Content-Security-Policy") == CONTENT_SECURITY_POLICY
        assert "<script" not in page.lower()
        assert SERVER in page
        assert NAME in page

        # 4. The operator pastes the value. It rides the body and nothing else.
        connection.request(
            "POST",
            "/secret",
            body=urlencode({"token": token, "name": NAME, "value": VALUE}).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        stored = connection.getresponse()
        answer = stored.read().decode("utf-8")
        connection.close()

        assert stored.status == 200
        assert answer == "stored"
        assert VALUE not in answer

        # 5. The value is sealed, root-owned and owner-only.
        sealed_file = secrets_dir / f"{NAME}.enc"
        assert sealed_file.read_bytes() == SEALED
        assert sealed_file.stat().st_mode & 0o777 == 0o600

        # 6. The gap is closed, and the token is spent.
        assert not (gaps_dir / f"{NAME}.json").exists()
        assert tokens.pending_count() == 0

        # 7. The next pass releases the port: nothing is waiting.
        assert one_pass(wiring) == (0, 0)
        assert listener.bound is False
    finally:
        listener.close()

    # 8. caregiver opens no gap for a name that has a value, and files the
    #    one `mcp-servers` request that installs the server.
    assert open_gaps(paths, {SERVER: (NAME,)}, 2.0) == ()
    assert request_servers(paths, (SERVER,), 3.0).servers == (SERVER,)
    (filed,) = list((spool / "requests").glob("*.json"))
    body = json.loads(filed.read_text(encoding="utf-8"))
    assert body["components"] == {"mcp-servers": "latest"}
    assert body["requested_by"] == "managerd"

    # 9. The whole point. The value is in no file but the sealed one, and
    #    the sealed one does not hold it either.
    for path, raw in _every_byte_under(tmp_path):
        assert VALUE.encode("utf-8") not in raw, path

    assert VALUE not in "\n".join(intake.log)
    assert VALUE not in json.dumps(phone.sent)
