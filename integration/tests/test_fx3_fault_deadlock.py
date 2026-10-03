"""A blocking fault that only blocked traffic could clear (packet FX3).

Gate 1b's `up` failed on the host on 2026-09-20 with `state=degraded` over one
fault: `grants_stale`, raised by the gate's OWN preflight minutes before
`caregiver` wrote the real grant file. `grants_stale` stops every turn
(contract 05 §3.3), and the PEP cleared it only when a CALL re-read a good
grant file (contract 04 §1.6 rule 3). Those two rules closed a loop:

    grant file gone for a moment
      -> the PEP raises grants_stale        (contract 04 §1.6 rule 1)
      -> attendance answers family_degraded   (contract 05 §3.3)
      -> no call reaches the PEP
      -> nothing re-reads the grant file
      -> the fault never clears

The orchestrator broke it by hand with one authenticated `GET /manifest`.

Two scenarios, one for each half of the fix. Both run the REAL PEP, built
through its own entry point, and the REAL `caregiver`. Neither needs
`attendance`: the deadlock is between these two.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from caregiver.apply import apply_once
from caregiver.credentials import read_creds
from caregiver.driver import FakeDriver
from caregiver.faults import FAULT_CLOCK_SLACK_S
from caregiver.litellm_keys import FakeLiteLLMKeys
from fastapi.testclient import TestClient
from stack import FAMILY, repo_root
from stage2 import IMAGE, write_registry

from caregiver import paths as caregiver_paths

#: `tests_manager/chaperone_harness.py` builds the real PEP through its real entry
#: point, as `stage3.py` does. One copy of `main()`'s plumbing, not two.
sys.path.insert(0, str(repo_root() / "integration" / "tests_manager"))

HTTP_OK = 200
HTTP_FORBIDDEN = 403

#: Short enough that a scenario does not wait on a production default, long
#: enough that it is still a timer and not a busy loop.
SWEEP_INTERVAL_S = "0.05"

#: Generous on purpose: this suite shares a machine with other builders'
#: suites, and the sweep above fires 200 times inside it.
CLEAR_DEADLINE_S = 10.0
POLL_STEP_S = 0.02

#: Contract 05 §3.3.1 rule 9 compares a second-resolution `since` with the
#: grant file's modification time, and keeps a fault raised inside one second
#: of the write. On the host the gap was MINUTES; here it has to be waited
#: out, or the scenario would test the slack instead of the rule.
GAP_S = FAULT_CLOCK_SLACK_S + 0.2


class Host:
    """One applied `chat` family and the real PEP that serves it."""

    def __init__(self, tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.registry_root = write_registry(tree / "registry")
        self.state_root = tree / "state"
        self.driver = FakeDriver()
        self.litellm = FakeLiteLLMKeys()
        self._tree = tree
        self._monkeypatch = monkeypatch

    def apply(self) -> Any:
        """One `apply_once`, with the two host-touching things faked."""
        return apply_once(
            self.registry_root,
            FAMILY,
            state_root=self.state_root,
            image=IMAGE,
            driver=self.driver,
            litellm=self.litellm,
        )

    def build_pep(self) -> Any:
        from chaperone_harness import build_pep

        self._monkeypatch.setenv("PEP_FAULT_SWEEP_INTERVAL_S", SWEEP_INTERVAL_S)
        return build_pep(self._monkeypatch, self._tree / "chaperone", self.state_root)

    @property
    def token(self) -> str:
        """The family token `caregiver` minted. A fixture value in a temp
        directory: it authorizes nothing outside this test process."""
        creds = read_creds(caregiver_paths.creds_path(self.state_root, FAMILY))
        assert creds is not None, "apply_once wrote no creds.json"
        return creds.pep_token

    @property
    def grant_path(self) -> Path:
        return caregiver_paths.grant_path(self.state_root, FAMILY)

    def pep_faults(self) -> list[dict[str, Any]]:
        """What the PEP currently reports for this family, as `caregiver`
        reads it (contract 05 §3.3.1). No file means no fault (rule 5)."""
        path = caregiver_paths.fault_path(self.state_root, "pep", FAMILY)
        if not path.is_file():
            return []

        body: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        entries: list[dict[str, Any]] = body["faults"]
        return entries


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Host:
    built = Host(tmp_path, monkeypatch)
    assert built.apply().ok, "the first apply must be green"
    return built


def manifest(chaperone: TestClient, token: str) -> int:
    return chaperone.get("/manifest", headers={"Authorization": f"Bearer {token}"}).status_code


def raise_the_fault(host: Host, chaperone: TestClient) -> None:
    """The gate's own sequence: serve the family, take its grant file away,
    then call again. The second call is what raises `grants_stale`, because
    `FamilyStore` keeps an entry for a family it has served before."""
    assert manifest(chaperone, host.token) == HTTP_OK
    host.grant_path.unlink()
    assert manifest(chaperone, host.token) == HTTP_FORBIDDEN

    codes = [one["code"] for one in host.pep_faults()]
    assert codes == ["grants_stale"], f"the PEP raised {codes}"

    time.sleep(GAP_S)


def test_an_apply_after_the_probe_publishes_in_sync(host: Host) -> None:
    """Gate 1b, end to end. `caregiver` writes the grant file the fault is no
    longer about, so the document says `in_sync` and no turn is refused."""
    with TestClient(host.build_pep()) as chaperone:
        raise_the_fault(host, chaperone)

    result = host.apply()

    assert result.status.faults == (), "a fault about the replaced grant file reached the document"
    assert result.status.state.value == "in_sync"
    assert result.ok is True


def test_an_idle_familys_fault_clears_with_no_call(host: Host) -> None:
    """The other half. Nothing calls the PEP after the grant file returns, so
    only the sweep of contract 04 §1.6 rule 7 can clear this."""
    with TestClient(host.build_pep()) as chaperone:
        raise_the_fault(host, chaperone)

        # `caregiver` puts the grant file back. From here nothing touches the
        # client: a call would clear the fault the old way and prove nothing.
        assert host.apply().ok
        cleared = _wait_until_cleared(host)

    assert cleared, f"the sweep left {host.pep_faults()} after {CLEAR_DEADLINE_S}s"


def _wait_until_cleared(host: Host) -> bool:
    deadline = time.monotonic() + CLEAR_DEADLINE_S
    while time.monotonic() < deadline:
        if host.pep_faults() == []:
            return True

        time.sleep(POLL_STEP_S)

    return False
