"""The FIRST MCP server release, on a host where none has run yet.

`mcp-servers` moves one component tree AND eleven per-server trees, reads
a second repository's declarations, and writes the file the PEP serves
from.

| What the host had on 2026-09-22 | What this module does |
|---|---|
| `/opt/mcp` `root:root 0755`, EMPTY | the directory, and nothing under it |
| no `/var/lib/agent-mcp` | the root `0755`, empty, as the visit makes it |
| `/srv/.../secrets` EMPTY, nine gaps open | no `<name>.enc`, nine `<name>.json` |
| eleven `mcp/<name>/server.yaml` | **the eleven declarations, comments removed** |
| `mcp-servers-v0.1.0` on agent-mcp `a1b2c3d` | one tag, one SHA, that tag |
| `managerd`'s `mcp-servers=latest` request | the same body, alone in the spool |
| no `/opt/components/mcp-servers` | `releasectl` and `pep` only |

The declarations in `release_mce_registry/` have the fields of a registry
that was in use, with its comments removed and its site's values replaced.
A registry written for a test would hide every rule below: each one is a
field in a file nobody wrote for a test.

**Four rules, each held against the real files.** The first three are
refusals at step 8 when broken, each with nothing swapped.

1. A fence's `arg` may be contract 01b §7.2's composite `owner/repo`, so
   both GitHub servers parse. The console-script pattern holds no `/`.
2. Two files that name one Home Assistant user twice on purpose declare
   the pair (contract 01b §4.3), agreed by both, and an undeclared
   duplicate is still refused (§6 row 8).
3. A `source: agent-mcp` file names no `install.ref`: the component tag
   is every such server's version (§3.3), and agent-mcp's own release
   tags are a namespace the release never deploys.
4. The released row wins a collision, and the base roster holds no row
   at all: the roster root writes is the only source for a declared
   server.

Two more hold in this package: a refusal names its server, and a FIRST
install's restore takes the new trees away.

**What is faked, and where it stands.** The GitHub API (`green_api`), the
agent-mcp source fetch (`GitFake`), `uv` and `curl` (`McpFake`),
`systemctl` (a recorded argv) and the phone (a granting transport). Every
one is an edge the design already puts a fake at. Nothing else is: the
spool, the request parser, the resolver, the registry reader, the
per-server build, the roster writer and the PEP's own roster parser are
the real code.

**What this rehearsal cannot see**, and the real host will:

1. A real `uv sync --frozen` of agent-mcp as `mcp-<name>`. `McpFake`
   writes the console script the build would leave and resolves nothing,
   so a distribution that will not install is invisible here.
2. A real `runuser`. `As.MCP` is recorded, never executed, so a missing
   `mcp-<name>` account fails on the host and not here.
3. A real `SIGHUP` to a running PEP. The signal is a recorded argv, and
   the reload is driven against the PEP's PARSER, not its process.
4. A real phone, a real Node-RED flow and a real tap.
5. The modes a real `UMask=0027` root unit leaves. One test sets the
   umask itself and asserts the directories that decide it.
6. The PEP's mount namespace. That `agent-pep.service`'s `ReadWritePaths`
   binds `/var/lib/agent-mcp` writable for a server is systemd's to prove
   on the host, and a real `install -d` giving the directory
   to `mcp-<name>` is too: `McpFake` makes it and owns it as the test user.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import ssl
import stat
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, cast
from urllib.parse import urlencode, urlsplit

import pytest
import yaml
from agent_pep.family_ids import MCP_TOOL_SEPARATOR
from agent_pep.mcp_client import Launcher, UpstreamError, load_upstreams, resolve_env
from agent_pep.reload_wiring import RosterSource
from agent_pep.run_as import child_env as server_env
from agent_release.catalog import CATALOG_BY_NAME
from agent_release.errors import Refusal
from agent_release.executor.approval import Decision, Summary
from agent_release.executor.drain import Counter, handle
from agent_release.executor.host import As, Command
from agent_release.executor.host import Result as RunResult
from agent_release.executor.install import INSTALL, normalize_dir
from agent_release.executor.mcpbuild import HUP_ARGV, STATE_DIR_MODE
from agent_release.executor.roster import rows as roster_rows
from agent_release.executor.spool import DONE_DIR, REQUESTS_DIR, Spool
from agent_release.executor.steps import MAX_SERVERS, REGISTRY_MCP_DIR, Wiring
from agent_release.intake.gaps import GapDirectory
from agent_release.intake.notify import make_push
from agent_release.intake.run import MAX_PUSHES_PER_PASS, Listener, Watch, one_pass
from agent_release.intake.run import Wiring as IntakeWiring
from agent_release.intake.service import Intake
from agent_release.intake.store import SecretStore
from agent_release.intake.token import Tokens
from agent_release.mcpserver import ServerFile, Source, read_registry
from release_executor_fixtures import (
    FakeRun,
    GitFake,
    fake_host,
    fake_readers,
    git_host_answers,
    grant_transport,
    green_api,
    make_spool_dirs,
    request_body,
    stamp_tree,
    this_uid,
    write_request,
)
from release_fixtures import manifest_text
from release_mcp_fixtures import McpFake, release_tarball, sha256_of

pytestmark = pytest.mark.slow

COMPONENT: Final = "mcp-servers"

#: What `allocate-tags.sh`'s first run in agent-mcp made: one component,
#: one version, one commit.
FIRST_VERSION: Final = "0.1.0"
TAG: Final = f"{COMPONENT}-v{FIRST_VERSION}"

#: agent-mcp `a1b2c3d`, the commit `component.yaml` merged at, padded to
#: the 40 lowercase hex the manifest's pattern wants.
AGENT_MCP_SHA: Final = "a1b2c3d" + "0" * 33

#: The request `managerd` holds, as `mcp_release._file_request` writes one.
MANAGERD_ID: Final = "01K5J8M2Q7V3X9R4T6N0B8C2DJ"

#: The registry's `mcp/` directory, copied out of agent-registry's `main`
#: on 2026-09-22. Eleven declarations, unedited.
REGISTRY_FIXTURE: Final = Path(__file__).resolve().parent / "release_mce_registry"

DECLARED: Final = (
    "board-lead",
    "github-code",
    "github-platform",
    "ha",
    "ha-read",
    "kagi",
    "mail",
    "vikunja-agent-control",
    "vikunja-finance",
    "vikunja-home-assistant",
    "vikunja-networking",
)

#: The nine gaps open under `secret-gaps/` on 2026-09-22, and the reason
#: there were nine where eleven servers name ten secrets:
#: `mcp_wire._secrets_by_server` then dropped a name two servers claim
#: from BOTH of them, so `ha_read_token` got no gap and no phone push.
#: Contract 01b §4.3 declares that pair, `mcp_wire._agreed` now keeps it,
#: and `mcp_release.open_gaps` writes ONE gap for it.
OPEN_GAPS: Final = (
    "github_token_code",
    "github_token_platform",
    "kagi_api_key",
    "smtp_password",
    "vikunja_token_agent_control",
    "vikunja_token_finance",
    "vikunja_token_home_assistant",
    "vikunja_token_networking",
    "vikunja_token_scrum_lead",
)

#: Which server each open gap belongs to, as `managerd._write_gap` wrote
#: it on 2026-09-21. One file per secret, and the server name is what the
#: phone push and the intake page show beside it.
GAP_OWNER: Final = {
    "github_token_code": "github-code",
    "github_token_platform": "github-platform",
    "kagi_api_key": "kagi",
    "smtp_password": "mail",
    "vikunja_token_agent_control": "vikunja-agent-control",
    "vikunja_token_finance": "vikunja-finance",
    "vikunja_token_home_assistant": "vikunja-home-assistant",
    "vikunja_token_networking": "vikunja-networking",
    "vikunja_token_scrum_lead": "board-lead",
}

#: When the reconciler wrote them. It decides nothing (`gaps.py` rule 6).
GAP_WRITTEN_AT: Final = 1_758_400_000.0

#: The name `ha` and `ha-read` both bind. It is rule 2's whole subject.
SHARED_SECRET: Final = "ha_read_token"

#: The six servers whose `install.source` is `agent-mcp`. They declare no
#: version at all (§3.3): `ref: v0.4.6` is agent-mcp's OWN release tag,
#: which this release cannot deploy.
AGENT_MCP_SERVERS: Final = (
    "board-lead",
    "mail",
    "vikunja-agent-control",
    "vikunja-finance",
    "vikunja-home-assistant",
    "vikunja-networking",
)
DECLARED_REF: Final = "v0.4.6"

#: The two servers whose fence is contract 01b §7.2's composite argument.
COMPOSITE_SERVERS: Final = ("github-code", "github-platform")
COMPOSITE_ARG: Final = "owner/repo"

#: One call name, as a family grant carries it and `family_app` splits
#: it. After the release it reaches `/opt/mcp/kagi`.
KAGI_CALL: Final = f"kagi{MCP_TOOL_SEPARATOR}kagi_search_fetch"

#: The checksum both GitHub servers declare: what GitHub published for
#: github-mcp-server 1.12.0's Linux x86_64 asset.
PUBLISHED_SHA256: Final = "f34de295acd8f1012c7f2c0e3b909d87361d0993b9489b57ee92ac72b85d7cca"

#: The real pinned requirement of each `pypi` server and the hashes its
#: committed closure carries for it, read out of the registry's own
#: `install.lock` files. The rest of each lock — about 1100 lines of
#: transitive closure — is elided: `_closure_of` reads the requirement
#: lines and the `--hash=` lines, and 240 KiB of test data would drive
#: the same two loops.
LOCK_PINS: Final = {
    "kagi": (
        "kagimcp==1.0.2",
        (
            "9e6b55d0243f1c5296b9f954e67f1d45cd0e2fdfd3bac194cdd136fbdb930a14",
            "bf52f0a89472966a0ce276caaa59dcda886a7e4bce236bab422c8eef48ad85b1",
        ),
    ),
    "ha": (
        "ha-mcp==8.4.3",
        (
            "8f2e1e593bede6f056079b9f3f67538418e09b7d7808daa93b1b575e60f8549a",
            "aa13fec6eed92521c03a69d3a0eeb7ea3a9a20036921f1772a725a5088984410",
        ),
    ),
    "ha-read": (
        "ha-mcp==8.4.3",
        (
            "8f2e1e593bede6f056079b9f3f67538418e09b7d7808daa93b1b575e60f8549a",
            "aa13fec6eed92521c03a69d3a0eeb7ea3a9a20036921f1772a725a5088984410",
        ),
    ),
}

#: Each server's console script, as its own `run.entrypoint` declares it.
#: `McpFake` writes one script per build and needs to know which.
ENTRYPOINTS: Final = {
    "board-lead": "board-lead-mcp",
    "github-code": "github-mcp-server",
    "github-platform": "github-mcp-server",
    "ha": "ha-mcp",
    "ha-read": "ha-mcp",
    "kagi": "kagimcp",
    "mail": "mail-mcp",
    "vikunja-agent-control": "vikunja-mcp",
    "vikunja-finance": "vikunja-mcp",
    "vikunja-home-assistant": "vikunja-mcp",
    "vikunja-networking": "vikunja-mcp",
}

#: The base roster, the file `agent-pep.service` names as
#: `PEP_UPSTREAMS`. Read from this repository rather than typed, because
#: its overlap with the registry's eleven is what rule 4 checks and a
#: typed list would answer it with whatever the typist remembered.
BASE_ROSTER: Final = Path(__file__).resolve().parents[2] / "pep" / "upstreams.yaml"

#: `component.yaml`'s own build line, as agent-mcp merged it at `a1b2c3d`:
#: one `uv sync --frozen` at the repository root.
BUILD_LINE: Final = 'build:\n  - ["/usr/local/bin/uv", "sync", "--frozen"]\ninstall:'

#: The hook that manifest names, and it runs as root.
VERIFY_HOOK: Final = "mcp-servers-verify"

#: `UMask=0027`, as the release unit sets it. The test that depends on it
#: sets it, because CI is Linux and a developer's shell is `022`.
UNIT_UMASK: Final = 0o027
OPEN_DIR_MODE: Final = 0o755
CLOSED_DIR_MODE: Final = 0o750

#: The intake, bound where a test may bind: loopback on an ephemeral
#: port. Production is the host's LAN address, port 8380, and `owner_uid=0`, which is the
#: same code with two different numbers.
LOOPBACK: Final = "127.0.0.1"
PASTE_S: Final = 10.0
CERT_TIMEOUT_S: Final = 60.0
HTTP_OK: Final = 200

#: The one secret the operator pastes in this rehearsal, and an obviously fake
#: value. Every assertion about it is that it reached exactly one file.
PASTED_SECRET: Final = "kagi_api_key"
PASTED_VALUE: Final = "pasted-by-the-operator-at-step-4"


# -- the bench ----------------------------------------------------------


@dataclass
class Bench:
    """The host before any MCP server has ever been installed on it."""

    tmp_path: Path
    spool_root: Path
    wiring: Wiring
    run: FakeRun
    components: Path
    registry: Path
    mcp_root: Path
    roster: Path
    secrets: Path
    gaps: Path
    state_root: Path

    # -- what the run left behind ---------------------------------------

    def ledger(self) -> dict[str, object]:
        path = self.spool_root / DONE_DIR / f"{MANAGERD_ID}.json"
        loaded: object = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(loaded, dict)

        return {str(key): value for key, value in cast("dict[object, object]", loaded).items()}

    def step(self, name: str) -> dict[str, object]:
        listed = self.ledger()["steps"]
        assert isinstance(listed, list)
        found = [one for one in cast("list[object]", listed) if isinstance(one, dict)]

        return next(cast("dict[str, object]", one) for one in found if one.get("name") == name)

    def log(self) -> list[str]:
        tail = self.ledger()["log_tail"]
        assert isinstance(tail, list)

        return [str(one) for one in cast("list[object]", tail)]

    def manual(self) -> list[str]:
        lines = self.ledger()["manual"]
        assert isinstance(lines, list)

        return [str(one) for one in cast("list[object]", lines)]

    def signalled(self) -> int:
        return sum(1 for one in self.run.seen if tuple(one.argv) == HUP_ARGV)

    def served(self) -> dict[str, object]:
        if not self.roster.is_file():
            return {}

        loaded: object = yaml.safe_load(self.roster.read_text(encoding="utf-8"))

        return cast("dict[str, object]", loaded) if isinstance(loaded, dict) else {}

    def installed(self) -> list[str]:
        """The server trees. `.python`, the shared interpreters root put
        beside them (`mcpbuild.PYTHON_DIR_NAME`), is not a server."""
        return sorted(one.name for one in self.mcp_root.iterdir() if not one.name.startswith("."))

    def open_gaps(self) -> tuple[str, ...]:
        return tuple(sorted(one.stem for one in self.gaps.glob("*.json")))

    # -- what a merged registry pull request would change ----------------

    def rewrite(self, name: str, old: str, new: str) -> None:
        path = self.registry / REGISTRY_MCP_DIR / name / "server.yaml"
        text = path.read_text(encoding="utf-8")

        assert old in text, f"{name}/server.yaml no longer holds {old!r}"

        path.write_text(text.replace(old, new), encoding="utf-8")

    def seal(self, secret: str) -> None:
        """What the intake leaves when the operator pastes one value: a
        `<name>.enc` root wrote, and the gap closed.

        The sealing itself is `test_release_r7g_intake_whole.py`'s, with a
        real `sops`. This module is about what the RELEASE does with a
        name that has a value and a name that has not, and neither branch
        reads a byte of the file.
        """
        (self.secrets / f"{secret}.enc").write_bytes(b"ENC[sealed by the intake]\n")
        (self.gaps / f"{secret}.json").unlink(missing_ok=True)


# -- the host ------------------------------------------------------------


def _registry_tree(corpus: Path) -> Path:
    """The agent-registry checkout root reads: the REAL declarations, a
    committed closure beside each `pypi` one, and the two `github-release`
    pins pointed at this bench's own asset."""
    registry = corpus / "agent-registry"
    shutil.copytree(REGISTRY_FIXTURE / REGISTRY_MCP_DIR, registry / REGISTRY_MCP_DIR)
    for name, (requirement, hashes) in LOCK_PINS.items():
        (registry / REGISTRY_MCP_DIR / name / "install.lock").write_text(
            _lock_text(name, requirement, hashes), encoding="utf-8"
        )

    # The declared `sha256` is the checksum GitHub published for the real
    # 1.12.0 tarball, and this bench serves a tarball of its own. Pointing
    # the pin at these bytes keeps the CHECK real — `_from_release` still
    # hashes what `curl` wrote and compares — against a file this module
    # owns rather than one it would have to download.
    for name in COMPOSITE_SERVERS:
        path = registry / REGISTRY_MCP_DIR / name / "server.yaml"
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace(PUBLISHED_SHA256, ASSET_SHA256), encoding="utf-8")

    return registry


