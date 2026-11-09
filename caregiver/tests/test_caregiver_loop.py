"""The watch loop.

`caregiver` is "a reconciler, not a command the operator runs" (contract 05 §1).
These tests hold the four things a loop adds over a single pass: when it
looks, how often it republishes, how long it waits after a pass raised,
and what it does about a family file that disappeared."""

from __future__ import annotations

import json
import logging
import shutil
import signal
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from agent_family import FamilyState, Registry
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
from caregiver.reconcile import Actors, ReconcileResult
from caregiver.timers import FakeUnits
from caregiver_helpers import (
    FENCES,
    KIND_MOVED,
    REFUSED_TOOLS,
    FakeVmm,
    accepted_digests,
    current_digest,
    expire_overlap,
    fake_policy,
    grants_alone,
    write_no_file_dir,
    write_registry,
)

from caregiver import loop as loop_module
from caregiver import paths

#: Long enough that a wait which does NOT end early fails the test by
#: hanging the suite, rather than by passing a lenient bound.
LONG_WAIT_S: float = 30.0


class RaisingLiteLLM(FakeLiteLLMKeys):
    """A LiteLLM client whose key call raises an error no step handles, so
    the pass of each family in `broken` raises."""

    def __init__(self, broken: frozenset[str] = frozenset({"chat"})) -> None:
        super().__init__()
        self.broken = broken

    def ensure_key(self, family: str, models: list[str], budget_usd_per_day: float) -> str:
        if family in self.broken:
            raise OSError("the state root went away")

        return super().ensure_key(family, models, budget_usd_per_day)


class Bench:
    """A registry, a state root and the three fakes, wired for the loop."""

    def __init__(self, tmp_path: Path) -> None:
        self.registry_root = tmp_path / "registry"
        self.state_root = tmp_path / "state"
        self.vmm = FakeVmm()
        self.litellm: FakeLiteLLMKeys = FakeLiteLLMKeys()
        self.units = FakeUnits()
        self.state = LoopState()

    def config(self, **overrides: object) -> LoopConfig:
        fields: dict[str, object] = {
            "registry_root": self.registry_root,
            "state_root": self.state_root,
            "fences": FENCES,
            "vmm": self.vmm,
        }
        fields.update(overrides)
        return LoopConfig(**fields)  # type: ignore[arg-type]

    def actors(self) -> Actors:
        return Actors(self.litellm, self.units, fake_policy(self.state_root))

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
    bench.litellm = RaisingLiteLLM()
    # Both are STARTED: a look dispatches, and one of the two then dies in
    # its own thread without touching the other.
    assert bench.look() == ("chat", "ops")
    assert bench.status_of("ops")["state"] == FamilyState.IN_SYNC
    # `chat` died before it published anything.
    assert not paths.status_path(bench.state_root, "chat").exists()


# --- a pass that raised ---------------------------------------------------------------


def test_a_pass_that_raised_is_not_retried_on_the_next_tick(bench: Bench) -> None:
    """A retry per tick is a busy loop against whatever is broken."""
    bench.litellm = RaisingLiteLLM()
    assert bench.look() == ("chat",)
    assert bench.look(heartbeat_s=0.0) == ()


def test_the_backoff_doubles_with_each_failure(bench: Bench) -> None:
    bench.litellm = RaisingLiteLLM()
    bench.look()
    assert delay_of(bench) == BACKOFF_FIRST_S

    expire(bench)
    bench.look(heartbeat_s=0.0)
    assert delay_of(bench) == BACKOFF_FIRST_S * 2


def test_the_backoff_stops_doubling_at_the_ceiling(bench: Bench) -> None:
    bench.litellm = RaisingLiteLLM()
    bench.look()
    for _ in range(10):
        expire(bench)
        bench.look(heartbeat_s=0.0)

    assert delay_of(bench) == BACKOFF_MAX_S


def test_an_edit_clears_the_backoff_at_once(bench: Bench) -> None:
    """The operator acted. The edit may be the fix the family is waiting
    for, so it must not sit out the rest of its wait."""
    bench.litellm = RaisingLiteLLM()
    bench.look()
    bench.litellm = FakeLiteLLMKeys()
    write_registry(bench.registry_root, description="Fixed.")
    bench.look()
    assert bench.state.of("chat").backoff is None
    assert bench.status_of("chat")["state"] == FamilyState.IN_SYNC


