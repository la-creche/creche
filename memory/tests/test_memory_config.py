"""The service reads its settings from its environment and fails closed."""

import getpass
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from creche_memory.cli import EX_CONFIG, EX_OK, main
from creche_memory.config import ConfigError, check, from_env

KEY = "sk-test-not-a-real-key"
START_TIMEOUT_S = 20.0


@pytest.fixture
def host() -> Iterator[Path]:
    # A Unix socket path has a short limit, so the host lives in a short
    # temp folder, not in pytest's tmp_path.
    root = Path(tempfile.mkdtemp(prefix="memh"))
    (root / "vault" / "profile").mkdir(parents=True)
    (root / "vault" / "profile" / "dentist.md").write_text("Oak St.\n", encoding="utf-8")
    (root / "store" / "hf" / "hub").mkdir(parents=True)
    (root / "run").mkdir()
    yield root
    subprocess.run(["rm", "-rf", str(root)], check=True)


def _env(root: Path, **changes: str) -> dict[str, str]:
    env = {
        "MEMORY_LITELLM_KEY": KEY,
        "AGENT_LAN_ADDRESS": "192.0.2.10",
        "MEMORY_VAULT_ROOT": str(root / "vault"),
        "MEMORY_STORE_ROOT": str(root / "store"),
        "MEMORY_SOCKET": str(root / "run" / "memory.sock"),
        "MEMORY_PEER_USER": getpass.getuser(),
    }

    return env | changes


def test_the_addresses_default_to_the_lan_address(host: Path) -> None:
    config = from_env(_env(host))

    assert config.store.llm_base_url == "http://192.0.2.10:4000"
    assert config.store.embedding_base_url == "http://192.0.2.10:8085"
    assert config.peer_uid == os.getuid()
    assert config.store.tokenizer_cache == host / "store" / "hf" / "hub"
    assert check(config) == []


def test_an_explicit_address_wins(host: Path) -> None:
    config = from_env(
        _env(host, AGENT_LITELLM_BASE_URL="https://llm.test", TEI_URL="http://e.test")
    )

    assert config.store.llm_base_url == "https://llm.test"
    assert config.store.embedding_base_url == "http://e.test"


@pytest.mark.parametrize(
    "changes",
    [
        {"MEMORY_LITELLM_KEY": ""},
        {"AGENT_LAN_ADDRESS": ""},
        {"MEMORY_VAULT_ROOT": "relative/vault"},
        {"MEMORY_PEER_USER": "no-such-user-of-this-test"},
        {"AGENT_LITELLM_BASE_URL": "http://llm.test/v1"},
    ],
    ids=["no key", "no address", "a relative vault", "an unknown peer", "a URL with /v1"],
)
def test_a_bad_environment_stops_the_start(host: Path, changes: dict[str, str]) -> None:
    with pytest.raises(ConfigError):
        from_env(_env(host, **changes))


def test_the_key_is_not_in_the_repr(host: Path) -> None:
    assert KEY not in repr(from_env(_env(host)))


def test_check_names_each_missing_folder(host: Path) -> None:
    config = from_env(_env(host, MEMORY_VAULT_ROOT=str(host / "none")))
    (host / "store" / "hf" / "hub").rmdir()

    problems = check(config)

    assert len(problems) == 2
    assert any("vault" in one for one in problems)
    assert any("tokenizer" in one for one in problems)


def test_the_check_flag_exits_0_or_78(host: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in _env(host).items():
        monkeypatch.setenv(name, value)

    assert main(["--check"]) == EX_OK

    monkeypatch.setenv("MEMORY_LITELLM_KEY", "")
    assert main(["--check"]) == EX_CONFIG
    assert main(["--other"]) == EX_CONFIG


def test_the_service_serves_a_read_and_stops_on_sigterm(host: Path) -> None:
    sock = host / "run" / "memory.sock"
    service = subprocess.Popen(
        [sys.executable, "-m", "creche_memory.cli"],
        env={**os.environ, **_env(host), "HOME": str(host)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + START_TIMEOUT_S
        while not sock.exists():
            assert service.poll() is None, service.communicate()[1][-2000:]
            assert time.monotonic() < deadline, "the socket did not appear"
            time.sleep(0.1)

        request = {"v": 1, "op": "read", "caller": "chat", "path": "profile/dentist.md"}
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(sock))
            client.sendall(json.dumps(request).encode() + b"\n")
            reply = json.loads(client.makefile("rb").readline())
    finally:
        service.send_signal(signal.SIGTERM)
        _, err = service.communicate(timeout=START_TIMEOUT_S)

    assert reply["text"] == "Oak St.\n"
    assert service.returncode == EX_OK, err[-2000:]
    assert KEY not in err