def _lock_text(name: str, requirement: str, hashes: tuple[str, ...]) -> str:
    """One `uv pip compile --generate-hashes` file, in its real shape: two
    comment lines, the requirement at column zero, its hashes indented."""
    lines = " \\\n".join(f"    --hash=sha256:{one}" for one in hashes)
    command = f"#    uv pip compile --generate-hashes -o mcp/{name}/install.lock -\n"

    return (
        f"# This file was autogenerated by uv via the following command:\n{command}"
        f"{requirement} \\\n{lines}\n"
    )


#: The release asset this bench serves, built ONCE. `tarfile`'s gzip
#: writer stamps the current time into the header, so two calls give two
#: hashes — and a pin written from one call and a file served from
#: another matched only while both landed in the same second.
ASSET: Final = release_tarball(entrypoint=ENTRYPOINTS["github-code"])
ASSET_SHA256: Final = sha256_of(ASSET)


def _asset_url() -> str:
    return (
        "https://github.com/github/github-mcp-server/releases/download/"
        "v1.12.0/github-mcp-server_Linux_x86_64.tar.gz"
    )


def _manifest(components: Path) -> str:
    """agent-mcp's own `component.yaml` at `a1b2c3d`, with its install path
    under the bench: `kind: venv`, `unit: null`, one `uv sync --frozen`,
    and a verify hook that runs as root."""
    text = manifest_text(COMPONENT).replace("/opt/components", str(components))
    text = text.replace("runs_as: operator", "runs_as: root")
    text = text.replace("user: operator", "user: root")
    text = text.replace(f"bin/{COMPONENT}-verify", f"bin/{VERIFY_HOOK}")

    return text.replace("install:", BUILD_LINE, 1)


