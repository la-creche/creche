"""The watch loop.

`caregiver` is "a reconciler, not a command the operator runs" (contract 05 §1).
These tests hold the four things a loop adds over a single pass: when it
looks, how often it republishes, how long it waits after a create burnt an
id, and what it does about a family file that disappeared."""

from __future__ import annotations

import json
import shutil
import signal
import time
from dataclasses import replace
from pathlib import Path

import pytest
from agent_family import FamilyState
from caregiver.driver import DriverError, FakeDriver, SandboxSpec
from caregiver.egress import EgressConfig
from caregiver.litellm_keys import FakeLiteLLMKeys, key_alias
from caregiver.loop import (
    BACKOFF_FIRST_S,
    BACKOFF_MAX_S,
    FixedControl,
    LoopConfig,
    LoopState,
    SignalControl,
    look,
    serve,
)
from caregiver.reconcile import Actors
from caregiver.switch import FakeSwitchClient
from caregiver.timers import FakeUnits
from caregiver_helpers import KIND_MOVED, REFUSED_TOOLS, expire_overlap, write_registry

from caregiver import paths, sandboxes

IMAGE: str = "sha256:deadbeef"

#: Long enough that a wait which does NOT end early fails the test by
#: hanging the suite, rather than by passing a lenient bound.
LONG_WAIT_S: float = 30.0


class FailingCreate(FakeDriver):
    def create(self, spec: SandboxSpec) -> None:
        super().create(spec)
        raise DriverError(f"sbx create {spec.name} failed: no such image")


class Bench:
    """A registry, a state root and the three fakes, wired for the loop."""

    def __init__(self, tmp_path: Path) -> None:
        self.registry_root = tmp_path / "registry"
        self.state_root = tmp_path / "state"
        self.driver = FakeDriver()
        self.litellm = FakeLiteLLMKeys()
        self.switch = FakeSwitchClient()
        self.units = FakeUnits()
        self.state = LoopState()

    def config(self, **overrides: object) -> LoopConfig:
        fields: dict[str, object] = {
            "registry_root": self.registry_root,
            "state_root": self.state_root,
            "image": IMAGE,
        }
        fields.update(overrides)
        return LoopConfig(**fields)  # type: ignore[arg-type]

    def actors(self) -> Actors:
        return Actors(self.driver, self.litellm, self.switch, EgressConfig(), self.units)

    def look(self, **overrides: object) -> tuple[str, ...]:
        """The families this look ran a pass for. It waits for them: a
        look with no pool of its own is self-contained (`loop.look`)."""
        return look(self.config(**overrides), self.actors(), self.state)

    def status_of(self, family: str) -> dict[str, object]:
        raw = paths.status_path(self.state_root, family).read_text(encoding="utf-8")
        return json.loads(raw)


@pytest.fixture
def bench(tmp_path: Path) -> Bench:
    ready = Bench(tmp_path)
    write_registry(ready.registry_root)
    return ready


def expire(bench: Bench, name: str = "chat") -> None:
    """Let this family's backoff come due, without sleeping through it."""
    hold = bench.state.of(name).backoff
    if hold is not None:
        bench.state.note(name, backoff=replace(hold, next_at=time.monotonic() - 1.0))


def delay_of(bench: Bench, name: str = "chat") -> float:
    hold = bench.state.of(name).backoff
    assert hold is not None, f"{name} is not waiting out a backoff"
    return hold.delay_s


# --- when the loop looks ----------------------------------------------------------


def test_the_first_look_converges_every_family(bench: Bench) -> None:
    write_registry(bench.registry_root, name="ops")
    assert bench.look() == ("chat", "ops")


def test_an_unchanged_registry_is_not_looked_at_twice(bench: Bench) -> None:
    """A poll that found nothing must cost one content hash, not a pass per
    family."""
    bench.look()
    assert bench.look() == ()


