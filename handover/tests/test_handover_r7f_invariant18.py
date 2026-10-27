"""Invariant 18, root's half: one file becomes an installed server tree.

`stage7-releases.md` §4.1's steps up to the release, as one test, with a
fake only where the design puts a human or root:

| Step | Here |
|---|---|
| 1 the operator writes `mcp/<name>/server.yaml` | the test writes the file |
| 2 CI validates it | `mcpserver`, which root re-runs anyway |
| 3 the reconciler files a request | `caregiver.mcp_release`, real |
| 5 the operator taps the release | a fake phone that grants |

Two claims: the release installs a tree at `/opt/mcp/<name>` from the
closure the review approved, and the release signals the PEP to reload.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Final, cast

import pytest
from caregiver.mcp_release import REQUEST_COMPONENT, McpPaths, request_servers
from handover.catalog import CATALOG_BY_NAME
from handover.executor.approval import Decision, Summary, Verdict
from handover.executor.drain import Counter, handle
from handover.executor.host import Command, Host
from handover.executor.host import Result as RunResult
from handover.executor.mcpbuild import HUP_ARGV
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
    stamp_tree,
    this_uid,
)
from handover_fixtures import manifest_text
from handover_mcp_fixtures import LOCK_TEXT, McpFake

pytestmark = pytest.mark.slow

COMPONENT: Final = "mcp-servers"
LIVE_VERSION: Final = "0.9.3"
NEW_VERSION: Final = "0.9.4"
TAG: Final = f"{COMPONENT}-v{NEW_VERSION}"
SERVER: Final = "kagi"
TOOL: Final = "kagi_search_fetch"
SECRET: Final = "kagi_api_key"

NOW: Final = 1_758_153_600.0

#: The one file the operator writes (§4.1 step 1). Contract 01b §8.1, shortened to
#: one tool.
SERVER_YAML: Final = f"""
name: {SERVER}
identity: "The fleet's Kagi API key. Search and page extraction, no account writes."
install:
  source: pypi
  package: kagimcp
  version: 1.0.2
  lock: mcp/kagi/install.lock
  python: "3.12"
run:
  entrypoint: kagimcp
  env:
    KAGI_API_KEY: secret:{SECRET}
tools:
  - name: {TOOL}
    description: Search the web and return ranked results with snippets.
