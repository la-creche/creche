"""Something watches the PEP (contract 05 §3.3).

A PEP that is down while nothing watches it leaves every family's status
document `in_sync` with no fault and the noticeboard showing a healthy
fleet, while no tool call works. These tests hold the
four rules that stop that happening quietly: one flap is not an outage,
the fault clears itself, a probe that hangs or raises never kills the
loop, and a watch with no address SAYS it is off."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from caregiver.chaperone_watch import (
    HEALTH_PATH,
    PEP_UNREACHABLE,
    FakePepProbe,
    HttpPepProbe,
    PepReach,
    PepWatch,
    RaisingPepProbe,
)
from caregiver.driver import FakeDriver
from caregiver.egress import EgressConfig
from caregiver.litellm_keys import FakeLiteLLMKeys
from caregiver.loop import LoopConfig, LoopState, look
from caregiver.reconcile import Actors
from caregiver.switch import FakeSwitchClient
from caregiver.timers import FakeUnits
from caregiver_helpers import write_registry

from caregiver import paths

PEP_URL: str = "http://chaperone.test:8300"
IMAGE: str = "sha256:deadbeef"


class Ticks:
    """A monotonic clock a test moves by hand, so no test sleeps."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def pass_s(self, seconds: float) -> None:
        self.now += seconds


def watch_of(
    probe: FakePepProbe | RaisingPepProbe | None,
    ticks: Ticks,
    *,
    interval_s: float = 10.0,
    threshold_s: float = 90.0,
) -> PepWatch:
    return PepWatch(
        probe,
        url=PEP_URL,
        interval_s=interval_s,
        threshold_s=threshold_s,
        monotonic=ticks,
    )


# --- the watch --------------------------------------------------------------


def test_no_address_means_the_watch_is_off_and_says_so() -> None:
    """A watch that is silently off is the same bug again."""
    watch = PepWatch(None, url="")

    watch.poll()
    report = watch.report()

    assert report.reach is PepReach.OFF
    assert report.fault() is None
    assert report.as_json() == {
        "watch": "off",
        "url": "",
        "checked_at": None,
        "unreachable_since": None,
    }


def test_a_healthy_pep_raises_nothing_and_stamps_the_check() -> None:
    ticks = Ticks()
    watch = watch_of(FakePepProbe(up=True), ticks)

    watch.poll()
    report = watch.report()

    assert report.reach is PepReach.OK
    assert report.fault() is None
    assert report.checked_at is not None
    assert report.as_json()["url"] == PEP_URL


def test_one_flap_is_not_an_outage() -> None:
    """`agent-control-deploy` restarts the PEP on every deploy, about 10
    to 15 s until `/healthz` answers. One failed probe must not fault
    eleven families."""
    ticks = Ticks()
    watch = watch_of(FakePepProbe(up=False), ticks)

    watch.poll()
    ticks.pass_s(15.0)
    watch.poll()

    assert watch.report().reach is PepReach.OK
    assert watch.report().fault() is None


def test_past_the_threshold_the_fault_is_raised_on_this_family() -> None:
    ticks = Ticks()
    watch = watch_of(FakePepProbe(up=False), ticks)

    watch.poll()
    ticks.pass_s(91.0)
    watch.poll()

    report = watch.report()
    fault = report.fault()
    assert report.reach is PepReach.UNREACHABLE
    assert fault is not None
    assert fault.code == PEP_UNREACHABLE
    assert fault.source == "managerd"
    # The contract's own row carries the reason.
    assert fault.blocks_turns is False
    assert fault.since == report.unreachable_since
    assert fault.detail["url"] == PEP_URL


def test_the_first_answer_clears_the_fault_with_no_turn_and_no_restart() -> None:
    ticks = Ticks()
    probe = FakePepProbe(up=False)
    watch = watch_of(probe, ticks)

    watch.poll()
    ticks.pass_s(91.0)
    watch.poll()
    assert watch.report().fault() is not None

    probe.up = True
    ticks.pass_s(10.0)
    watch.poll()

    assert watch.report().reach is PepReach.OK
    assert watch.report().fault() is None
    assert watch.report().unreachable_since is None


def test_one_probe_per_interval_for_the_whole_fleet() -> None:
    ticks = Ticks()
    probe = FakePepProbe(up=True)
    watch = watch_of(probe, ticks, interval_s=10.0)

    for _ in range(5):
        watch.poll()

    ticks.pass_s(10.0)
    watch.poll()

    # Five looks inside one interval cost one probe, not five, and never
    # one per family.
    assert probe.calls == 2


def test_a_probe_that_raises_never_leaves_the_watch() -> None:
    """One unmapped `httpx` error must not end `caregiver`. Every transport
    error is mapped, none escapes."""
    ticks = Ticks()
    watch = watch_of(RaisingPepProbe(), ticks)

    watch.poll()
    ticks.pass_s(91.0)
    watch.poll()

    # A probe that raises is a probe that did not answer 200.
    assert watch.report().reach is PepReach.UNREACHABLE


def test_moved_answers_only_when_the_verdict_changes() -> None:
    """The loop forces a pass on every family when this answers True, so
    the fault appears and clears without waiting out a heartbeat."""
    ticks = Ticks()
    probe = FakePepProbe(up=True)
    watch = watch_of(probe, ticks)

    assert watch.poll() is False  # ok, and it was ok
    probe.up = False
    ticks.pass_s(10.0)
    assert watch.poll() is False  # inside the threshold, still ok

    ticks.pass_s(91.0)
    assert watch.poll() is True  # ok -> unreachable

    probe.up = True
    ticks.pass_s(10.0)
    assert watch.poll() is True  # unreachable -> ok


