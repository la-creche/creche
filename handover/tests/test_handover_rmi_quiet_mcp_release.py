"""An `mcp-servers` release installs the server trees and does nothing more.

The Rust chaperone reads `/etc/chaperone/install.toml` and one plain file
for each secret. It reads no roster, and it has no handler for `SIGHUP`.
A signal from root ends its process, and systemd then starts it again.

So root writes no roster and sends no signal, at the switch and at the
restore. A release that declares no server is the same. The operator adds
a server to chaperone by hand: one manifest, one entry in `install.toml`,
and `systemctl restart creche-chaperone.service`.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Final, cast

import pytest
from handover.catalog import CATALOG_BY_NAME
from handover.executor.approval import Decision, Summary, Verdict
from handover.executor.drain import Counter, handle
from handover.executor.host import Command
from handover.executor.host import Result as RunResult
from handover.executor.spool import DONE_DIR, Spool
from handover.executor.steps import Wiring
from handover_executor_fixtures import (
    SHA_OF,
    FakeRun,
    GitFake,
    fake_host,
    fake_readers,
    git_host_answers,
    green_api,
    make_spool_dirs,
    request_body,
    stamp_tree,
    this_uid,
    write_request,
)
from handover_fixtures import manifest_text
from handover_mcp_fixtures import LOCK_TEXT, McpFake

pytestmark = pytest.mark.slow

COMPONENT: Final = "mcp-servers"
LIVE_VERSION: Final = "0.9.3"
NEW_VERSION: Final = "0.9.4"
TAG: Final = f"{COMPONENT}-v{NEW_VERSION}"
SERVER: Final = "kagi"
NOW: Final = 1_758_153_600.0
VERIFY_HOOK: Final = f"{COMPONENT}-verify"
REQUEST_ID: Final = "01K5J8M2Q7V3X9R4T6N0B8C2DK"

#: The name of the file that the Python policy enforcement point read.
OLD_ROSTER_NAME: Final = "upstreams.yaml"

SERVER_YAML: Final = f"""
name: {SERVER}
identity: "A search key. Search and page extraction, no account writes."
install:
  source: pypi
  package: kagimcp
  version: 1.0.2
  lock: mcp/{SERVER}/install.lock
  python: "3.12"
run:
  entrypoint: kagimcp
  env:
    KAGI_API_KEY: secret:kagi_api_key
tools:
  - name: kagi_search_fetch
    description: Search the web and return ranked results with snippets.