def _teach(run: FakeRun, mcp: McpFake, components: Path, manifest: str) -> None:
    """Every child this release starts, answered.

    Three kinds reach here and they are told apart by what they ARE, never
    by a name in a string: `git` (the clone `install.fetch` makes), the
    COMPONENT's own build (its staged tree is under the install root), and
    every per-server child (`uv`, `curl`, `install`, `chown`).
    """

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

        if _is_component_build(command, components):
            _build_the_component_tree(components)

            return RunResult(0, "", "")

        if command.argv[0].endswith(("/uv", "/curl", "/install", "/chown", "/chmod")):
            mcp.entrypoint = _entrypoint_for(command)

            return mcp(command)

        return None

    run.dynamic = dynamic


def _is_component_build(command: Command, components: Path) -> bool:
    """The component's own `uv sync`: the one child whose staged tree is
    under the INSTALL root rather than under the MCP root."""
    if not command.argv[0].endswith("/uv"):
        return False

    return any(value.startswith(str(components)) for _name, value in command.env)


def _build_the_component_tree(components: Path) -> None:
    """What `uv sync --frozen` leaves at `<install.to>.new`: the console
    script the verify hook names, and a site-packages the self-contained
    walk needs."""
    staged = stamp_tree(components, f"{COMPONENT}.new", FIRST_VERSION)
    hook = staged / "bin" / VERIFY_HOOK
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    hook.chmod(0o755)


def _entrypoint_for(command: Command) -> str:
    """Which console script `McpFake` leaves behind. One fake serves one
    build and this release runs eleven, so the name is read off the staged
    tree's own path — in the argv for a `pypi` build, in the environment
    for an `agent-mcp` one, whose argv is `uv sync --frozen` and holds no
    path at all.
    """
    words = [*command.argv, *(value for _name, value in command.env)]
    for word in words:
        for name in DECLARED:
            if word.endswith(f"/{name}.new") or f"/{name}.new/" in word:
                return ENTRYPOINTS[name]

    return ENTRYPOINTS["kagi"]


