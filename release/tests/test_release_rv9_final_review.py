"""Faults in the intake and the MCP build, one regression test each.

Every test here fails on code that has its fault.

Nothing here runs as root. The listener binds `127.0.0.1` on an ephemeral
port, every directory is `tmp_path`, and `owner_uid` is this test's own uid.
"""

from __future__ import annotations

import http.client
import shutil
import socket
import ssl
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest
from agent_release.errors import Refusal
from agent_release.intake import service
from agent_release.intake.gaps import MAX_GAPS_PER_PASS, GapDirectory, OpenGap
from agent_release.intake.run import MAX_PUSHES_PER_PASS, MAX_WATCHED, Watch, Wiring, one_pass
from agent_release.intake.service import Intake
from agent_release.intake.store import SecretStore
from agent_release.intake.token import Tokens
from agent_release.mcpserver import read_registry

pytestmark = pytest.mark.slow

LOOPBACK: Final = "127.0.0.1"

#: More servers than any of these tests declares.
REGISTRY_LIMIT: Final = 64

#: Short enough that a wedged accept loop is a fast test, long enough that
#: a loaded machine still answers a good request well inside half of it.
WEDGE_TIMEOUT_S: Final = 3.0

#: How long the accept loop gets to reach the handshake before the real
#: client connects. Without it the test races its own wedge.
SETTLE_S: Final = 0.3

HTTP_NOT_FOUND: Final = 404

#: One byte of a TLS record header. Enough for the server to start a
#: handshake, never enough for it to finish one.
ONE_TLS_BYTE: Final = b"\x16"


def _certificate(tmp_path: Path) -> tuple[str, str]:
    """A throwaway self-signed certificate for loopback.

    `openssl` and not a library, for `test_release_r7g_intake_whole.py`'s
    reason: the intake package imports no certificate builder and this test
    must not add a dependency to it.
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


def _seal_nothing(plaintext: bytes) -> bytes | None:
    del plaintext

    return None


@pytest.fixture
def running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    """A real intake on loopback, with a short connection clock.

    `CONNECTION_TIMEOUT_S` is patched before `serve`, because `_handler_for`
    reads it when it builds the handler class and `get_request` reads it per
    connection.
    """
    monkeypatch.setattr(service, "CONNECTION_TIMEOUT_S", WEDGE_TIMEOUT_S)
    secrets = tmp_path / "secrets"
    secrets.mkdir(mode=0o700)
    intake = Intake(
        tokens=Tokens(time.time),
        store=SecretStore(secrets, _seal_nothing, owner_uid=tmp_path.stat().st_uid),
    )
    certfile, keyfile = _certificate(tmp_path)
    server = service.serve(intake, certfile, keyfile, LOOPBACK, 0)
    thread = threading.Thread(target=server.serve_forever, name="rv9-intake", daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=WEDGE_TIMEOUT_S)


def test_a_silent_peer_does_not_hold_the_accept_loop(running: int) -> None:
    """Why moving the handshake off the listening socket is half a fix.

    A handshake moved off the LISTENING socket, with a timeout on the
    accepted one, can still sit in the accept loop: `socketserver` calls
    `get_request()` in the accept thread, before `process_request` spawns
    anything, so a peer that connects and says one byte still stops every
    other connection — for `CONNECTION_TIMEOUT_S` instead of for ever.

    With the handshake in the loop the good request answers after the
    wedge's whole timeout. A test that drives `accept_one` with a fake
    socket and checks only the ORDER of two calls cannot see it.
    """
    wedge = socket.create_connection((LOOPBACK, running), timeout=WEDGE_TIMEOUT_S)
    try:
        wedge.sendall(ONE_TLS_BYTE)
        time.sleep(SETTLE_S)

        started = time.monotonic()
        connection = _client(running)
        try:
            connection.request("GET", "/secret/no-such-token/")
            reply = connection.getresponse()
            reply.read()
            status = reply.status
        finally:
            connection.close()

        elapsed = time.monotonic() - started

        assert status == HTTP_NOT_FOUND
        assert elapsed < WEDGE_TIMEOUT_S / 2, f"the accept loop was held for {elapsed:.2f}s"
    finally:
        wedge.close()


def test_a_plain_http_probe_costs_one_connection(running: int) -> None:
    """The other half of that move: a handshake that fails now fails in a
    worker thread, so it must close that one connection there and leave the
    listener serving. An earlier test proved this against a fake socket in
    `accept_one`; the handshake is not there any more, so it is proved
    against a real listener instead.
    """
    probe = socket.create_connection((LOOPBACK, running), timeout=WEDGE_TIMEOUT_S)
    try:
        probe.sendall(b"GET /secret/ HTTP/1.1\r\nHost: x\r\n\r\n")
        # A close or a reset, never an answer: the handshake refused it.
        try:
            answered = probe.recv(1)
        except ConnectionResetError:
            answered = b""

        assert answered == b"", "a plain HTTP request was answered on a TLS port"
    finally:
        probe.close()

    connection = _client(running)
    try:
        connection.request("GET", "/secret/no-such-token/")
        reply = connection.getresponse()
        reply.read()

        assert reply.status == HTTP_NOT_FOUND
    finally:
        connection.close()


def _client(port: int) -> http.client.HTTPSConnection:
    """A browser that accepts a self-signed certificate, which is what
    the operator's phone does until they trust the certificate once."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    return http.client.HTTPSConnection(LOOPBACK, port, context=context, timeout=WEDGE_TIMEOUT_S * 3)


