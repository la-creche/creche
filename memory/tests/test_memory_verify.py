"""The verify hook passes only when the service could serve right now."""

import getpass
import json
import shutil
import socket
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from creche_memory import verify
from creche_memory.verify import EXIT_FAILED, EXIT_OK, main

KEY = "sk-test-not-a-real-key"


@pytest.fixture
def host() -> Iterator[Path]:
    # A Unix socket path has a short limit, so the host lives in a short
    # temp folder, not in pytest's tmp_path.
    root = Path(tempfile.mkdtemp(prefix="memv"))
    (root / "vault").mkdir()
    (root / "store" / "hf" / "hub").mkdir(parents=True)
    (root / "run").mkdir()
    yield root
    shutil.rmtree(root)


def _env_file(root: Path, *extra: str, key: str = KEY) -> Path:
    lines = [
        "# the env file of the unit",
        f"MEMORY_LITELLM_KEY={key}",
        f"MEMORY_VAULT_ROOT={root / 'vault'}",
        f"MEMORY_STORE_ROOT={root / 'store'}",
        f"MEMORY_SOCKET={root / 'run' / 'memory.sock'}",
        f"MEMORY_PEER_USER={getpass.getuser()}",
        *extra,
    ]
    path = root / "memory.env"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return path


def _listen(root: Path) -> socket.socket:
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(root / "run" / "memory.sock"))
    server.listen(1)

    return server


def _verdict(capsys: pytest.CaptureFixture[str], code: int) -> dict[str, object]:
    out = capsys.readouterr().out
    assert KEY not in out

    verdict = json.loads(out)
    assert verdict["ok"] is (code == EXIT_OK)

    return verdict


def test_a_listening_service_passes(host: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with _listen(host):
        code = main(["--json", "--env-file", str(_env_file(host))])

    assert code == EXIT_OK
    _verdict(capsys, code)


def test_no_socket_fails(
    host: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(verify, "SOCKET_WAIT_S", 0.5)
    code = main(["--json", "--env-file", str(_env_file(host))])

    assert code == EXIT_FAILED
    checks = _verdict(capsys, code)["checks"]
    assert isinstance(checks, list)
    assert [one["name"] for one in checks if not one["ok"]] == ["socket"]  # pyright: ignore


@pytest.mark.parametrize(
    ("extra", "key"),
    [((), ""), (("not a key line",), KEY), (("MEMORY_PEER_USER=no-such-user-here",), KEY)],
    ids=["no key", "a bad line", "an unknown peer"],
)
def test_a_bad_env_file_fails(
    host: Path, capsys: pytest.CaptureFixture[str], extra: tuple[str, ...], key: str
) -> None:
    with _listen(host):
        code = main(["--json", "--env-file", str(_env_file(host, *extra, key=key))])

    assert code == EXIT_FAILED
    _verdict(capsys, code)


def test_a_missing_env_file_fails(host: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["--json", "--env-file", str(host / "none.env")])

    assert code == EXIT_FAILED
    _verdict(capsys, code)


def test_a_socket_that_appears_soon_passes(host: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The executor runs the hook right after it restarts the unit, and the
    service binds its socket about a second later."""
    listening: list[socket.socket] = []
    timer = threading.Timer(1.0, lambda: listening.append(_listen(host)))
    timer.start()
    try:
        code = main(["--json", "--env-file", str(_env_file(host))])
    finally:
        timer.join()
        for one in listening:
            one.close()

    assert code == EXIT_OK
    _verdict(capsys, code)