def test_a_success_forgets_the_backoff(bench: Bench) -> None:
    bench.litellm = RaisingLiteLLM()
    bench.look()
    expire(bench)
    bench.litellm = FakeLiteLLMKeys()
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
    assert bench.vmm.dropped == ["ops"]


def test_a_deleted_family_loses_its_key_before_its_sessions(bench: Bench) -> None:
    """Credentials die before processes (invariant 13)."""
    write_registry(bench.registry_root, name="ops")
    bench.look()
    seen: list[str] = []

    class WatchingLiteLLM(FakeLiteLLMKeys):
        def delete_key(self, family: str) -> None:
            seen.append(f"delete_key {family}")
            super().delete_key(family)

    bench.vmm = FakeVmm(on_drop=lambda family: seen.append(f"drop {family}"))
    bench.litellm = WatchingLiteLLM()
    shutil.rmtree(bench.registry_root / "families" / "ops")
    bench.look()
    assert seen == ["delete_key ops", "drop ops"]


def test_a_drop_the_vmm_refuses_is_tried_again_after_a_wait(bench: Bench) -> None:
    """Decision CH20: a vmm that does not answer is transient. The state of
    the family stays, so a later look runs the delete again."""
    write_registry(bench.registry_root, name="ops")
    bench.look()
    shutil.rmtree(bench.registry_root / "families" / "ops")
    bench.vmm = FakeVmm(refuse="unreachable")
    bench.look()
    assert paths.family_dir(bench.state_root, "ops").is_dir()
    assert delay_of(bench, "ops") == BACKOFF_FIRST_S

    expire(bench, "ops")
    bench.vmm = FakeVmm()
    bench.look(heartbeat_s=0.0)
    assert not paths.family_dir(bench.state_root, "ops").exists()
    assert bench.vmm.dropped == ["ops"]


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


