"""A tool the operator grants reaches a pi process that is already running (GRF).

No unit test can reach this one. `playpen/test/grant-refresh.test.ts`
drives the real bridge against `fake-pep.ts`, which has no grant file;
`pep/tests/test_pep_grf_manifest_revalidate.py` drives the real PEP with a
`TestClient`, which is not the bridge. The claim the packet makes is about
both at once, plus the file between them:

    grant file rewritten ──► PEP re-reads it per call (contract 04 §1.4)
             │
             └──► bridge poll, If-None-Match: "<old rev>"  (§4.2)
                       │
                       └──► 200 ──► registerTool / setActiveTools
                                     (contract 03 §7.3 rule 4)

The PEP is the real one, built through its real entry point. The bridge is
the real bundle. The grant file is written the way `caregiver` writes it,
atomically, with a fresh revision. Nothing here binds anything but loopback
and nothing needs the host.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from stack import repo_root

sys.path.insert(0, str(repo_root() / "integration" / "tests_manager"))

from pep_harness import build_pep, free_port, serving

#: The bridge bundle, and the driver that plays pi around it for as long as a
#: grant file takes to move.
DRIVER = repo_root() / "integration" / "tests_manager" / "node" / "grant_refresh_driver.mjs"
BUNDLE = repo_root() / "playpen" / "dist" / "pep-bridge.js"

FAMILY = "chat"
SESSION = "owui-grf1c2e3"
TURN = "01K5J9QWB9R1V3T5Y7H9J2K4P6"

#: An obvious fixture. Invariant 13 forbids a real token in a test fixture.
FAMILY_TOKEN = "FIXTURE-FAMILY-TOKEN-CHAT"

#: Fast enough for a suite. Production is 60 s (`MANIFEST_WATCH_MS`), and the
#: bridge takes its pace as a parameter for exactly this.
PACE_MS = 150

#: How many paces the driver stays alive. Two rewrites need far fewer, and
#: the bound is what stops a hung test leaving a node process behind.
DRIVER_POLLS = 60
DRIVER_TIMEOUT_S = 60.0

#: How long the test waits for one rewrite to reach the driver's report.
SETTLE_S = 15.0

EMBED = "embed"
HA_CALL = "ha_call"


def token_digest(token: str) -> str:
    import hashlib

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def grant_file(rework: Path) -> Path:
    return rework / "grants" / f"{FAMILY}.json"


def write_grants(rework: Path, verbs: dict[str, Any]) -> str:
    """Contract 04 §1.3's atomic write, with §1.2's fresh opaque revision.

    `caregiver` mints `uuid4().hex` per write (`steps.py`, `grant_revision`),
    so this does too: a test that reused a revision would be testing a
    revision the real writer never produces.
    """
    rev = uuid.uuid4().hex
    payload = {
        "version": 2,
        "family": FAMILY,
        "rev": rev,
        "token_sha256": [token_digest(FAMILY_TOKEN)],
        "model_alias": "agent-router",
        "tools": {},
        "verbs": verbs,
        "delegates": [],
        "approval": [],
        "limits": {"pep_rpm": 600, "max_inflight_delegations": 2, "max_open_gates": 10},
    }

    path = grant_file(rework)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(payload), encoding="utf-8")
    temp.replace(path)

    return rev


#: The two verbs this PEP can really execute, so §4 rule 4 offers them.
EMBED_ONLY: dict[str, Any] = {"embed": {}}
EMBED_AND_HA: dict[str, Any] = {
    "embed": {},
    "ha_call": {
        "allow": [{"domain": "notify", "service": "mobile_app_example_phone", "entity_id": None}]
    },
}


async def read_reports(report: Path, wanted: int) -> list[dict[str, Any]]:
    """The driver's report file, once it holds `wanted` entries."""
    deadline = asyncio.get_running_loop().time() + SETTLE_S
    while asyncio.get_running_loop().time() < deadline:
        try:
            entries = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            entries = []

        if len(entries) >= wanted:
            return list(entries)

        await asyncio.sleep(0.05)

    raise AssertionError(f"the bridge reported {wanted - 1} tool sets, never {wanted}: {report}")


def active_names(entry: dict[str, Any]) -> list[str]:
    return sorted(str(tool["name"]) for tool in entry["active"])


@pytest.fixture
def pep_url(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """The real PEP, on loopback, reading a rework root this test writes."""
    rework = tmp_path / "rework"
    (rework / "grants").mkdir(parents=True)
    app = build_pep(monkeypatch, tmp_path, rework)
    with serving(app, free_port()) as url:
        yield url, rework


@pytest.mark.skipif(not BUNDLE.is_file(), reason="run `pnpm build` in playpen/ first")
async def test_a_granted_verb_reaches_a_running_bridge(pep_url: Any, tmp_path: Path) -> None:
    """The whole of packet GRF, through the real PEP and the real bridge."""
    url, rework = pep_url
    write_grants(rework, EMBED_ONLY)

    report = tmp_path / "bridge-report.json"
    child = await asyncio.create_subprocess_exec(
        "node",
        str(DRIVER),
        str(BUNDLE),
        str(report),
        str(PACE_MS),
        str(DRIVER_POLLS),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PEP_URL": url,
            "PEP_TOKEN": FAMILY_TOKEN,
            "AGENT_SESSION": SESSION,
            "AGENT_TURN": TURN,
        },
    )

    try:
        first = await read_reports(report, 1)
        assert active_names(first[0]) == [EMBED]

        # The operator grants `ha_call`. This is the whole action: one file, written
        # once, with no restart, no signal and no apply command (invariant 9).
        write_grants(rework, EMBED_AND_HA)
        after_add = await read_reports(report, 2)
        assert active_names(after_add[1]) == sorted([EMBED, HA_CALL])

        # And they take it away again. pi has no unregister, so the tool leaves
        # the ACTIVE set and the PEP denies it from the moment the file moved.
        write_grants(rework, EMBED_ONLY)
        after_remove = await read_reports(report, 3)
        assert active_names(after_remove[2]) == [EMBED]
    finally:
        child.kill()
        await asyncio.wait_for(child.communicate(), timeout=DRIVER_TIMEOUT_S)