# -- one secret name, two upstreams --------------------------------------


def _server_yaml(name: str, secrets: dict[str, str]) -> str:
    """One valid `mcp/<name>/server.yaml` with the `run.env` a test needs."""
    env = "".join(f"    {key}: secret:{value}\n" for key, value in secrets.items())

    return (
        f"name: {name}\n"
        'identity: "A server this test declares."\n'
        "install:\n"
        "  source: pypi\n"
        "  package: a-package\n"
        "  version: 1.0.0\n"
        f"  lock: mcp/{name}/install.lock\n"
        '  python: "3.12"\n'
        "run:\n"
        "  entrypoint: a-package\n"
        "  env:\n"
        f"{env}"
        "tools:\n"
        "  - name: a_tool\n"
        "    description: One tool.\n"
    )


def _registry(tmp_path: Path, servers: dict[str, dict[str, str]]) -> Path:
    root = tmp_path / "mcp"
    for name, secrets in servers.items():
        directory = root / name
        directory.mkdir(parents=True)
        (directory / "server.yaml").write_text(_server_yaml(name, secrets), encoding="utf-8")

    return root


def test_two_servers_may_not_name_one_secret(tmp_path: Path) -> None:
    """`stage7-releases.md` §6 row 8.

    Row 8 says a second binding "is refused by the manifest validator".
    `mcp_release.open_gaps` takes a `server -> names` map with no check
    across servers, so `read_registry` keeps the names a secret-shaped key
    uses and refuses a second server that names one. The gap file is keyed
    on the SECRET name, so a second declaration would also overwrite the
    first one's gap and show the operator one server.

    The attack: `mcp/evil/server.yaml` names `secret:github_token`, and a
    third-party package reads the platform's GitHub credential out of its
    own environment.
    """
    root = _registry(
        tmp_path,
        {"kagi": {"KAGI_API_KEY": "kagi_api_key"}, "evil": {"ANYTHING": "kagi_api_key"}},
    )

    with pytest.raises(Refusal) as refused:
        read_registry(root, REGISTRY_LIMIT)

    detail = str(refused.value)

    assert "kagi_api_key" in detail
    assert "evil" in detail
    assert "kagi" in detail


def test_one_server_may_name_its_own_secret_twice(tmp_path: Path) -> None:
    """Two variables of one server holding one credential is ordinary. The
    rule is one UPSTREAM per name, not one variable."""
    root = _registry(tmp_path, {"kagi": {"KAGI_API_KEY": "kagi_api_key", "ALSO": "kagi_api_key"}})
    found = read_registry(root, REGISTRY_LIMIT)

    assert [one.name for one in found] == ["kagi"]
    assert found[0].secrets == ("kagi_api_key",)


# -- the lock pins the requirement, or only mentions it ------------------

