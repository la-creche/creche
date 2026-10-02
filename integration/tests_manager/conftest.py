"""Packet C2's shared fixtures: one applied `chat` family, real on both sides.

`managerd` writes four things that three other programs read:

    managerd.apply_once
        |-- grants/<family>.json + creds.json ----> the PEP family path
        |-- config/ (runtime.json, instructions.md, skills/) -> the supervisor
        |-- creds/ + control/ (mounts) -----------> the supervisor
        `-- families/<family>/status.json --------> sessiond

Every test here runs the REAL writer and the REAL reader. Only the two
things that would touch a host are faked: the sandbox driver (`FakeDriver`)
and LiteLLM (`FakeLiteLLMKeys`). Nothing dials the host's LAN address and nothing
needs the host.

Every test in this directory is marked `slow`: the ones that are not slow
today share a root with the ones that spawn Node, and one marker over the
directory beats a reader guessing which is which.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import pytest
import yaml
from agent_managerd import paths
from agent_managerd.apply import ApplyResult, apply_once
from agent_managerd.credentials import read_creds
from agent_managerd.driver import FakeDriver
from agent_managerd.litellm_keys import FakeLiteLLMKeys

FAMILY: Final = "chat"
SKILL: Final = "kitchen"

#: The one sandbox an apply makes. The control directory is per sandbox
#: (contract 03 §7.1), so a fixture path needs the id.
FIRST_SANDBOX: Final = "chat-s1"

#: An obvious fixture, never resolved: contract 06 owns digest selection.
IMAGE: Final = "sha256:0000000000000000000000000000000000000000000000000000000000000001"

#: The repo root, four levels up from this file
#: (integration/tests_manager/conftest.py).
REPO_ROOT: Final = Path(__file__).resolve().parents[2]
SUPERVISOR_DIR: Final = REPO_ROOT / "supervisor"

# `sessiond/tests/sessiond_harness.py` holds the far side of the channel: a
# supervisor that speaks contract 03. Seam 4 reuses it rather than writing a
# second one that could disagree with it. pytest puts a test directory on
# `sys.path` only when it collects from that directory, and a run of
# `integration/tests_manager` alone collects nothing there.
sys.path.insert(0, str(REPO_ROOT / "sessiond" / "tests"))

#: The locked fleet's attended chat family, trimmed to what stage 1 runs.
#: `egress: []` is deliberate: contract 01 §3.7 rule 5 makes it the normal
#: attended case, and the manager adds the two plane endpoints itself.
CHAT_FAMILY: Final[dict[str, Any]] = {
    "name": FAMILY,
    "kind": "attended",
    "description": "The attended chat family.",
    "model": {"router": "agent-router", "budget_usd_per_day": 15},
    "verbs": {
        "embed": {},
        "ha_call": {"allow": [{"domain": "notify", "service": "mobile_app_example_phone"}]},
    },
    "egress": [],
    "shell": False,
    "sandbox_tools": ["read", "grep"],
    "skills": [SKILL],
}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """One marker over the directory (deliverable 1: `-m slow`)."""
    for item in items:
        item.add_marker(pytest.mark.slow)


@dataclass
class Applied:
    """One applied family: every path its readers open, and the two fakes.

    The fakes live here rather than per call because both stand in for a
    service that outlives one `apply_once`. A second `FakeLiteLLMKeys` would
    have forgotten the key the first one minted, which no real LiteLLM does.
    """

    result: ApplyResult
    driver: FakeDriver
    litellm: FakeLiteLLMKeys
    registry_root: Path
    state_root: Path
    sessions_root: Path

    @property
    def family(self) -> str:
        return FAMILY

    def reapply(self, body: dict[str, Any] | None = None) -> ApplyResult:
        """Rewrite the family file, then apply again, against the same
        LiteLLM and the same sandbox driver."""
        if body is not None:
            write_family(self.registry_root, body)

        self.result = apply(self.registry_root, self.state_root, self.driver, self.litellm)
        return self.result

    @property
    def token(self) -> str:
        """The PEP token `managerd` minted. A fixture value in a temp
        directory: it authorizes nothing outside this test process."""
        creds = read_creds(paths.creds_path(self.state_root, FAMILY))
        assert creds is not None, "apply_once wrote no creds.json"
        return creds.pep_token

    @property
    def grant_path(self) -> Path:
        return paths.grant_path(self.state_root, FAMILY)

    @property
    def config_dir(self) -> Path:
        return paths.config_dir(self.state_root, FAMILY)

    @property
    def creds_dir(self) -> Path:
        return paths.creds_dir(self.state_root, FAMILY)

    @property
    def control_dir(self) -> Path:
        """The first sandbox's own control directory (contract 03 §7.1)."""
        return paths.control_dir(self.state_root, FAMILY, FIRST_SANDBOX)

    @property
    def status_path(self) -> Path:
        return paths.status_path(self.state_root, FAMILY)


