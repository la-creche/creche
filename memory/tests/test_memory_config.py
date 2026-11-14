"""The service reads its settings from its environment and fails closed."""

import getpass
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from concurrent.futures import Future
from pathlib import Path

import pytest
from creche_memory.cli import EX_CONFIG, EX_OK, EX_TEMPFAIL, ended, main
from creche_memory.config import ConfigError, check, from_env
from creche_memory.graph import GraphCheck, GraphState, side_files
from creche_memory.search import STOP_TIMEOUT_S, StoreLoop
from creche_memory.sync import Syncer
from memory_graph_helpers import (
    GRAPH_NAME,
    group_left,
    held_open,
    kill_graph,
    ladybug_check,
    leave_recoverable,
    make_graph,
)

KEY = "sk-test-not-a-real-key"
#: The start imports Cognee and opens the graph database before the socket.
START_TIMEOUT_S = 60.0


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
    assert config.places.peer_uid == os.getuid()
    assert config.store.tokenizer_cache == host / "store" / "hf" / "hub"
    assert check(config.places) == []


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

    problems = check(config.places)

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


def _start(root: Path) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-m", "creche_memory.cli"],
        env={**os.environ, **_env(root), "HOME": str(root)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )


def _graph_file(root: Path) -> Path:
    return root / "store" / "system" / "databases" / GRAPH_NAME


def test_the_service_serves_a_read_and_stops_on_sigterm(host: Path) -> None:
    sock = host / "run" / "memory.sock"
    service = _start(host)
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
    # The stop closed the graph database, and the close ended its worker.
    lines = [one for one in err.splitlines() if one.startswith("creche-memory: ")]
    assert len(lines) == 1, err[-2000:]
    assert lines[0].startswith("creche-memory: The graph database closed in ")
    assert not group_left(service.pid)
    assert side_files(_graph_file(host)) == ()


def test_a_dead_graph_stops_the_start_with_exit_78_and_one_line(host: Path) -> None:
    # Cognee reports the path with each symlink resolved.
    graph_file = make_graph(_graph_file(host)).resolve()
    kill_graph(graph_file)
    wal = graph_file.with_name(f"{GRAPH_NAME}.wal")
    wal.write_bytes(b"\x5a" * 42)

    service = _start(host)
    _, err = service.communicate(timeout=START_TIMEOUT_S)

    assert service.returncode == EX_CONFIG, err[-2000:]
    assert not (host / "run" / "memory.sock").exists()
    lines = [one for one in err.splitlines() if one.startswith("creche-memory: ")]
    assert len(lines) == 1, err[-2000:]
    assert lines[0].startswith(f"creche-memory: The graph database {graph_file} does not open: ")
    assert "Checksum verification failed" in lines[0]
    assert f"To recover, set aside {graph_file.parent}. Then start the unit." in lines[0]
    # The report names the files from before the open.
    assert f"Files beside it: {GRAPH_NAME}.wal, {GRAPH_NAME}.wal.checkpoint, " in lines[0]
    # Cognee's open removes the `.wal` file when the error names the WAL.
    # The service itself moves and removes no other file of the store.
    assert not wal.exists()
    assert (graph_file.parent / f"{GRAPH_NAME}.wal.checkpoint").exists()
    assert KEY not in err


def test_a_lock_at_the_start_check_exits_75_with_one_line(host: Path) -> None:
    graph_file = make_graph(_graph_file(host))

    with held_open(graph_file):
        service = _start(host)
        _, err = service.communicate(timeout=START_TIMEOUT_S)

    assert service.returncode == EX_TEMPFAIL, err[-2000:]
    assert not (host / "run" / "memory.sock").exists()
    lines = [one for one in err.splitlines() if one.startswith("creche-memory: ")]
    assert len(lines) == 1
    # The line carries ladybug's text, so the journal names the cause.
    assert lines[0].startswith(
        "creche-memory: the graph check failed: NotAVerdict: "
        "IO exception: Could not set lock on file"
    )
    assert str(graph_file) in lines[0]
    # Ladybug's text ends in a period, and the line keeps only one.
    assert lines[0].endswith(
        "for more information. systemd restarts the unit. "
        "The next start checks the graph database again."
    )
    assert KEY not in err


def test_a_checkpoint_that_stopped_early_does_not_stop_the_start(host: Path) -> None:
    leave_recoverable(make_graph(_graph_file(host)))
    sock = host / "run" / "memory.sock"

    service = _start(host)
    try:
        deadline = time.monotonic() + START_TIMEOUT_S
        while not sock.exists():
            assert service.poll() is None, service.communicate()[1][-2000:]
            assert time.monotonic() < deadline, "the socket did not appear"
            time.sleep(0.1)
    finally:
        service.send_signal(signal.SIGTERM)
        _, err = service.communicate(timeout=START_TIMEOUT_S)

    assert service.returncode == EX_OK, err[-2000:]
    assert not (graph_file := _graph_file(host)).with_name(graph_file.name + ".shadow").exists()


async def _failing_sync(folders: list[str]) -> None:
    raise ConnectionError("http://secret.test/ refused")


def _end_of_sync_loop(root: Path, check: GraphCheck) -> int:
    """Run a real sync loop whose sync fails, as `_serve` runs it, then
    read its exit status as `_serve` does after the stop."""
    (root / "profile" / "memories").mkdir(parents=True)
    (root / "profile" / "memories" / "a.md").write_text("a", encoding="utf-8")
    syncer = Syncer(root, _failing_sync, check, poll_s=60.0, quiet_s=0.0)
    loop = StoreLoop()
    loop.start()
    stop = threading.Event()
    try:
        syncing = loop.spawn(syncer.run(stop))
        assert stop.wait(START_TIMEOUT_S)
        syncer.wake()
        return ended(syncing, STOP_TIMEOUT_S)
    finally:
        loop.stop()


def test_an_error_of_the_check_after_a_failed_sync_exits_75_with_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    async def wedged() -> GraphState:
        raise TimeoutError("Subprocess call_async exceeded 30s deadline")

    assert _end_of_sync_loop(tmp_path / "vault", wedged) == EX_TEMPFAIL

    err = capsys.readouterr().err
    lines = [one for one in err.splitlines() if one.startswith("creche-memory: ")]
    assert lines == [
        "creche-memory: the sync loop failed: TimeoutError. "
        "systemd restarts the unit. The next start checks the graph database again."
    ]
    assert "secret.test" not in err


def test_a_dead_graph_after_a_failed_sync_exits_78_with_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    graph_file = make_graph(tmp_path / "databases" / GRAPH_NAME)
    kill_graph(graph_file)

    assert _end_of_sync_loop(tmp_path / "vault", ladybug_check(graph_file)) == EX_CONFIG

    lines = [one for one in capsys.readouterr().err.splitlines() if "creche-memory: " in one]
    assert len(lines) == 1
    assert lines[0].startswith(f"creche-memory: The graph database {graph_file} does not open: ")


def test_a_sync_loop_still_in_a_sync_after_a_stop_exits_0() -> None:
    assert ended(Future(), 0.01) == EX_OK