#: A hash line, so `_closure_of`'s second rule is satisfied either way and
#: the test is about the first one.
HASH_LINE: Final = "    --hash=sha256:" + "0" * 64

DECLARED: Final = "kagimcp==1.0.2"


def _closure(name: str, text: str, requirement: str) -> tuple[str, ...]:
    from agent_release.executor.mcpbuild import _closure_of

    return _closure_of(name, text, requirement)


def test_a_lock_that_only_mentions_the_requirement_is_refused() -> None:
    """Contract 01b §3.4 rule 5 says a lock that does
    not pin the declared requirement is refused, and `_closure_of` proved
    it with `requirement.lower() not in text.lower()` — a substring test
    over the whole file, comments included.

    A `uv pip compile` lock is mostly comments, and the compiler writes
    the requirement into one of them itself (`# via kagimcp`). So a lock
    whose real requirement lines install something else passed, the ledger
    and `ServerBuild.artifact` still said `kagimcp==1.0.2`, and the phone
    showed X while the executor deployed Y.
    """
    text = f"# uv pip compile, and this comment names {DECLARED}\nevil==1.0.0 \\\n{HASH_LINE}\n"

    with pytest.raises(Refusal) as refused:
        _closure(DECLARED, text, DECLARED)

    assert "does not pin" in str(refused.value)


def test_a_lock_whose_requirement_line_pins_it_is_kept() -> None:
    """The shape `uv pip compile --generate-hashes` actually writes: the
    requirement at column zero with a continuation, hashes indented under
    it, and a `# via` note."""
    text = (
        "# This file was autogenerated by uv via the following command:\n"
        "#    uv pip compile --generate-hashes requirements.in\n"
        "--index-url https://pypi.org/simple\n"
        f"{DECLARED} \\\n"
        f"{HASH_LINE}\n"
        "    # via -r requirements.in\n"
    )

    assert _closure(DECLARED, text, DECLARED) == (HASH_LINE.strip(),)


def test_a_longer_version_does_not_satisfy_the_pin() -> None:
    """`startswith` alone would let `kagimcp==1.0.21` answer for
    `kagimcp==1.0.2`."""
    text = f"{DECLARED}1 \\\n{HASH_LINE}\n"

    with pytest.raises(Refusal):
        _closure(DECLARED, text, DECLARED)


# -- one write, sixty-four notifications ---------------------------------