def _errors(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [one.getMessage() for one in caplog.records if one.levelno >= logging.ERROR]


def test_a_directory_with_no_family_file_is_ignored(
    bench: Bench, caplog: pytest.LogCaptureFixture
) -> None:
    """Contract 01 §5.6 rule 3. The loop starts no pass for the directory
    and writes no state for it. The family beside it converges."""
    write_no_file_dir(bench.registry_root)

    assert bench.look() == ("chat",)

    assert not paths.family_dir(bench.state_root, "stray").exists()
    assert _errors(caplog) == []


def test_a_family_whose_file_went_away_keeps_its_state(
    bench: Bench, caplog: pytest.LogCaptureFixture
) -> None:
    """The directory stays and holds no file. The loop ignores it: no pass
    and no delete, so the key and the sessions of the family stay."""
    write_registry(bench.registry_root, name="ops")
    bench.look()
    (bench.registry_root / "families" / "ops" / "family.toml").unlink()

    assert bench.look() == ("chat",)

    assert paths.creds_path(bench.state_root, "ops").exists()
    assert bench.litellm.deleted == []
    assert bench.vmm.dropped == []
    assert _errors(caplog) == []


def test_a_family_whose_file_went_away_is_named_one_time(
    bench: Bench, caplog: pytest.LogCaptureFixture
) -> None:
    """No pass keeps the status document of that family fresh, so the loop
    says it. One warning names the family, and the next look adds none. A
    directory that never had state gets no line."""
    write_registry(bench.registry_root, name="ops")
    bench.look()
    (bench.registry_root / "families" / "ops" / "family.toml").unlink()
    write_no_file_dir(bench.registry_root)

    with caplog.at_level(logging.WARNING, logger="caregiver.loop"):
        bench.look()
        bench.state.force_pass()
        assert bench.look() == ("chat",)

    warnings = [one.getMessage() for one in caplog.records if one.levelno == logging.WARNING]
    named = [line for line in warnings if line.startswith("ops: ")]
    assert len(named) == 1
    assert "family.toml" in named[0]
    assert not [line for line in warnings if line.startswith("stray: ")]


def test_an_ignored_directory_does_not_keep_the_loop_due(bench: Bench) -> None:
    """A family that the loop ignores is behind no revision. Without that,
    each poll reads the whole registry for as long as the directory stays."""
    write_registry(bench.registry_root, name="ops")
    bench.look()
    (bench.registry_root / "families" / "ops" / "family.toml").unlink()
    bench.look()

    assert bench.look() == ()
    assert not bench.state.due(bench.state.revision, LONG_WAIT_S, frozenset())


def test_an_invalid_family_file_still_ends_a_rotation_overlap(bench: Bench) -> None:
    """The settle runs before the pass and moves the digests alone. A file
    the validator refused cannot hold the previous token open, and no grant
    of that file reaches the one file the chaperone enforces."""
    bench.look()
    expire_overlap(bench.state_root)
    grants = grants_alone(bench.state_root)

    write_registry(bench.registry_root, tools=REFUSED_TOOLS)
    bench.look()

    assert bench.status_of("chat")["state"] == FamilyState.INVALID
    assert accepted_digests(bench.state_root) == [current_digest(bench.state_root)]
    assert grants_alone(bench.state_root) == grants


def test_a_refused_kind_move_still_ends_a_rotation_overlap(bench: Bench) -> None:
    """A file whose `kind` moved has an ok report: only the applied
    snapshot proves the move. The pass refuses the file, so the settle
    before it must write none of it."""
    bench.look()
    expire_overlap(bench.state_root)
    grants = grants_alone(bench.state_root)

    write_registry(bench.registry_root, **KIND_MOVED)
    bench.look()

    status = bench.status_of("chat")
    assert status["state"] == FamilyState.INVALID
    assert "kind changed" in str(status["validation"])
    assert accepted_digests(bench.state_root) == [current_digest(bench.state_root)]
    assert grants_alone(bench.state_root) == grants


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


def test_a_wait_says_when_a_hangup_ended_it(restored_handlers: object) -> None:
    """The loop asks the wait whether a signal asked for a look now."""
    del restored_handlers
    control = SignalControl()
    control.install()
    signal.raise_signal(signal.SIGHUP)

    assert control.wait(LONG_WAIT_S) is True
    assert control.wait(0.01) is False


class Asked(FixedControl):
    """A control whose first wait ends as a `SIGHUP` ends it, or as the
    poll interval ends it. The wait returns when the first pass of the
    family ended and its thread is gone, so the second look does not find
    that pass in flight."""

    def __init__(self, states: list[LoopState], *, hangup: bool) -> None:
        super().__init__(looks=2)
        self._states = states
        self._hangup = hangup

    def wait(self, seconds: float) -> bool:
        super().wait(seconds)
        deadline = time.monotonic() + LONG_WAIT_S
        while self._states[0].of("chat").revision == "" or self._in_flight():
            assert time.monotonic() < deadline, "the first pass did not end"
            time.sleep(0.01)

        return self._hangup and len(self.waits) == 1

    @staticmethod
    def _in_flight() -> bool:
        """`Passes` names the thread of a pass after its family."""
        return any(one.name == "pass-chat" for one in threading.enumerate())


@pytest.fixture
def loop_states(monkeypatch: pytest.MonkeyPatch) -> list[LoopState]:
    """Each `LoopState` that `serve` makes, for a control that reads one."""
    made: list[LoopState] = []

    class Recorded(LoopState):
        def __init__(self) -> None:
            super().__init__()
            made.append(self)

    monkeypatch.setattr(loop_module, "LoopState", Recorded)
    return made


@pytest.fixture
def passes_of(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The family of each pass that the loop runs, in order."""
    ran: list[str] = []
    real = loop_module.reconcile_family

    def counted(registry: Registry, family_name: str, **kwargs: Any) -> ReconcileResult:
        ran.append(family_name)
        return real(registry, family_name, **kwargs)

    monkeypatch.setattr(loop_module, "reconcile_family", counted)
    return ran


def test_a_hangup_brings_the_next_pass_forward(
    bench: Bench, loop_states: list[LoopState], passes_of: list[str]
) -> None:
    """`SIGHUP` says "look now". The family then takes a pass at the next
    look. It does not wait for the heartbeat."""
    serve(bench.config(), bench.actors(), Asked(loop_states, hangup=True))

    assert passes_of == ["chat", "chat"]


def test_a_wait_that_runs_out_brings_no_pass_forward(
    bench: Bench, loop_states: list[LoopState], passes_of: list[str]
) -> None:
    """Without the signal, the family waits for its heartbeat."""
    serve(bench.config(), bench.actors(), Asked(loop_states, hangup=False))

    assert passes_of == ["chat"]


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
    assert "creche-door-trigger-ops-t1.timer" in bench.units.names()


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