def test_an_off_watch_probes_nothing_and_never_moves() -> None:
    watch = PepWatch(None, url="")

    assert watch.poll() is False
    assert watch.report().reach is PepReach.OFF


# --- the real probe ---------------------------------------------------------


@pytest.mark.parametrize(("status", "expected"), [(200, True), (204, False), (503, False)])
def test_only_a_200_counts_as_an_answer(status: int, expected: bool) -> None:
    seen: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(status)

    client = httpx.Client(transport=httpx.MockTransport(answer))
    probe = HttpPepProbe(PEP_URL, client=client)

    assert probe.healthy() is expected
    assert seen == [f"{PEP_URL}{HEALTH_PATH}"]


def test_every_transport_error_is_mapped_to_no_answer() -> None:
    """`litellm_keys._send`'s rule, applied here: a PEP that is AWAY is a
    failed probe, never an exception out of the loop."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = httpx.Client(transport=httpx.MockTransport(refuse))

    assert HttpPepProbe(PEP_URL, client=client).healthy() is False


def test_the_probe_carries_no_bearer() -> None:
    """Contract 05 §3.3: `/healthz` holds no secret and needs none, so the
    watch sends none (invariant 13)."""
    seen: list[httpx.Headers] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers)
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(answer))
    HttpPepProbe(PEP_URL, client=client).healthy()

    assert "authorization" not in seen[0]


# --- the documents MOVE -----------------------------------------------------


class Fleet:
    """A registry, a state root and the fakes, wired for one look. A fault
    that is computed and never published counts for nothing, so every
    assertion below reads the FILE."""

    def __init__(self, tmp_path: Path, watch: PepWatch | None) -> None:
        self.registry_root = tmp_path / "registry"
        self.state_root = tmp_path / "state"
        self.watch = watch
        self.state = LoopState()
        self.actors = Actors(
            FakeDriver(), FakeLiteLLMKeys(), FakeSwitchClient(), EgressConfig(), FakeUnits()
        )

    def look(self) -> None:
        config = LoopConfig(
            registry_root=self.registry_root,
            state_root=self.state_root,
            image=IMAGE,
            chaperone=self.watch,
        )
        look(config, self.actors, self.state)

    def document(self, family: str) -> dict[str, Any]:
        body = json.loads(paths.status_path(self.state_root, family).read_text(encoding="utf-8"))
        return cast("dict[str, Any]", body)

    def codes(self, family: str) -> list[str]:
        faults = cast("list[dict[str, Any]]", self.document(family)["faults"])
        return [one["code"] for one in faults]


def test_a_fault_reaches_the_document_and_leaves_it_again(tmp_path: Path) -> None:
    """The whole watch in one test: a PEP outage reaches the document, and
    leaves it again."""
    ticks = Ticks()
    probe = FakePepProbe(up=True)
    fleet = Fleet(tmp_path, watch_of(probe, ticks))
    write_registry(fleet.registry_root)

    fleet.look()
    assert fleet.codes("chat") == []
    assert fleet.document("chat")["pep"]["watch"] == "ok"

    probe.up = False
    ticks.pass_s(10.0)
    fleet.look()
    # One flap is not an outage: the first failed probe only starts the
    # clock, and a deploy's restart is over inside 15 s.
    assert fleet.codes("chat") == []

    ticks.pass_s(91.0)
    fleet.look()
    assert fleet.codes("chat") == [PEP_UNREACHABLE]
    assert fleet.document("chat")["state"] == "degraded"
    assert fleet.document("chat")["pep"]["watch"] == "unreachable"

    probe.up = True
    ticks.pass_s(10.0)
    fleet.look()
    assert fleet.codes("chat") == []
    assert fleet.document("chat")["state"] != "degraded"
    assert fleet.document("chat")["pep"]["watch"] == "ok"


def test_one_outage_faults_every_family(tmp_path: Path) -> None:
    """One PEP serves the fleet, so one outage is every family's outage —
    the reading §3.3 already gives `audit_unreadable`."""
    ticks = Ticks()
    probe = FakePepProbe(up=False)
    fleet = Fleet(tmp_path, watch_of(probe, ticks))
    write_registry(fleet.registry_root)
    write_registry(fleet.registry_root, name="code")

    fleet.look()
    ticks.pass_s(91.0)
    fleet.look()

    assert fleet.codes("chat") == [PEP_UNREACHABLE]
    assert fleet.codes("code") == [PEP_UNREACHABLE]
    # Two families, and still one probe per interval.
    assert probe.calls == 2


def test_a_document_says_the_watch_is_off(tmp_path: Path) -> None:
    """No address configured. The watch is off and the document says so,
    because a watch that is silently off is this same bug again."""
    fleet = Fleet(tmp_path, None)
    write_registry(fleet.registry_root)

    fleet.look()

    assert fleet.document("chat")["pep"] == {
        "watch": "off",
        "url": "",
        "checked_at": None,
        "unreachable_since": None,
    }
    assert fleet.codes("chat") == []


def test_no_sandbox_row_claims_a_running_turn(tmp_path: Path) -> None:
    """No row carries `turns_running`: `caregiver` cannot count running
    turns, and a 0 in every row would read as "0 running" on the
    noticeboard's front page."""
    fleet = Fleet(tmp_path, None)
    write_registry(fleet.registry_root)

    fleet.look()

    rows = cast("list[dict[str, Any]]", fleet.document("chat")["sandboxes"])
    assert rows
    assert all("turns_running" not in one for one in rows)