@pytest.fixture
def bench(tmp_path: Path) -> Bench:
    """The host of 2026-09-22, with nothing idealised."""
    spool_root = make_spool_dirs(tmp_path)
    run = FakeRun(answers=git_host_answers())
    host = fake_host(tmp_path, run)
    components = tmp_path / "components"
    # The two trees the operator's morning visit leaves. `mcp-servers` is NOT
    # one: `/opt/components/mcp-servers` does not exist before this
    # release, which is why `previous` reads None.
    for name in ("releasectl", "pep"):
        (components / name).mkdir(parents=True, exist_ok=True)

    registry = _registry_tree(tmp_path / "corpus")
    mcp_root = tmp_path / "opt-mcp"
    mcp_root.mkdir()
    # The one directory this bench adds to 2026-09-22's host.
    # The executor that makes a server's state directory reaches the host
    # only through `bin/rework-release-visit.sh`, and that same run makes
    # this root before it restarts the PEP. The host without it is
    # `test_today_s_host_is_refused_until_the_visit_makes_the_root`.
    state_root = tmp_path / "var-lib-agent-mcp"
    state_root.mkdir()
    state_root.chmod(OPEN_DIR_MODE)
    roster = tmp_path / "state" / "upstreams.yaml"
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    # The store refuses a directory that group or other may write, and a
    # bare `mkdir` takes the machine's umask: 0775 under the host's 002, which
    # answered 500 to the paste on the Linux probe. `chmod` is not masked.
    secrets.chmod(CLOSED_DIR_MODE)
    gaps = tmp_path / "secret-gaps"
    gaps.mkdir()
    for secret in OPEN_GAPS:
        # The three keys `gaps.GAP_KEYS` allows, as `managerd._write_gap`
        # writes them. Root reads this directory as hostile input, so a
        # fixture with a key set of its own would test the refusal path
        # rather than the gap path.
        body = {"server": GAP_OWNER[secret], "secret": secret, "at": GAP_WRITTEN_AT}
        (gaps / f"{secret}.json").write_text(json.dumps(body), encoding="utf-8")

    _teach(run, McpFake(asset_bytes=ASSET), components, _manifest(components))

    return Bench(
        tmp_path=tmp_path,
        spool_root=spool_root,
        wiring=Wiring(
            host=replace(
                host,
                install_roots=(components,),
                mcp_root=mcp_root,
                roster_file=roster,
                mcp_state_root=state_root,
            ),
            transport=grant_transport(),
            readers=fake_readers(
                latest={COMPONENT: FIRST_VERSION}, known={COMPONENT: AGENT_MCP_SHA}
            ),
            api=green_api("agent-mcp", TAG, AGENT_MCP_SHA),
        ),
        run=run,
        components=components,
        registry=registry,
        mcp_root=mcp_root,
        roster=roster,
        secrets=secrets,
        gaps=gaps,
        state_root=state_root,
    )


def _release(bench: Bench, request_id: str = MANAGERD_ID) -> str:
    """`managerd`'s own request, alone in the spool, through every step."""
    write_request(
        bench.spool_root,
        request_id,
        request_body({COMPONENT: "latest"}, request_id=request_id, requested_by="managerd"),
    )
    spool = Spool(str(bench.spool_root), this_uid())
    try:
        return handle(spool, f"{request_id}.json", bench.wiring, Counter())
    finally:
        spool.close()


def _read_servers(bench: Bench) -> tuple[ServerFile, ...]:
    """Every declaration root's own reader accepts, in its own order."""
    return read_registry(bench.registry / REGISTRY_MCP_DIR, MAX_SERVERS)


def _server_names(bench: Bench) -> tuple[str, ...]:
    return tuple(one.name for one in _read_servers(bench))


# -- the registry as it is -----------------------------------------------


def test_the_fixture_is_the_registry_as_agent_registry_holds_it(bench: Bench) -> None:
    """Eleven declarations, and the count matters: `MAX_SERVERS` is 20, so
    a twelfth is not what refuses this release."""
    found = tuple(sorted(one.name for one in (bench.registry / REGISTRY_MCP_DIR).iterdir()))

    assert found == DECLARED
    assert len(DECLARED) < MAX_SERVERS


def test_rule_1_the_composite_fence_contract_01b_defines_parses(
    bench: Bench,
) -> None:
    """Rule 1. Without it root refuses the whole registry.

    `ENTRYPOINT_RE`, the console-script pattern, holds no `/`. Contract
    01b §7.2 is a composite argument: `arg: owner/repo` compares two
    arguments joined by
    `/` as one string, because matching `repo` alone lets
    `someone-else/agent-control` pass a fence meant for
    `<owner>/agent-control`. `pep/src/agent_pep/fences.py` implements it
    (`COMPOSITE_SEPARATOR`) and contract 01b §8.2's own example uses it.

    Both files that carry `docs/rework/spec.md` §4.4's github split declare
    it, and an ALLOW refused is worse than a DENY refused: an allow is
    deny-by-default, so `github-platform` refused is the platform token's
    whole fence gone rather than one rule of it.
    """
    found = {one.name: one for one in _read_servers(bench)}

    assert found["github-code"].arg_denies[0].arg == COMPOSITE_ARG
    assert found["github-platform"].arg_allows[0].arg == COMPOSITE_ARG
    # The deny covers every declared tool, expanded by the roster writer
    # because the PEP's reader has no word for `all`.
    assert found["github-code"].arg_denies[0].tools is None


def test_rule_2_the_two_ha_files_declare_their_shared_secret(
    bench: Bench,
) -> None:
    """Rule 2, contract 01b §4.3.

    `ha/server.yaml` and `ha-read/server.yaml` both declare
    `HOMEASSISTANT_TOKEN: secret:ha_read_token` on purpose: they are one
    Home Assistant user in two modes, and contract 01b §5 rule 5 is why
    that is two files. `stage7-releases.md` §6 row 8 alone would refuse
    the pair and therefore the whole registry.

    The pair is accepted because BOTH files say so. Each names the other,
    and root checks the agreement across the registry.
    """
    found = {one.name: one for one in _read_servers(bench)}

    assert found["ha"].secrets == (SHARED_SECRET,)
    assert found["ha-read"].secrets == (SHARED_SECRET,)
    assert found["ha"].shared[0].server == "ha-read"
    assert found["ha-read"].shared[0].server == "ha"


def test_rule_2_an_undeclared_duplicate_is_still_refused(bench: Bench) -> None:
    """§6 row 8 with the declaration taken away again. This is the typo
    case the rule exists for, and the refusal names the file to edit."""
    bench.rewrite("ha", "    server: ha-read", "    server: kagi")

    with pytest.raises(Refusal) as refused:
        read_registry(bench.registry / REGISTRY_MCP_DIR, MAX_SERVERS)

    assert refused.value.subject == "mcp/ha"
    assert "does not declare sharing ha_read_token with ha-read" in refused.value.detail


def test_the_tenth_gap_waits_on_the_manager_s_own_half(bench: Bench) -> None:
    """Why the host had nine gaps where eleven servers name ten secrets.

    On 2026-09-22 `mcp_wire._secrets_by_server` dropped a name two
    servers claim from BOTH of them, so `ha_read_token` reached
    `open_gaps` under neither and no link was pushed. Contract 01b §4.3
    declares the pair, `mcp_wire._agreed` now keeps it, and
    `mcp_release.open_gaps` writes ONE gap when it is handed the name.

    Nothing about the RELEASE waits on it: the release never reads the
    secrets directory.
    """
    declared = {secret for one in _read_servers(bench) for secret in one.secrets}

    assert bench.open_gaps() == OPEN_GAPS
    assert SHARED_SECRET in declared
    assert SHARED_SECRET not in bench.open_gaps()
    assert sorted(declared - {SHARED_SECRET}) == list(OPEN_GAPS)