"""

#: `mcp-servers` as contract 06 §1 row 6 has it: one venv, no unit, so
#: nothing restarts for it and the reload is the only thing that moves.
BUILD_LINE: Final = 'build:\n  - ["/usr/local/bin/uv", "sync", "--frozen"]\ninstall:'


def _manifest(components: Path) -> str:
    text = manifest_text(COMPONENT)
    text = text.replace("/opt/components", str(components))
    text = text.replace("install:", BUILD_LINE, 1)

    return text


def _stage_the_component(run: FakeRun, components: Path, manifest: str) -> None:
    """What the fake clone leaves, and what the component's own build does.

    The per-server build is NOT faked away: `McpFake` does what `uv` and
    `curl` do, so the tree under the MCP root is built by the real
    `McpBuilder` and the real `mcpserver` reader.
    """
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


def _is_mcp_command(command: Command) -> bool:
    """Every child the per-server build starts, and nothing else."""
    first = command.argv[0]

    return first.endswith(("/uv", "/curl", "/install", "/chown", "/chmod"))


def _registry(tmp_path: Path) -> Path:
    """The registry checkout root reads: the operator's declaration, and the
    closure committed beside it in the same pull request (contract 01b
    §3.4). Root installs FROM that lock and resolves nothing."""
    # The CORPUS clone, not the platform root: every source root reads
    # sits in `/srv/agents/code/<repo>`, which no sandbox can write, and
    # `fake_host` makes the three checkouts there.
    directory = tmp_path / "corpus" / "agent-registry" / "mcp" / SERVER
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "server.yaml").write_text(SERVER_YAML, encoding="utf-8")
    (directory / "install.lock").write_text(LOCK_TEXT, encoding="utf-8")

    return directory


def _wiring(tmp_path: Path, run: FakeRun) -> Wiring:
    def transport(action_id: str, gate: str, summary: Summary, wait_s: float) -> Decision:
        """§4.1 step 5: one tap. The human, and the only one here."""
        del action_id, summary, wait_s

        return Decision(Verdict.GRANTED, gate, NOW + 10.0)

    return Wiring(
        host=fake_host(tmp_path, run),
        transport=transport,
        readers=fake_readers(latest={COMPONENT: NEW_VERSION}),
        api=green_api(str(CATALOG_BY_NAME[COMPONENT].repo), TAG, SHA_OF[COMPONENT]),
    )


def test_one_server_file_becomes_an_installed_tree_and_a_reload(tmp_path: Path) -> None:
    # -- step 1: the operator writes one file, and nothing else -----------
    _registry(tmp_path)

    # -- step 3: the reconciler files one request, and installs nothing ---
    spool_root = make_spool_dirs(tmp_path)
    paths = McpPaths(
        requests=spool_root / "requests",
        done=spool_root / DONE_DIR,
        secrets=_made(tmp_path / "secrets"),
        gaps=_made(tmp_path / "secrets" / "gaps"),
        installed_root=_made(tmp_path / "opt-mcp"),
        marker=tmp_path / "mcp-request.json",
    )
    filed = request_servers(paths, (SERVER,), NOW).servers

    assert filed == (SERVER,)
    request = _one_request(paths.requests)
    assert request["requested_by"] == "managerd"
    assert request["components"] == {REQUEST_COMPONENT: "latest"}
    assert not any(paths.installed_root.iterdir())

    # -- step 5: one tap, and root does the rest --------------------------
    components = _made(tmp_path / "components")
    run = FakeRun(answers=git_host_answers())
    _stage_the_component(run, components, _manifest(components))
    wiring = _wiring(tmp_path, run)
    wiring = Wiring(
        host=_with_mcp_root(wiring, paths.installed_root),
        transport=wiring.transport,
        readers=wiring.readers,
        api=wiring.api,
    )
    stamp_tree(components, COMPONENT, LIVE_VERSION)
    spool = Spool(str(spool_root), this_uid())
    try:
        outcome = handle(spool, f"{request['id']}.json", wiring, Counter())
    finally:
        spool.close()

    assert outcome.endswith("succeeded"), outcome
    # The tree the design promises, built from the pin and owned by root.
    assert (paths.installed_root / SERVER / "bin" / "kagimcp").is_file()
    # §4.4 step 1, as the last thing the switch did.
    assert any(tuple(one.argv) == HUP_ARGV for one in run.seen)
    entry = _ledger(spool_root, str(request["id"]))
    assert any("staged kagi: kagimcp==1.0.2" in line for line in _log(entry))


def _with_mcp_root(wiring: Wiring, mcp_root: Path) -> Host:
    """The MCP root is the one root builds server trees under. It is NOT an
    install root: no `component.yaml` may name a path inside it."""
    return replace(wiring.host, mcp_root=mcp_root)


def _made(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)

    return path


def _one_request(requests: Path) -> dict[str, object]:
    found = sorted(requests.iterdir())
    assert len(found) == 1
    loaded: object = json.loads(found[0].read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)

    return {str(key): value for key, value in cast("dict[object, object]", loaded).items()}


def _ledger(spool_root: Path, request_id: str) -> dict[str, object]:
    path = spool_root / DONE_DIR / f"{request_id}.json"
    loaded: object = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)

    return {str(key): value for key, value in cast("dict[object, object]", loaded).items()}


def _log(entry: dict[str, object]) -> list[str]:
    return [str(one) for one in cast("list[object]", entry["log_tail"])]
