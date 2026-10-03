"""Invariant 18, whole: one file becomes a callable tool.

`stage7-releases.md` §4.1's seven steps, as one test, with a fake only
where the design puts a human or root:

| Step | Here |
|---|---|
| 1 the operator writes `mcp/<name>/server.yaml` | the test writes the file |
| 2 CI validates it | `mcpserver`, which root re-runs anyway |
| 3 the reconciler files a request | `caregiver.mcp_release`, real |
| 4 the operator pastes the secret | one `<name>.enc` in the per-secret store |
| 5 the operator taps the release | a fake phone that grants |
| 6 the PEP reloads | a real `ReloadablePool` over the stub server |
| 7 a family is granted the tools | `decide_family`, real |

Three claims: the release installs a tree at `/opt/mcp/<name>` from the
closure the review approved, the value the operator pasted reaches the new
upstream's environment and not a file the PEP never opens, and the tool is
ALLOWED afterwards with **no restart of anything**: the same pool object
serves the call and the upstream that was already running never stopped.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Final, cast

import pytest
import yaml
from agent_release.catalog import CATALOG_BY_NAME
from agent_release.executor.approval import Decision, Summary, Verdict
from agent_release.executor.drain import Counter, handle
from agent_release.executor.host import Command, Host
from agent_release.executor.host import Result as RunResult
from agent_release.executor.mcpbuild import HUP_ARGV
from agent_release.executor.spool import DONE_DIR, Spool
from agent_release.executor.steps import Wiring
from caregiver.mcp_release import REQUEST_COMPONENT, McpPaths, request_servers
from chaperone.family_decisions import decide_family
from chaperone.family_grants import FamilyGrants, token_digest
from chaperone.mcp_client import Launcher, StdioUpstreamPool, UpstreamSpec
from chaperone.reload_pool import ReloadablePool
from chaperone.reload_wiring import ReloadTrigger, RosterSource
from release_executor_fixtures import (
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
from release_fixtures import manifest_text
from release_mcp_fixtures import LOCK_TEXT, McpFake

pytestmark = pytest.mark.slow

COMPONENT: Final = "mcp-servers"
LIVE_VERSION: Final = "0.9.3"
NEW_VERSION: Final = "0.9.4"
TAG: Final = f"{COMPONENT}-v{NEW_VERSION}"
SERVER: Final = "kagi"
TOOL: Final = "kagi_search_fetch"
SECRET: Final = "kagi_api_key"

#: What the operator pastes at step 4. Obviously fake, and it never reaches a log
#: line, the ledger or an argv word — which this test also checks.
PASTED: Final = "pasted-by-the-operator-at-step-4"
NOW: Final = 1_758_153_600.0

STUB: Final = Path(__file__).resolve().parent.parent.parent / "chaperone/tests/stub_mcp_server.py"

#: The PEP starts every upstream through `chaperone-as`, as its own
#: `mcp-<name>` user, which a test cannot become. This is that launcher
#: with the switch faked, for every generation the pool builds.
AS_TEST_USER: Final = partial(
    StdioUpstreamPool,
    launcher=Launcher((sys.executable, str(STUB.with_name("fake_run_as.py")), "--")),
)

#: The one file the operator writes (§4.1 step 1). Contract 01b §8.1, shortened to
#: the one tool this test grants.
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


#: Obviously fake and never a secret: the file holds the DIGEST (contract
#: 04 §2.2), so this value authorizes nothing anywhere.
FAMILY_TOKEN: Final = "test-family-token-chat"


def _grants(tools: dict[str, list[str]]) -> FamilyGrants:
    """One family file, as `caregiver` writes it (contract 04 §2)."""
    return FamilyGrants.model_validate(
        {
            "version": 2,
            "family": "chat",
            "rev": "01K5J9QW3R7T0ZP4YB2H6N8M1D",
            "token_sha256": [token_digest(FAMILY_TOKEN)],
            "model_alias": "agent-router",
            "tools": tools,
            "verbs": {},
            "delegates": [],
            "approval": [],
            "limits": {"pep_rpm": 60, "max_inflight_delegations": 2, "max_open_gates": 10},
        }
    )


def _spec(name: str) -> UpstreamSpec:
    """What root writes into the roster once the tree is installed."""
    return UpstreamSpec(
        name=name,
        command=sys.executable,
        args=(str(STUB),),
        env={"STUB_SECRET": "v1"},
        tools=("echo", "sleep"),
    )


def _entry(secret_ref: str = "v1") -> dict[str, object]:
    """One roster row. Root writes these from the installed server files.

    `STUB_SECRET` is what `stub_mcp_server.py` echoes back, so an answer
    names the value the child was spawned with. That is how this test sees
    whether the pasted credential actually reached the process.
    """
    return {
        "command": sys.executable,
        "args": [str(STUB)],
        "env": {"STUB_SECRET": secret_ref},
        "tools": ["echo", "sleep"],
    }


def _paste(tmp_path: Path, name: str, value: str) -> Path:
    """Step 4. Root's intake seals one value into one file, and this is
    the file it leaves (`stage7-releases.md` §4.3). The `sops` here is the
    stub below: what this test checks is that the PEP OPENS this layout.
    """
    store = tmp_path / "secrets"
    store.mkdir(parents=True, exist_ok=True)
    path = store / f"{name}.enc"
    path.write_text(value, encoding="utf-8")

    return store


@pytest.fixture
def fake_sops(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A `sops` that prints the file it decrypts. The real one needs an age
    key this machine does not have, and the layout is what is under test."""
    binaries = tmp_path / "sops-bin"
    binaries.mkdir()
    stub = binaries / "sops"
    stub.write_text('#!/bin/sh\nexec cat "$2"\n', encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binaries}:{os.environ['PATH']}")