def test_a_changed_file_is_picked_up_without_being_asked(bench: Bench) -> None:
    """Invariant 9: one action, no manual apply."""
    bench.look()
    write_registry(bench.registry_root, egress=["docs.python.org:443"])
    assert bench.look() == ("chat",)


def test_a_due_heartbeat_republishes_an_unchanged_family(bench: Bench) -> None:
    """Contract 05 §2 rule 4: the document is rewritten at least every 30 s,
    because §2 rule 5 makes a stale one mean "caregiver is not running"."""
    bench.look()
    assert bench.look(heartbeat_s=0.0) == ("chat",)


def test_a_new_family_joins_without_a_restart(bench: Bench) -> None:
    bench.look()
    write_registry(bench.registry_root, name="ops")
    assert bench.look() == ("chat", "ops")


# --- the loop itself ---------------------------------------------------------------


def test_serve_runs_one_look_per_turn_of_the_control(bench: Bench) -> None:
    control = FixedControl(looks=3)
    state = serve(bench.config(), bench.actors(), control)
    assert control.waits == [bench.config().poll_interval_s] * 3
    assert state.revision != ""


def test_a_look_leaves_the_family_in_sync(bench: Bench) -> None:
    """A look with no pool of its own waits for the passes it started, so
    this reads the document the pass wrote and not the one before it."""
    bench.look()
    assert bench.status_of("chat")["state"] == FamilyState.IN_SYNC


def test_one_family_that_raises_does_not_stop_the_others(bench: Bench) -> None:
    write_registry(bench.registry_root, name="ops")

    class HalfBroken(FakeDriver):
        def create(self, spec: SandboxSpec) -> None:
            if spec.name.startswith("chat"):
                raise OSError("the state root went away")

            super().create(spec)

    bench.driver = HalfBroken()
    # Both are STARTED: a look dispatches, and one of the two then dies in
    # its own thread without touching the other.
    assert bench.look() == ("chat", "ops")
    assert bench.status_of("ops")["state"] == FamilyState.IN_SYNC
    # `chat` published that it was creating, then died inside the create.
    assert bench.status_of("chat")["state"] == FamilyState.RECONCILING


def test_a_restart_mid_replacement_converges(bench: Bench) -> None:
    """A kill between the create and the switch leaves two sandboxes and
    the old applied snapshot. A fresh `LoopState`, as a restart gives,
    must finish the same replacement, not start a third."""
    bench.look()
    bench.switch = FakeSwitchClient(refuse="killed before the answer landed")
    write_registry(bench.registry_root, sandbox={"cpus": 4})
    bench.look()
    assert set(live_ids(bench)) == {"chat-s1", "chat-s2"}

    bench.switch = FakeSwitchClient()
    bench.state = LoopState()
    bench.look()
    assert live_ids(bench) == ("chat-s2",)
    assert bench.status_of("chat")["state"] == FamilyState.IN_SYNC


def test_the_replacement_never_holds_reach_the_new_file_dropped(bench: Bench) -> None:
    """One revision that both narrows egress and forces a replacement. At
    no point may a sandbox of this family carry a row the file dropped."""
    write_registry(bench.registry_root, egress=["docs.python.org:443"])
    bench.look()
    write_registry(bench.registry_root, egress=[], sandbox={"cpus": 4})
    bench.look()
    for record in sandboxes.read_ledger(bench.state_root, "chat"):
        if record.id == "chat-s2":
            assert "docs.python.org:443" not in record.allow


def live_ids(bench: Bench) -> tuple[str, ...]:
    return tuple(
        one.id
        for one in sandboxes.read_ledger(bench.state_root, "chat")
        if one.state in sandboxes.LIVE_STATES
    )


# --- a create that failed ------------------------------------------------------------


def test_a_failed_create_is_not_retried_on_the_next_tick(bench: Bench) -> None:
    """Each attempt burns an id, and an id is never reused (contract 05
    §4.1). A retry per tick would spend the id space in minutes."""
    bench.driver = FailingCreate()
    bench.look()
    assert paths.read_sandbox_sequence(bench.state_root, "chat") == 1
    bench.look(heartbeat_s=0.0)
    assert paths.read_sandbox_sequence(bench.state_root, "chat") == 1