"""

#: `mcp-servers` has one venv and no unit.
BUILD_LINE: Final = 'build:\n  - ["/usr/local/bin/uv", "sync", "--frozen"]\ninstall:'


class Declares:
    """How many servers the registry of a test declares."""

    ONE: Final = (SERVER,)
    NONE: Final = ()


def _manifest(components: Path) -> str:
    text = manifest_text(COMPONENT)
    text = text.replace("/opt/components", str(components))

    return text.replace("install:", BUILD_LINE, 1)


def _is_mcp_command(command: Command) -> bool:
    return command.argv[0].endswith(("/uv", "/curl", "/install", "/chown", "/chmod"))


def _teach(run: FakeRun, components: Path) -> None:
    """The fake clone writes the manifest. `McpFake` builds each server."""
    manifest = _manifest(components)
    mcp = McpFake()

    def write_tree(destination: Path, component: str) -> None:
        row = CATALOG_BY_NAME[component]
        sub = destination if row.path == "." else destination / row.path
        sub.mkdir(parents=True, exist_ok=True)
        (sub / "component.yaml").write_text(manifest, encoding="utf-8")

    git = GitFake(write_tree)

    def dynamic(command: Command) -> RunResult | None:
        answered = git(command)
        if answered is not None:
            return answered

        if command.argv[0].endswith("/uv") and "sync" in command.argv:
            stamp_tree(components, f"{COMPONENT}.new", NEW_VERSION)

            return RunResult(0, "", "")

        if _is_mcp_command(command):
            return mcp(command)

        return None

    run.dynamic = dynamic


def _registry(tmp_path: Path, servers: tuple[str, ...]) -> None:
    mcp = tmp_path / "corpus" / "registry" / "mcp"
    mcp.mkdir(parents=True, exist_ok=True)
    for name in servers:
        directory = mcp / name
        directory.mkdir()
        (directory / "server.yaml").write_text(SERVER_YAML, encoding="utf-8")
        (directory / "install.lock").write_text(LOCK_TEXT, encoding="utf-8")


def _grant(action_id: str, gate: str, summary: Summary, wait_s: float) -> Decision:
    del action_id, summary, wait_s

    return Decision(Verdict.GRANTED, gate, NOW + 10.0)


def _release(tmp_path: Path, run: FakeRun, servers: tuple[str, ...]) -> dict[str, object]:
    """One `mcp-servers` release. Answers its ledger entry."""
    _registry(tmp_path, servers)
    components = tmp_path / "components"
    components.mkdir(exist_ok=True)
    _teach(run, components)
    stamp_tree(components, COMPONENT, LIVE_VERSION)
    mcp_root = tmp_path / "opt-mcp"
    mcp_root.mkdir()
    wiring = Wiring(
        host=replace(fake_host(tmp_path, run), mcp_root=mcp_root),
        transport=_grant,
        readers=fake_readers(latest={COMPONENT: NEW_VERSION}),
        api=green_api(str(CATALOG_BY_NAME[COMPONENT].repo), TAG, SHA_OF[COMPONENT]),
    )
    spool_root = make_spool_dirs(tmp_path)
    write_request(
        spool_root, REQUEST_ID, request_body({COMPONENT: "latest"}, request_id=REQUEST_ID)
    )
    spool = Spool(str(spool_root), this_uid())
    try:
        handle(spool, f"{REQUEST_ID}.json", wiring, Counter())
    finally:
        spool.close()

    path = spool_root / DONE_DIR / f"{REQUEST_ID}.json"
    loaded: object = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)

    return {str(key): value for key, value in cast("dict[object, object]", loaded).items()}


def _signals(run: FakeRun) -> list[str]:
    return [line for line in run.argv_lines() if " kill " in f" {line} "]


def _rosters(tmp_path: Path) -> list[Path]:
    return sorted(tmp_path.rglob(OLD_ROSTER_NAME))


def _said(entry: dict[str, object]) -> str:
    return json.dumps([entry["log_tail"], entry["manual"]])


@pytest.mark.parametrize("servers", [Declares.ONE, Declares.NONE], ids=["one", "none"])
def test_a_release_sends_no_signal_and_writes_no_roster(
    tmp_path: Path, servers: tuple[str, ...]
) -> None:
    run = FakeRun(answers=git_host_answers())

    entry = _release(tmp_path, run, servers)

    assert entry["status"] == "succeeded", entry
    assert _signals(run) == []
    assert _rosters(tmp_path) == []
    assert "roster" not in _said(entry)
    assert "reload" not in _said(entry)


def test_the_release_still_installs_the_server_tree(tmp_path: Path) -> None:
    run = FakeRun(answers=git_host_answers())

    entry = _release(tmp_path, run, Declares.ONE)

    assert entry["status"] == "succeeded", entry
    assert (tmp_path / "opt-mcp" / SERVER / "bin" / "kagimcp").is_file()


def test_a_restore_sends_no_signal_and_writes_no_roster(tmp_path: Path) -> None:
    run = FakeRun(answers=git_host_answers(), fails={VERIFY_HOOK: 1})

    entry = _release(tmp_path, run, Declares.ONE)

    # The hook fails again after the restore, so the status is `failed`.
    assert entry["status"] == "failed", entry
    assert f"back {SERVER}" in _said(entry)
    assert _signals(run) == []
    assert _rosters(tmp_path) == []
    assert "roster" not in _said(entry)
    assert "reload" not in _said(entry)