def test_rule_3_no_agent_mcp_server_names_a_version_of_its_own(
    bench: Bench,
) -> None:
    """Rule 3, contract 01b §3.3.

    agent-mcp carries two tag namespaces: its own release tags, such as
    `v0.4.6`, which `release.yml` moves on every merge, and the COMPONENT
    tag this release deploys, `mcp-servers-v0.1.0`, which the tag
    workflow makes. A per-server ref would compare across them. So the six
    declare no version, and the artifact line names the component tag the
    phone showed.
    """
    found = {one.name: one for one in _read_servers(bench)}
    for name in AGENT_MCP_SERVERS:
        assert found[name].pin.source is Source.AGENT_MCP, name

    _release(bench)

    assert f"staged board-lead: agent-mcp@{TAG}" in bench.log()
    assert DECLARED_REF not in "\n".join(bench.log())


def test_an_agent_mcp_server_that_still_names_a_ref_is_refused(
    bench: Bench,
) -> None:
    """The registry pull request, half applied. One file kept its ref, and
    root refuses the whole set rather than installing ten of eleven."""
    bench.rewrite("board-lead", "  source: agent-mcp", f"  source: agent-mcp\n  ref: {TAG}")

    result = _release(bench)

    assert "succeeded" not in result
    assert bench.ledger()["refused_check"] == "server"
    assert bench.ledger()["reason"] == "install.ref does not belong to agent-mcp"
    assert bench.step("stage")["status"] == "refused"
    # §2.4 row 8: the refusal lands before any swap, so the host is
    # exactly as it was.
    assert bench.installed() == []
    assert bench.served() == {}
    assert not (bench.components / COMPONENT).exists()


def test_a_refusal_names_the_server_it_refused(bench: Bench) -> None:
    """The ledger line the operator reads at 02:00, with eleven servers declared.

    `_step` records `refusal.detail` as the reason and drops
    `refusal.subject`, so `done/<ULID>.json` alone says "pins v0.4.6, this
    release deploys mcp-servers-v0.1.0" and never which of the eleven
    files said it. Every refusal also writes one log line carrying the
    subject, so a line in `log_tail` holds `mcp/board-lead`.
    """
    bench.rewrite("board-lead", "  source: agent-mcp", f"  source: agent-mcp\n  ref: {TAG}")

    _release(bench)

    assert (
        "refused [server] mcp/board-lead: install.ref does not belong to agent-mcp" in bench.log()
    )


# -- the release that lands ----------------------------------------------


def test_the_whole_registry_installs(
    bench: Bench,
) -> None:
    """One `mcp-servers` release, end to end, on the eleven real files
    with both registry pull requests merged.

    Eleven servers install: three `pypi` from their committed closures,
    two `github-release` from one hash-pinned asset, six `agent-mcp` from
    the checkout this release already fetched. The component's own tree
    lands under the install root, the roster names eleven upstreams, and
    the PEP is signalled once.
    """

    result = _release(bench)

    assert "succeeded" in result, bench.ledger()["reason"]
    assert bench.ledger()["status"] == "succeeded"
    assert bench.ledger()["previous"] == {COMPONENT: None}
    assert bench.installed() == list(DECLARED)
    assert (bench.components / COMPONENT / "bin" / VERIFY_HOOK).is_file()
    assert sorted(bench.served()) == list(DECLARED)
    assert bench.signalled() == 1


def test_the_seven_rows_the_operator_reads_before_the_tap(bench: Bench) -> None:
    """§2.5's seven fields, for THIS release, as the phone shows them.

    Two rows read as warnings and neither is one.

    `contracts` is empty because `mcp-servers` neither provides nor
    requires one of contract 06 §3's six (rule C5 says why: an MCP pin
    bump should drag nothing else along). `restarts` says `no unit`
    because an MCP server is not a daemon — the PEP spawns one on demand
    — so step 9 restarts nothing and the reload is the only thing that
    moves.

    `review` is `safe` and not `suspect`, unlike `ui`'s first release:
    root read no manifest at an unverified commit here. The unstamped
    agent-control trees land under `manual` instead, because an
    `mcp-servers` release fetches agent-mcp alone and root then has no
    clone of theirs to read (contract 06 §10.2's fifth row, the other
    branch).
    """
    shown: list[Summary] = []

    def watching(action_id: str, gate: str, summary: Summary, wait_s: float) -> Decision:
        shown.append(summary)

        return grant_transport()(action_id, gate, summary, wait_s)

    bench.wiring = replace(bench.wiring, transport=watching)

    _release(bench)

    assert len(shown) == 1, "one tap, for the whole set"
    assert shown[0].review == "safe: 1 component(s), venv"
    assert shown[0].components == f"{COMPONENT} absent → {FIRST_VERSION}"
    assert shown[0].contracts == "0 contracts satisfied"
    assert shown[0].restarts == f"{COMPONENT}: no unit"
    assert shown[0].restore == "automatic"
    assert shown[0].requested_by == "managerd"
    assert [one for one in bench.manual() if one.startswith("installed and unstamped")] == [
        "installed and unstamped, nothing root can read: pep",
        "installed and unstamped, nothing root can read: releasectl",
    ]


def test_the_ledger_names_the_artifact_and_the_closure_of_each_server(
    bench: Bench,
) -> None:
    """`done/<ULID>.json` names the artifact and the closure. A pin is a
    public fact and no `run.env` value is read at all, so the receipt
    carries names and hashes only (invariant 13)."""

    _release(bench)

    lines = bench.log()
    requirement, hashes = LOCK_PINS["kagi"]

    assert f"staged kagi: {requirement}" in lines
    assert f"  closure kagi: --hash=sha256:{hashes[0]} \\" in lines
    assert f"staged github-code: {_asset_url()}" in lines
    assert f"  closure github-code: sha256:{ASSET_SHA256}" in lines
    assert f"staged board-lead: agent-mcp@{TAG}" in lines
    assert "secret:" not in "\n".join(f"{one}" for one in lines if one.startswith("staged "))


def test_an_open_secret_gap_installs_the_tree_and_rosters_the_name(
    bench: Bench,
) -> None:
    """Whether an open gap holds the install back, answered against the
    code rather than the document.

    Every server still has an OPEN gap: the secrets directory is empty.
    The release installs all eleven anyway and writes a roster row for
    each, carrying `secret:<name>` as a NAME. Nothing in the release reads
    the secrets directory, and `managerd` takes the same reading: filing
    early is the better behaviour, and contract 01b §4.1
    rule 4 turns the missing value into a visible `running: false` rather
    than an invisible nothing.

    §4.3 does not say otherwise. Its five rules are about how a value
    travels, not about when a tree installs.
    """

    _release(bench)

    assert not any(bench.secrets.iterdir()), "no secret was ever sealed"
    assert bench.open_gaps() == OPEN_GAPS
    kagi = cast("dict[str, object]", bench.served()["kagi"])

    assert kagi["env"] == {"KAGI_API_KEY": "secret:kagi_api_key"}
    assert (bench.mcp_root / "kagi" / "bin" / "kagimcp").is_file()