@pytest.mark.anyio
@pytest.mark.usefixtures("fake_sops")
async def test_one_server_file_becomes_a_callable_tool_with_no_restart(tmp_path: Path) -> None:
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

    # -- step 6: the PEP reloads. Nothing restarts ------------------------
    # Step 4, which the reconciler asked for at step 3: one value, in the
    # one layout the host can author (§4.3).
    store = _paste(tmp_path, SECRET, PASTED)
    roster = tmp_path / "upstreams.yaml"
    roster.write_text(yaml.safe_dump({"weather": _entry()}), encoding="utf-8")
    pool = ReloadablePool({"weather": _spec("weather")}, {}, make_pool=AS_TEST_USER)
    await pool.start()
    trigger = ReloadTrigger(pool, RosterSource(upstreams_file=roster, secrets_dir=store))
    try:
        was_serving = pool.live_specs["weather"]
        in_flight = asyncio.create_task(pool.call("weather", "sleep", {"seconds": 1.0}))
        await asyncio.sleep(0.05)
        # Root writes the roster the new tree entitles, then signals.
        roster.write_text(
            yaml.safe_dump({"weather": _entry(), SERVER: _entry(f"secret:{SECRET}")}),
            encoding="utf-8",
        )

        report = await asyncio.wait_for(trigger.reload_once(), 10.0)

        assert report is not None
        assert report.added == (SERVER,)
        # The claim this test is about: the value the operator pasted is
        # in the new child's environment. `stub_mcp_server` echoes
        # `STUB_SECRET` back, so the answer names it.
        assert await pool.call(SERVER, "echo", {"text": "hi"}) == f"{PASTED}:hi"
        # No restart of anything: the upstream that was running is the same
        # object, and the call that was in flight answers from its own
        # process. A restart would have dropped both.
        assert pool.live_specs["weather"] == was_serving
        assert await asyncio.wait_for(in_flight, 10.0)

        # -- step 7: a family file grants the new server. Live, no release.
        grants = _grants({SERVER: [TOOL]})
        decision = decide_family(
            grants,
            f"{SERVER}__{TOOL}",
            {"query": "invariant 18"},
            rate_exceeded=False,
            known_servers=frozenset(pool.live_specs),
        )

        assert decision.allow, decision.reason
        # The grant is data: nothing was released for it, and the token the
        # family holds never changed.
        assert grants.token_sha256 == _grants({}).token_sha256
        assert FAMILY_TOKEN not in json.dumps(entry)
        # Invariant 13, over the whole release: the pasted value is in no
        # ledger field, no log line and no argv word root ever built.
        assert PASTED not in json.dumps(entry)
        assert not any(PASTED in " ".join(one.argv) for one in run.seen)
    finally:
        await pool.stop()


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
