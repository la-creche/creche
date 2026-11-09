"""Packet C2's shared fixtures: one applied `chat` family, real on both sides.

`caregiver` writes the files that two other programs read:

    caregiver.apply_once
        |-- config/ (runtime.json, instructions.md, skills/) -> the vmm
        |-- creds/creds.json -----------------------------> the vmm
        `-- families/<family>/status.json ----------------> attendance

Every test here runs the REAL writer and the REAL reader. Only the one
thing that would touch a host is faked: LiteLLM (`FakeLiteLLMKeys`).
Nothing dials the host's LAN address and nothing needs the host.

Every test in this directory is marked `slow`: one marker over the
directory beats a reader guessing which is which.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import pytest
from agent_family import Fences
from caregiver.apply import ApplyResult, apply_once
from caregiver.credentials import read_creds
from caregiver.litellm_keys import FakeLiteLLMKeys
from caregiver.policy_fleet import FakeChecker, FleetPolicy, PolicyTarget
from noticeboard.tomlout import to_toml

from caregiver import paths

#: The fence of the test host (`agent_family.fences`).
FENCES: Final = Fences(platform_families=frozenset({"code"}))

FAMILY: Final = "chat"
SKILL: Final = "kitchen"

#: The repo root, four levels up from this file
#: (integration/tests_manager/conftest.py).
REPO_ROOT: Final = Path(__file__).resolve().parents[2]

# `attendance/tests/attendance_harness.py` holds the far side of the channel: a
# playpen that speaks contract 03. Seam 4 reuses it rather than writing a
# second one that could disagree with it. pytest puts a test directory on
# `sys.path` only when it collects from that directory, and a run of
# `integration/tests_manager` alone collects nothing there.
sys.path.insert(0, str(REPO_ROOT / "attendance" / "tests"))

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
    """One applied family: every path its readers open, and the fake.

    The fake lives here rather than per call because it stands in for a
    service that outlives one `apply_once`. A second `FakeLiteLLMKeys` would
    have forgotten the key the first one minted, which no real LiteLLM does.
    """

    result: ApplyResult
    litellm: FakeLiteLLMKeys
    registry_root: Path
    state_root: Path
    sessions_root: Path

    @property
    def family(self) -> str:
        return FAMILY

    def reapply(self, body: dict[str, Any] | None = None) -> ApplyResult:
        """Rewrite the family file, then apply again, against the same
        LiteLLM."""
        if body is not None:
            write_family(self.registry_root, body)

        self.result = apply(self.registry_root, self.state_root, self.litellm)
        return self.result

    @property
    def token(self) -> str:
        """The PEP token `caregiver` minted. A fixture value in a temp
        directory: it authorizes nothing outside this test process."""
        creds = read_creds(paths.creds_path(self.state_root, FAMILY))
        assert creds is not None, "apply_once wrote no creds.json"
        return creds.pep_token

    @property
    def config_dir(self) -> Path:
        return paths.config_dir(self.state_root, FAMILY)

    @property
    def creds_dir(self) -> Path:
        return paths.creds_dir(self.state_root, FAMILY)

    @property
    def status_path(self) -> Path:
        return paths.status_path(self.state_root, FAMILY)


def write_family(registry_root: Path, body: dict[str, Any]) -> None:
    """One family directory, plus the one skill the file names."""
    family_dir = registry_root / "families" / str(body["name"])
    family_dir.mkdir(parents=True, exist_ok=True)
    (family_dir / "family.toml").write_text(to_toml(body), encoding="utf-8")
    (family_dir / "instructions.md").write_text(
        "You are the chat family. Answer plainly.\n", encoding="utf-8"
    )

    for name in body.get("skills", []):
        skill_dir = registry_root / "skills" / str(name)
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(f"# {name}\n\nA fixture skill.\n", encoding="utf-8")


def apply(registry_root: Path, state_root: Path, litellm: FakeLiteLLMKeys) -> ApplyResult:
    """One `apply_once`, with LiteLLM faked."""
    return apply_once(
        registry_root,
        FAMILY,
        state_root=state_root,
        fences=FENCES,
        policy=FleetPolicy(PolicyTarget(state_root.parent / "chaperone"), FakeChecker()),
        litellm=litellm,
    )


def run_apply(registry_root: Path, state_root: Path, sessions_root: Path) -> Applied:
    litellm = FakeLiteLLMKeys()
    return Applied(
        result=apply(registry_root, state_root, litellm),
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