class _Phone:
    """Every push root sent, so a test can count them."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def __call__(self, gap: OpenGap, token: str) -> bool:
        del token
        self.sent.append(gap.secret)

        return True


class _AlwaysBound:
    @property
    def bound(self) -> bool:
        return True

    def open(self) -> bool:
        return True

    def close(self) -> None:
        return


def _flooded(tmp_path: Path, count: int) -> tuple[Wiring, _Phone]:
    """A gap directory with `count` planted gaps, as the operator can write it."""
    gaps = tmp_path / "secret-gaps"
    gaps.mkdir()
    secrets = tmp_path / "secrets"
    secrets.mkdir(mode=0o700)
    for index in range(count):
        body = f'{{"server": "evil", "secret": "planted_{index:03d}", "at": 1.0}}'
        (gaps / f"planted_{index:03d}.json").write_text(body, encoding="utf-8")

    uid = tmp_path.stat().st_uid
    phone = _Phone()

    return (
        Wiring(
            gaps=GapDirectory(gaps, uid),
            store=SecretStore(secrets, _seal_nothing, owner_uid=uid),
            tokens=Tokens(time.time),
            push=phone,
            watch=Watch(time.time),
            listener=_AlwaysBound(),
            say=lambda line: None,
        ),
        phone,
    )


def test_one_write_cannot_cost_the_operator_a_pass_of_pushes(tmp_path: Path) -> None:
    """Without a cap `one_pass` mints and pushes for every due gap.
    `gaps.MAX_GAPS_PER_PASS` is 64 and the directory is operator-writable, so
    one write would send the operator 64 notifications at once.
    `Watch.due` paces a repeat of one NAME, and a writer that wants a
    flood uses a new name each round.

    It would also keep `pending_count()` above zero, so the root listener
    on the LAN would stay bound instead of only while the operator has something
    to answer.
    """
    wiring, phone = _flooded(tmp_path, MAX_GAPS_PER_PASS)
    minted, closed = one_pass(wiring)

    assert closed == 0
    assert minted == MAX_PUSHES_PER_PASS
    assert len(phone.sent) == MAX_PUSHES_PER_PASS


def test_the_pushed_roster_does_not_grow_without_bound(tmp_path: Path) -> None:
    """A dict keyed on a name the attacker writes, in a process that runs
    for days."""
    wiring, _ = _flooded(tmp_path, 1)
    watch = wiring.watch
    for index in range(MAX_WATCHED * 2):
        watch.pushed(OpenGap("evil", f"planted_{index:05d}", 1.0))

    assert len(watch.pushed_at) <= MAX_WATCHED


# -- the seam nobody had run end to end ----------------------------------

PASTED: Final = "sk-live-0000-a-real-looking-upstream-credential"
SECRET_NAME: Final = "kagi_api_key"


def _sops_and_keygen() -> tuple[str, str]:
    sops = shutil.which("sops")
    keygen = shutil.which("age-keygen")
    if sops is None or keygen is None:
        missing = "sops" if sops is None else "age-keygen"
        pytest.skip(f"{missing} is not on this machine, so the seal cannot be run here")

    return sops, keygen


def _sealed_store(tmp_path: Path) -> tuple[Path, Path]:
    """A secrets directory holding one file that a REAL `sops` sealed, with
    `seal_argv`'s own argv. Answers the directory and the private key."""
    from agent_release.executor.host import seal_argv

    sops, keygen = _sops_and_keygen()
    key_file = tmp_path / "key.txt"
    made = subprocess.run(
        [keygen, "-o", str(key_file)], capture_output=True, timeout=60, check=True
    )
    public = made.stderr.decode("utf-8").split(": ", 1)[1].strip()

    sealed = subprocess.run(
        [sops, *seal_argv((public,))[1:]],
        input=PASTED.encode("utf-8"),
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        cwd=str(tmp_path),
        capture_output=True,
        timeout=60,
        check=True,
    )

    store = tmp_path / "secrets"
    store.mkdir()
    (store / f"{SECRET_NAME}.enc").write_bytes(sealed.stdout)

    return store, key_file


def test_the_pep_reads_back_what_the_intake_sealed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two halves of "one action" meet here and nowhere else.

    `test_release_r7f_invariant18.py` writes the PLAINTEXT into
    `<name>.enc` and calls that the operator's paste, so the seal — which is
    ROOT's step and the thing under test — is not in it at all.
    `test_release_r7g_sealer_sops.py` seals for real and reads back with
    `sops --decrypt --input-type binary --output-type binary`, which is
    not the PEP's command: `secrets._decrypt_one` runs `sops -d <path>`
    and lets the `.enc` extension pick the store on both ends.

    So this runs the real seal and the PEP's own loader over it.
    """
    from chaperone.secrets import load_secret_dir

    store, key_file = _sealed_store(tmp_path)
    monkeypatch.setenv("SOPS_AGE_KEY_FILE", str(key_file))

    assert load_secret_dir(store) == {SECRET_NAME: PASTED}


def test_a_planted_sops_config_cannot_wedge_the_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The opposite of the seal's wedge, and the reason the PEP needs no
    `--config /dev/null`.

    On the ENCRYPT side a `.sops.yaml` above the child's working directory
    whose `creation_rules` match nothing makes `sops` exit 1, which is why
    `seal_argv` names the config explicitly. Measured here: the DECRYPT
    side ignores it, so the PEP reading from a checkout that carries one
    is not a wedge. The next reader does not have to re-run this.
    """
    store, key_file = _sealed_store(tmp_path)
    (tmp_path / ".sops.yaml").write_text(
        "creation_rules:\n  - path_regex: matches-nothing\n    age: age1" + "0" * 58 + "\n",
        encoding="utf-8",
    )
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("SOPS_AGE_KEY_FILE", str(key_file))

    from chaperone.secrets import load_secret_dir

    assert load_secret_dir(store) == {SECRET_NAME: PASTED}