def test_a_pasted_secret_changes_nothing_the_release_does(bench: Bench) -> None:
    """The second half of question 2: one secret pasted, then a release.

    Nothing the release does moves. The roster is what the writer would
    have written with the gap still open, because the writer never opens
    the secrets directory — and a release that behaved differently would
    make a secret an input to an install, which is the coupling §4.3 rule
    4 exists to prevent.

    What the paste DOES change is one function, and it is the PEP's:
    `resolve_env` refuses a spec whose `secret:` name it cannot find and
    answers the value when it can. That is where "the operator taps twice" meets
    "the upstream serves".
    """
    bench.seal("kagi_api_key")

    _release(bench)
    served = bench.served()
    spec = load_upstreams(bench.roster)["kagi"]

    assert "kagi_api_key" not in bench.open_gaps()
    assert (bench.secrets / "kagi_api_key.enc").is_file()
    assert served == yaml.safe_load(
        yaml.safe_dump(
            roster_rows(_read_servers(bench), bench.mcp_root, bench.state_root),
            sort_keys=True,
            default_flow_style=False,
        )
    )
    with pytest.raises(UpstreamError):
        resolve_env(spec, {})

    assert resolve_env(spec, {"kagi_api_key": "a-value"}) == {"KAGI_API_KEY": "a-value"}


def test_the_next_tag_needs_no_registry_edit(bench: Bench) -> None:
    """Why the rule is "no version" and not "the right version".

    Pinning the six at `mcp-servers-v0.1.0` would have worked once.
    `managerd` files `{"mcp-servers": "latest"}` unattended, so the day
    agent-mcp carries `mcp-servers-v0.1.1` the same request would refuse
    again, on the same line, until somebody edited six files in
    agent-registry. A pin that has to be rewritten on every release of
    the thing it pins is not a pin.

    With no version declared, the next tag releases itself.
    """
    next_tag = f"{COMPONENT}-v0.1.1"
    bench.wiring = replace(
        bench.wiring,
        readers=fake_readers(latest={COMPONENT: "0.1.1"}, known={COMPONENT: AGENT_MCP_SHA}),
        api=green_api("agent-mcp", next_tag, AGENT_MCP_SHA),
    )

    result = _release(bench)

    assert "succeeded" in result, bench.ledger()["reason"]
    assert f"staged board-lead: agent-mcp@{next_tag}" in bench.log()
    assert bench.installed() == list(DECLARED)


# -- the secret, pasted through root's own intake ------------------------


class _Phone:
    """The fake phone. It keeps each push whole, which is how a test learns
    a token: no route of the intake ever answers with one."""

    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    def summaries(self) -> list[str]:
        return [str(one["summary"]) for one in self.sent]

    def link_for(self, secret: str) -> str:
        for body in self.sent:
            fields = cast("dict[str, object]", body["fields"])
            if fields["secret"] == secret:
                return str(fields["link"])

        raise AssertionError(f"no push named {secret}")

    def __call__(self, body: dict[str, object]) -> bool:
        self.sent.append(body)

        return True


def _sealed(plaintext: bytes) -> bytes | None:
    """The fake `sops`. A real seal is `test_release_r7g_sealer_sops.py`,
    and the question here is what the RELEASE does with the file, not
    what is inside it."""
    del plaintext

    return b"-----BEGIN AGE ENCRYPTED FILE-----\nsealed\n"


@dataclass
class _Intake:
    """Root's intake, bound on loopback, with its own gap directory."""

    wiring: IntakeWiring
    listener: Listener
    phone: _Phone


def _stand_up_the_intake(bench: Bench) -> _Intake:
    certfile, keyfile = _certificate(bench.tmp_path)
    clock = _frozen_clock()
    phone = _Phone()
    tokens = Tokens(clock)
    directory = GapDirectory(bench.gaps, os.getuid())
    store = SecretStore(bench.secrets, _sealed, owner_uid=os.getuid())
    intake = Intake(tokens=tokens, store=store, close_gap=directory.close_named)
    listener = Listener(intake, certfile, keyfile, LOOPBACK, 0)

    return _Intake(
        wiring=IntakeWiring(
            gaps=directory,
            store=store,
            tokens=tokens,
            push=make_push("https://hook.invalid", "bearer", f"https://{LOOPBACK}:0", phone),
            watch=Watch(clock),
            listener=listener,
            say=lambda line: None,
        ),
        listener=listener,
        phone=phone,
    )


def _frozen_clock() -> Callable[[], float]:
    return lambda: GAP_WRITTEN_AT + 60.0


def _certificate(tmp_path: Path) -> tuple[str, str]:
    """A throwaway self-signed certificate for loopback, made the way the
    host's own is: `openssl`, by hand, once."""
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
        timeout=CERT_TIMEOUT_S,
        check=True,
    )

    return str(cert), str(key)