def test_the_backoff_doubles_with_each_failure(bench: Bench) -> None:
    bench.driver = FailingCreate()
    bench.look()
    assert delay_of(bench) == BACKOFF_FIRST_S

    expire(bench)
    bench.look(heartbeat_s=0.0)
    assert delay_of(bench) == BACKOFF_FIRST_S * 2
    assert paths.read_sandbox_sequence(bench.state_root, "chat") == 2


def test_the_backoff_stops_doubling_at_the_ceiling(bench: Bench) -> None:
    bench.driver = FailingCreate()
    bench.look()
    for _ in range(10):
        expire(bench)
        bench.look(heartbeat_s=0.0)

    assert delay_of(bench) == BACKOFF_MAX_S


def test_an_edit_clears_the_backoff_at_once(bench: Bench) -> None:
    """The operator acted. The edit may be the fix the family is waiting
    for, so it must not sit out the rest of its wait."""
    bench.driver = FailingCreate()
    bench.look()
    bench.driver = FakeDriver()
    write_registry(bench.registry_root, description="Fixed.")
    bench.look()
    assert bench.state.of("chat").backoff is None
    assert bench.status_of("chat")["state"] == FamilyState.IN_SYNC


def test_a_success_forgets_the_backoff(bench: Bench) -> None:
    bench.driver = FailingCreate()
    bench.look()
    expire(bench)
    bench.driver = FakeDriver()
    bench.look(heartbeat_s=0.0)
    assert bench.state.of("chat").backoff is None


# --- a family file that disappeared ----------------------------------------------------


def test_a_deleted_family_file_deletes_the_family(bench: Bench) -> None:
    write_registry(bench.registry_root, name="ops")
    bench.look()
    shutil.rmtree(bench.registry_root / "families" / "ops")
    bench.look()
    assert not paths.family_dir(bench.state_root, "ops").exists()
    assert key_alias("ops") in bench.litellm.deleted


def test_a_deleted_family_loses_its_key_before_its_sandbox(bench: Bench) -> None:
    """Credentials die before processes (invariant 13)."""
    write_registry(bench.registry_root, name="ops")
    bench.look()
    seen: list[str] = []

    class WatchingDriver(FakeDriver):
        def destroy(self, name: str, allow: tuple[str, ...]) -> None:
            seen.append(f"destroy {name}")
            super().destroy(name, allow)

    class WatchingLiteLLM(FakeLiteLLMKeys):
        def delete_key(self, family: str) -> None:
            seen.append(f"delete_key {family}")
            super().delete_key(family)

    bench.driver = WatchingDriver()
    bench.litellm = WatchingLiteLLM()
    shutil.rmtree(bench.registry_root / "families" / "ops")
    bench.look()
    assert seen == ["delete_key ops", "destroy ops-s1"]


def test_an_empty_registry_deletes_nothing(bench: Bench) -> None:
    """A vanished mount and a deleted fleet look identical from here. One
    of the two readings destroys every family's credentials."""
    bench.look()
    shutil.rmtree(bench.registry_root / "families" / "chat")
    bench.look()
    assert paths.family_dir(bench.state_root, "chat").exists()
    assert bench.litellm.deleted == []


def test_an_invalid_family_file_is_never_treated_as_deleted(bench: Bench) -> None:
    """Invariant 19: a bad file keeps the last good definition serving. It
    still has a report, so the loop still sees the family."""
    write_registry(bench.registry_root, name="ops")
    bench.look()
    write_registry(bench.registry_root, name="ops", kind="nonsense")
    bench.look()
    assert paths.family_dir(bench.state_root, "ops").exists()
    assert bench.litellm.deleted == []