def write_family(registry_root: Path, body: dict[str, Any]) -> None:
    """One family directory, plus the one skill the file names."""
    family_dir = registry_root / "families" / str(body["name"])
    family_dir.mkdir(parents=True, exist_ok=True)
    (family_dir / "family.yaml").write_text(yaml.safe_dump(body), encoding="utf-8")
    (family_dir / "instructions.md").write_text(
        "You are the chat family. Answer plainly.\n", encoding="utf-8"
    )

    for name in body.get("skills", []):
        skill_dir = registry_root / "skills" / str(name)
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(f"# {name}\n\nA fixture skill.\n", encoding="utf-8")


def apply(
    registry_root: Path, state_root: Path, driver: FakeDriver, litellm: FakeLiteLLMKeys
) -> ApplyResult:
    """One `apply_once`, with the two host-touching things faked."""
    return apply_once(
        registry_root,
        FAMILY,
        state_root=state_root,
        image=IMAGE,
        driver=driver,
        litellm=litellm,
    )


def run_apply(registry_root: Path, state_root: Path, sessions_root: Path) -> Applied:
    driver = FakeDriver()
    litellm = FakeLiteLLMKeys()
    return Applied(
        result=apply(registry_root, state_root, driver, litellm),
        driver=driver,
        litellm=litellm,
        registry_root=registry_root,
        state_root=state_root,
        sessions_root=sessions_root,
    )


@pytest.fixture
def registry_root(tmp_path: Path) -> Path:
    root = tmp_path / "registry"
    write_family(root, CHAT_FAMILY)
    return root


@pytest.fixture
def applied(registry_root: Path, tmp_path: Path) -> Applied:
    """`apply_once` for the `chat` family, run once, green."""
    applied = run_apply(registry_root, tmp_path / "state", tmp_path / "sessions")
    assert applied.result.ok, f"apply_once failed: {applied.result.status.faults}"
    return applied


# --- Node, for the two seams that drive real TypeScript -------------------


def _node_or_skip() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH")
    return node


@pytest.fixture(scope="session")
def node_bin() -> str:
    return _node_or_skip()


#: The root conftest's example site address (TEST-NET-1, RFC 5737). The two
#: session fixtures below build before any test's environment is set, so
#: they pass it themselves: a supervisor bundle fixes it at build time.
EXAMPLE_LAN_ADDRESS = "192.0.2.10"


@pytest.fixture(scope="session")
def supervisor_build() -> Path:
    """`supervisor/`, installed and built with its own documented commands
    (`supervisor/README.md`: `pnpm install`, `pnpm build`). Session-scoped:
    the install is the expensive part and nothing here mutates it."""
    _node_or_skip()
    pnpm = shutil.which("pnpm")
    if pnpm is None:
        pytest.skip("pnpm is not on PATH")

    bundle = SUPERVISOR_DIR / "dist" / "pep-bridge.js"
    _run([pnpm, "install", "--frozen-lockfile"], SUPERVISOR_DIR, "pnpm install")
    build_env = {**os.environ, "AGENT_LAN_ADDRESS": EXAMPLE_LAN_ADDRESS}
    _run([pnpm, "build"], SUPERVISOR_DIR, "pnpm build", build_env)
    assert bundle.is_file(), f"pnpm build wrote no {bundle}"
    return SUPERVISOR_DIR


def _run(args: list[str], cwd: Path, what: str, env: dict[str, str] | None = None) -> None:
    done = subprocess.run(
        args, cwd=cwd, env=env, capture_output=True, text=True, check=False, timeout=600
    )
    if done.returncode != 0:
        pytest.fail(f"{what} failed ({done.returncode}):\n{done.stdout}\n{done.stderr}")


@pytest.fixture(scope="session")
def supervisor_driver(supervisor_build: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """`node/supervisor_driver.ts`, bundled with `supervisor/`'s own esbuild.

    The supervisor's composition root hardcodes the three sandbox mount
    points, so it cannot be pointed anywhere else. The driver replaces that
    one file and imports every other module from `supervisor/src/`. It is
    bundled into a temporary directory, so this package writes nothing into
    `supervisor/`.

    esbuild checks no types. `supervisor/`'s `pnpm typecheck` checks this
    driver: its `tsconfig.json` includes `node/`.
    """
    esbuild = supervisor_build / "node_modules" / ".bin" / "esbuild"
    if not esbuild.is_file():
        pytest.skip("supervisor/node_modules holds no esbuild")

    source = Path(__file__).parent / "node" / "supervisor_driver.ts"
    bundle = tmp_path_factory.mktemp("supervisor-driver") / "supervisor_driver.mjs"
    _run(
        [
            str(esbuild),
            str(source),
            "--bundle",
            "--platform=node",
            "--target=node24",
            "--format=esm",
            f"--define:__AGENT_LAN_ADDRESS__={json.dumps(EXAMPLE_LAN_ADDRESS)}",
            f"--outfile={bundle}",
        ],
        REPO_ROOT,
        "esbuild supervisor_driver.ts",
    )
    return bundle


@pytest.fixture
def scratch(tmp_path: Path) -> Iterator[Path]:
    """A directory for a driver's argv and its report file."""
    directory = tmp_path / "scratch"
    directory.mkdir()
    yield directory