def _paste(port: int, token: str, secret: str, value: str) -> int:
    """What the operator's browser posts: a form body, over TLS, to root."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    connection = http.client.HTTPSConnection(LOOPBACK, port, context=context, timeout=PASTE_S)
    try:
        connection.request(
            "POST",
            "/secret",
            body=urlencode({"token": token, "name": secret, "value": value}),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        return connection.getresponse().status
    finally:
        connection.close()


def test_nine_gaps_take_two_intake_passes(bench: Bench) -> None:
    """§4.3 step 2, against the nine gaps the host really has.

    `run.MAX_PUSHES_PER_PASS` is 8, so the first pass mints eight and says
    one is waiting. The ninth is not held for an hour — the cooldown paces
    a gap that was already pushed, and this one was not — so the very next
    pass sends it. The operator reads nine links, in two bursts.
    """
    intake = _stand_up_the_intake(bench)
    try:
        first = one_pass(intake.wiring)
        second = one_pass(intake.wiring)
    finally:
        intake.listener.close()

    assert first == (MAX_PUSHES_PER_PASS, 0)
    assert second == (len(OPEN_GAPS) - MAX_PUSHES_PER_PASS, 0)
    assert sorted(intake.phone.summaries()) == sorted(
        f"{GAP_OWNER[secret]} needs the secret {secret}" for secret in OPEN_GAPS
    )


def test_one_secret_pasted_through_the_intake_then_the_release(
    bench: Bench,
) -> None:
    """§4.1 steps 4 and 5, in order, with root's own intake in between.

    The operator opens one link, pastes one value, and root seals it. Then the
    release runs — and this is the assertion the whole of §4.3 exists for,
    now over an ELEVEN-server release: the value is in no roster row, no
    ledger entry, no log line and no argv word root built. The roster
    carries `secret:kagi_api_key`, which is a name.

    What the release does NOT do is pick the value up. It never reads the
    secrets directory. The PEP does, at its next reload, which is why the
    two taps are independent and why one can be answered days after the
    other.
    """
    intake = _stand_up_the_intake(bench)
    try:
        one_pass(intake.wiring)
        token = urlsplit(intake.phone.link_for(PASTED_SECRET)).path.strip("/").split("/")[-1]

        assert _paste(intake.listener.port, token, PASTED_SECRET, PASTED_VALUE) == HTTP_OK
    finally:
        intake.listener.close()

    assert (bench.secrets / f"{PASTED_SECRET}.enc").is_file()
    assert PASTED_SECRET not in bench.open_gaps()

    result = _release(bench)

    assert "succeeded" in result, bench.ledger()["reason"]
    kagi = cast("dict[str, object]", bench.served()["kagi"])

    assert kagi["env"] == {"KAGI_API_KEY": f"secret:{PASTED_SECRET}"}
    assert PASTED_VALUE not in bench.roster.read_text(encoding="utf-8")
    assert PASTED_VALUE not in json.dumps(bench.ledger())
    assert not [one for one in bench.run.argv_lines() if PASTED_VALUE in one]


# -- the roster, read by the PEP's own parser ----------------------------


def test_the_roster_root_writes_is_what_the_peps_parser_reads(bench: Bench) -> None:
    """The two shapes, against each other. `load_upstreams` is the PEP's
    own reader, imported here because reading `pep/` is allowed and
    changing it is not this package's.

    A roster the PEP refuses does not install nothing — it leaves the OLD
    pool serving with one line in a log, so a disagreement would be
    invisible until somebody wondered why the new upstreams never came.
    """

    _release(bench)
    specs = load_upstreams(bench.roster)

    assert sorted(specs) == list(DECLARED)
    assert specs["kagi"].command == str(bench.mcp_root / "kagi" / "bin" / "kagimcp")
    assert specs["github-code"].args == (
        "stdio",
        "--toolsets=context,repos,pull_requests,issues",
        "--lockdown-mode",
    )
    # `tools: all` is expanded by the writer, because the PEP's reader
    # takes a list and has no word for `all`.
    assert specs["github-code"].arg_denies[0].tools == frozenset(specs["github-code"].tools)
    assert specs["board-lead"].env["VIKUNJA_TOKEN"] == "secret:vikunja_token_scrum_lead"


def test_the_declared_allow_fence_is_reported_and_not_written(bench: Bench) -> None:
    """`github-platform` declares `arg_allows`, `UpstreamSpec` has no
    field for it, and a roster carrying one would be refused whole. Root
    says so under `manual` rather than dropping it in silence."""

    _release(bench)

    assert "arg_allows is declared and not applied for: github-platform" in bench.manual()
    assert "arg_allows" not in bench.roster.read_text(encoding="utf-8")


def test_rule_4_every_released_server_serves_from_the_tree_it_installed(
    bench: Bench,
) -> None:
    """Rule 4, against the REAL base roster of this repository.

    A generated roster adds to the pool rather than REPLACING it, and a
    released row wins a name collision. The base file holds no row
    (`stage7-releases.md` §4.4). Two facts together are the rule:

    1. Every declared name serves root's own command.
    2. The base names none of them, so no declared name serves from a
       tree no release installed.
    """
    _release(bench)
    base = _base_names()

    specs = RosterSource(upstreams_file=BASE_ROSTER, generated_file=bench.roster).upstreams()

    assert not base & set(DECLARED), "the base roster declares a server the registry declares"
    assert set(specs) == set(DECLARED), "a name serves that no release installed"
    for name in DECLARED:
        assert specs[name].command == str(bench.mcp_root / name / "bin" / ENTRYPOINTS[name])


def test_rule_4_the_call_name_a_grant_carries_reaches_the_released_tree(
    bench: Bench,
) -> None:
    """The proof in the words a family grant uses. `kagi__kagi_search_fetch`
    is what a sandbox asks for and what `family_app` splits on `__`, and
    after this release its server half resolves to `/opt/mcp/kagi`."""
    _release(bench)

    specs = RosterSource(upstreams_file=BASE_ROSTER, generated_file=bench.roster).upstreams()
    server, _, tool = KAGI_CALL.partition(MCP_TOOL_SEPARATOR)

    assert tool in specs[server].tools
    assert specs[server].command == str(bench.mcp_root / "kagi" / "bin" / "kagimcp")
    assert specs[server].command != "/opt/agent-pep-mcp/mcp/bin/kagimcp"


def test_rule_4_no_command_the_pep_starts_is_outside_the_mcp_root(
    bench: Bench,
) -> None:
    """The base roster holds no row, so after the release every command
    the PEP would start is inside the tree root installed."""
    _release(bench)

    specs = RosterSource(upstreams_file=BASE_ROSTER, generated_file=bench.roster).upstreams()

    assert _base_names() == set()
    for name, spec in specs.items():
        assert spec.command.startswith(f"{bench.mcp_root}/"), name


def test_a_generated_name_the_base_does_not_hold_is_the_one_that_serves(
    bench: Bench,
) -> None:
    """The other side of rule 4: a name the base roster does NOT hold
    comes through, with root's own command.

    `weather` is not in `pep/upstreams.yaml`, so a twelfth declaration
    serves on its first release.
    """
    _release(bench)
    written = yaml.safe_load(bench.roster.read_text(encoding="utf-8"))
    written["weather"] = written.pop("kagi")
    bench.roster.write_text(yaml.safe_dump(written), encoding="utf-8")

    specs = RosterSource(upstreams_file=BASE_ROSTER, generated_file=bench.roster).upstreams()

    assert "weather" in specs
    assert specs["weather"].command == str(bench.mcp_root / "kagi" / "bin" / "kagimcp")


def _base_names() -> set[str]:
    loaded: object = yaml.safe_load(BASE_ROSTER.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)

    return {str(one) for one in cast("dict[object, object]", loaded)}


def test_root_builds_every_roster_command_from_the_validated_name(
    bench: Bench,
) -> None:
    """§6 row 9. Not one `command` in the roster comes out of a file: each
    is `<mcp root>/<name>/bin/<entrypoint>`, joined by root from two
    values that each matched a pattern with no slash in it."""

    _release(bench)
    served = bench.served()

    assert _server_names(bench) == DECLARED
    for name in DECLARED:
        row = cast("dict[str, object]", served[name])

        assert row["command"] == str(bench.mcp_root / name / "bin" / ENTRYPOINTS[name])


# -- the state directories -----------------------------------------------

#: The two real files that declare `run.state_dir`, and the variable both
#: name. ha-mcp 8.4.3 reads it first of four places
#: (`ha_mcp/utils/data_paths.py`, `_resolve_data_dir`), and falls back to
#: `/tmp/ha-mcp` when it is unset or unwritable.
STATEFUL: Final = ("ha", "ha-read")
HA_STATE_ENV: Final = "HA_MCP_CONFIG_DIR"

#: A second request, for the release after the first.
SECOND_ID: Final = "01K5J8M2Q7V3X9R4T6N0B8C2DK"
NEXT_VERSION: Final = "0.1.1"

#: What ha-mcp writes into its directory: its tool-visibility state.
KEPT_NAME: Final = "tool_config.json"
KEPT_BODY: Final = '{"kept": true}\n'


def _state_dirs(bench: Bench) -> list[str]:
    return sorted(one.name for one in bench.state_root.iterdir())


def test_ha_and_ha_read_each_get_a_directory_of_their_own(bench: Bench) -> None:
    """Contract 01b §4.2's last sentence, on the real files: two blocks of
    one binary never share a directory. Without one, both reach for
    `/tmp/ha-mcp` under the PEP's `PrivateTmp`.

    Made as root at step 8 for that server's own user, before its tree
    goes live, and for no other of the eleven."""
    _release(bench)

    assert _state_dirs(bench) == list(STATEFUL)
    lines = bench.log()
    for name in STATEFUL:
        path = bench.state_root / name
        user = f"mcp-{name}"
        made = [one for one in bench.run.seen if one.argv[-1] == str(path)]

        assert [(one.identity, one.argv) for one in made] == [
            (As.ROOT, (INSTALL, "-d", "-m", STATE_DIR_MODE, "-o", user, "-g", user, str(path)))
        ]
        said = lines.index(f"  state {name}: {path}")
        live = next(n for n, one in enumerate(lines) if one.startswith(f"live {name}: "))
        assert said < live


def test_only_the_two_rows_carry_a_state_directory(bench: Bench) -> None:
    """The roster is how the PEP learns the path. `ha` and `ha-read` carry
    their declared environment plus one variable naming two different
    directories; the other nine carry exactly what they declared."""
    _release(bench)
    declared = {one.name: dict(one.env) for one in _read_servers(bench)}
    served = bench.served()

    for name in DECLARED:
        env = cast("dict[str, object]", served[name])["env"]
        if name not in STATEFUL:
            assert env == declared[name], name
            continue

        assert env == {**declared[name], HA_STATE_ENV: str(bench.state_root / name)}


def test_the_pep_hands_the_path_to_the_server_as_it_is(bench: Bench) -> None:
    """A child's environment is exactly its row's `env`, so the PEP
    needs no change. The path is a literal, `resolve_env` passes a
    literal through, and what the launcher decodes after the switch is the
    environment the server execs with."""
    _release(bench)
    spec = load_upstreams(bench.roster)["ha-read"]

    params = Launcher(("agent-pep-as",)).params(spec, resolve_env(spec, {SHARED_SECRET: "x"}))
    execs_with = server_env(cast("dict[str, str]", params.env))

    assert execs_with[HA_STATE_ENV] == str(bench.state_root / "ha-read")


def test_the_next_release_keeps_what_the_server_wrote(bench: Bench) -> None:
    """State is the point, so a release never empties or remakes the
    directory. ha-mcp's `tool_config.json` survives the next tag."""
    _release(bench)
    kept = bench.state_root / "ha-read" / KEPT_NAME
    kept.write_text(KEPT_BODY, encoding="utf-8")
    bench.wiring = replace(
        bench.wiring,
        readers=fake_readers(latest={COMPONENT: NEXT_VERSION}, known={COMPONENT: AGENT_MCP_SHA}),
        api=green_api("agent-mcp", f"{COMPONENT}-v{NEXT_VERSION}", AGENT_MCP_SHA),
    )

    result = _release(bench, SECOND_ID)

    assert "succeeded" in result, result
    assert kept.read_text(encoding="utf-8") == KEPT_BODY
    assert _state_dirs(bench) == list(STATEFUL)