def test_an_invalid_family_file_never_settles_a_rotation(bench: Bench) -> None:
    """The settle runs before the pass, and it writes the grant file from
    the family it is handed. A file the validator refused still parses, so
    it would land in the one file the chaperone enforces."""
    bench.look()
    expire_overlap(bench.state_root)
    creds = paths.creds_path(bench.state_root, "chat").read_bytes()
    grant = paths.grant_path(bench.state_root, "chat").read_bytes()

    write_registry(bench.registry_root, tools=REFUSED_TOOLS)
    bench.look()

    assert bench.status_of("chat")["state"] == FamilyState.INVALID
    assert paths.creds_path(bench.state_root, "chat").read_bytes() == creds
    assert paths.grant_path(bench.state_root, "chat").read_bytes() == grant


def test_a_refused_kind_move_never_settles_a_rotation(bench: Bench) -> None:
    """A file whose `kind` moved has an ok report: only the applied
    snapshot proves the move. The pass refuses the file, so the settle
    before it must refuse the file too."""
    bench.look()
    expire_overlap(bench.state_root)
    creds = paths.creds_path(bench.state_root, "chat").read_bytes()
    grant = paths.grant_path(bench.state_root, "chat").read_bytes()

    write_registry(bench.registry_root, **KIND_MOVED)
    bench.look()

    status = bench.status_of("chat")
    assert status["state"] == FamilyState.INVALID
    assert "kind changed" in str(status["validation"])
    assert paths.creds_path(bench.state_root, "chat").read_bytes() == creds
    assert paths.grant_path(bench.state_root, "chat").read_bytes() == grant


# --- the signals a unit sends ------------------------------------------------------


@pytest.fixture
def restored_handlers() -> object:
    """`SignalControl.install` replaces three handlers. pytest runs in the
    main thread, so leaving them installed would outlive the test."""
    before = {one: signal.getsignal(one) for one in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)}
    yield None
    for number, handler in before.items():
        signal.signal(number, handler)


def test_a_hangup_ends_the_wait_at_once(restored_handlers: object) -> None:
    """A `SIGHUP` says "look now". `systemctl --user reload caregiver`
    is the local verb that sends it."""
    del restored_handlers
    control = SignalControl()
    control.install()
    signal.raise_signal(signal.SIGHUP)

    started = time.monotonic()
    control.wait(LONG_WAIT_S)
    assert time.monotonic() - started < 1.0
    assert control.stopped() is False


def test_a_second_wait_after_a_hangup_still_sleeps(restored_handlers: object) -> None:
    """The look-now flag is cleared by the wait it ended. A flag left set
    would turn the poll interval into a busy loop."""
    del restored_handlers
    control = SignalControl()
    control.install()
    signal.raise_signal(signal.SIGHUP)
    control.wait(LONG_WAIT_S)

    started = time.monotonic()
    control.wait(0.05)
    assert time.monotonic() - started >= 0.05


def test_a_termination_signal_stops_the_loop(restored_handlers: object) -> None:
    del restored_handlers
    control = SignalControl()
    control.install()
    signal.raise_signal(signal.SIGTERM)
    assert control.stopped() is True
    control.wait(LONG_WAIT_S)


# --- timers for autonomous families ------------------------------------------------


def test_an_autonomous_family_gets_its_timer(bench: Bench) -> None:
    write_registry(
        bench.registry_root,
        name="ops",
        kind="autonomous",
        triggers=[{"cron": "0 * * * *"}],
    )
    bench.look()
    assert "creche-trigger-ops-t1.timer" in bench.units.names()


def test_a_deleted_family_loses_its_timer_too(bench: Bench) -> None:
    """A schedule that outlives its family would keep asking for a session
    for a family with no key."""
    write_registry(
        bench.registry_root,
        name="ops",
        kind="autonomous",
        triggers=[{"cron": "0 * * * *"}],
    )
    bench.look()
    shutil.rmtree(bench.registry_root / "families" / "ops")
    bench.look()
    assert bench.units.names() == ()