def test_a_failed_verify_takes_the_trees_and_leaves_the_state(bench: Bench) -> None:
    """The restore of a first install removes every tree. The
    two directories stay: removing state is a bigger verb than a release,
    and an empty one costs nothing the next release does not reuse."""
    bench.run.fails[VERIFY_HOOK] = 1

    _release(bench)

    assert bench.installed() == []
    assert _state_dirs(bench) == list(STATEFUL)


def test_today_s_host_is_refused_until_the_visit_makes_the_root(bench: Bench) -> None:
    """The host on 2026-09-22 had no `/var/lib/agent-mcp`, and two of the
    eleven files declare `run.state_dir`. The release refuses at step 8
    with nothing staged and names the command that makes the root. It does
    not make the root itself: the PEP's `ReadWritePaths` binds only a path
    that exists as the PEP starts, so a root made now would be read-only
    to both servers until a restart, behind a ledger saying `succeeded`."""
    bench.state_root.rmdir()

    result = _release(bench)

    assert "succeeded" not in result
    assert bench.ledger()["refused_check"] == "server"
    assert "bin/rework-release-visit.sh" in str(bench.ledger()["reason"])
    assert bench.step("stage")["status"] == "refused"
    assert bench.installed() == []
    assert bench.served() == {}
    assert not bench.state_root.exists()


# -- the drain, the restore and the modes --------------------------------


def test_the_managerd_request_is_the_only_one_in_the_spool(bench: Bench) -> None:
    """§4.1 step 3's own request and nothing else. One drain, one entry,
    and `requests/` empty afterwards — the path unit is a
    `PathExistsGlob` and re-fires while any match remains."""

    _release(bench)

    assert bench.ledger()["requested_by"] == "managerd"
    assert sorted((bench.spool_root / REQUESTS_DIR).glob("*.json")) == []
    assert sorted((bench.spool_root / DONE_DIR).glob("*.json")) == [
        bench.spool_root / DONE_DIR / f"{MANAGERD_ID}.json"
    ]


def test_a_failed_verify_puts_every_server_tree_and_the_roster_back(
    bench: Bench,
) -> None:
    """Contract 06 §5.1 over a set that is one component and eleven server
    trees. The component's verify fails, step 10 walks `deployed`, and the
    server trees and the roster go back with it.

    Two signals, not one: the switch sent the first, and the restore sends
    the second so the PEP reads the roster root put back.
    """
    bench.run.fails[VERIFY_HOOK] = 1

    result = _release(bench)

    assert "succeeded" not in result
    assert bench.ledger()["status"] in ("failed", "restored")
    assert bench.served() == {}
    assert bench.installed() == [], "a server tree was left live"
    assert bench.signalled() == 2


def test_the_mcp_root_and_the_work_root_stay_traversable_under_the_umask(
    bench: Bench,
) -> None:
    """The umask `mcpbuild` and `install.fetch` open against, SET here.

    The release unit runs `UMask=0027`, `Path.mkdir` takes it, and an
    `mcp-<name>` is in no group that reaches a `0750` directory. Three
    directories decide whether a build child can run at all: the MCP root
    every server traverses, each staged tree, and `<work root>/<request
    id>/`, which holds both the closure a `pypi` child reads and the
    checkout an `agent-mcp` child runs in.

    `stage7-releases.md` §4.2 still says the third one is not opened. It
    is: `install.fetch` calls `normalize_dir(into.parent)`.
    """
    with _umask(UNIT_UMASK):
        result = _release(bench)

    assert "succeeded" in result, bench.ledger()["reason"]
    assert _mode(bench.mcp_root) == OPEN_DIR_MODE
    assert _mode(bench.mcp_root / "kagi") == OPEN_DIR_MODE
    assert _mode(bench.wiring.host.work_root / MANAGERD_ID) == OPEN_DIR_MODE


def test_normalize_dir_is_the_pass_that_opens_one_directory(tmp_path: Path) -> None:
    """The rule the test above proves, in one call, so a reader can see
    that a mode is a pass over a directory and not a `chmod` child."""
    with _umask(UNIT_UMASK):
        made = tmp_path / "closed"
        made.mkdir()

        assert _mode(made) == CLOSED_DIR_MODE

        normalize_dir(made)

    assert _mode(made) == OPEN_DIR_MODE


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class _umask:
    """`os.umask` for the length of a block, put back whatever happens.

    A test that depends on the umask sets it, because CI is Linux and a
    developer's shell is `022`. A test that leaked one would change every
    later test in the same process.
    """

    def __init__(self, value: int) -> None:
        self.value = value
        self.previous = 0

    def __enter__(self) -> None:
        self.previous = os.umask(self.value)

    def __exit__(self, *_exception: object) -> None:
        os.umask(self.previous)
