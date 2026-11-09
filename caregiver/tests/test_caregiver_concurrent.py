"""One family's slow pass never delays another family.

A pass calls LiteLLM, `chaperone check` and systemctl, each with a timeout
of up to 30 s. A loop that walked eleven families one at a time would make
an edit to the eleventh family wait for each call of the other ten, and
healthy families would publish a document older than 90 seconds, which
contract 05 §2 rule 5 makes a reader call `unknown`.

These tests hold the rule.

    look -> dispatch chat ---(held inside its timer step)------> publish
         -> dispatch ops  --> publish            (ops never waits for chat)

No `sleep` synchronises anything here. A `Hold` that must be caught inside
the timer step sets a condition when it gets there, and every wait is
bounded, so a test that would hang fails instead."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

import pytest
from agent_family import FamilyFile, FamilyState, Registry
from caregiver.litellm_keys import FakeLiteLLMKeys
from caregiver.loop import LoopConfig, LoopState, Passes, look, serve
from caregiver.reconcile import Actors
from caregiver.status import forget_published, restamp_status
from caregiver.timers import FakeUnits, TimerOutcome, UnitWriter, apply_timers
from caregiver_helpers import FENCES, FakeVmm, fake_policy, write_registry

from caregiver import loop as loop_module
from caregiver import paths
from caregiver import reconcile as reconcile_module

#: Every wait here is bounded. Long enough that a loaded machine does not
#: trip it, short enough that a broken build fails inside a minute.
WAIT_S: Final = 20.0

#: A grace short enough to prove `serve` gives up on a pass that outruns
#: it, without the test waiting out a real one.
SHORT_GRACE_S: Final = 0.2


class Hold:
    """The timer step of each pass, which holds the named families inside it.

    It answers the two questions a counter must answer: how many passes
    were inside at once, and whether one family ever had two passes at the
    same time."""

    def __init__(self, hold: tuple[str, ...]) -> None:
        self._hold = hold
        self._inside: dict[str, int] = {}
        self._state = threading.Condition()
        self.release = threading.Event()
        self.peak = 0
        self.family_peak = 0

    def __call__(self, family: FamilyFile, units: UnitWriter) -> TimerOutcome:
        with self._state:
            self._inside[family.name] = self._inside.get(family.name, 0) + 1
            self.peak = max(self.peak, sum(self._inside.values()))
            self.family_peak = max(self.family_peak, self._inside[family.name])
            self._state.notify_all()

        if family.name in self._hold:
            self.release.wait(WAIT_S)

        with self._state:
            self._inside[family.name] -= 1

        return apply_timers(family, units)

    def wait_inside(self, count: int) -> None:
        """Block until `count` passes are inside at once."""
        with self._state:
            reached = self._state.wait_for(
                lambda: sum(self._inside.values()) >= count, timeout=WAIT_S
            )

        assert reached, f"only {self.peak} pass(es) were ever inside at once, wanted {count}"


class Bench:
    """A registry, a state root and the fakes, wired for the loop."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.registry_root = tmp_path / "registry"
        self.state_root = tmp_path / "state"
        self.litellm = FakeLiteLLMKeys()
        self.units = FakeUnits()
        self.vmm = FakeVmm()
        self.state = LoopState()
        self._monkeypatch = monkeypatch

    def hold(self, *families: str) -> Hold:
        """Hold the pass of each of `families` in its timer step from now on."""
        held = Hold(families)
        self._monkeypatch.setattr(reconcile_module, "apply_timers", held)
        return held

    def break_timers(self) -> None:
        """Make the timer step of each pass raise, as a bug would."""

        def broken(family: FamilyFile, units: UnitWriter) -> TimerOutcome:
            del units
            raise RuntimeError(f"{family.name}: the timer step broke")

        self._monkeypatch.setattr(reconcile_module, "apply_timers", broken)

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

    def look(self, passes: Passes | None = None, **overrides: object) -> tuple[str, ...]:
        return look(self.config(**overrides), self.actors(), self.state, passes)

    def converge(self) -> None:
        """One look with no hold, waited out: every family publishes."""
        with Passes(bound=4) as earlier:
            self.look(earlier)
            assert earlier.drain(WAIT_S) is True

    def status_of(self, family: str) -> dict[str, Any]:
        raw = paths.status_path(self.state_root, family).read_text(encoding="utf-8")
        return json.loads(raw)

    def written_ns(self, family: str) -> int:
        """When the document was last written, at the filesystem's own
        resolution. `written_at` counts whole seconds, which is too coarse
        to tell two writes of one look apart."""
        return paths.status_path(self.state_root, family).stat().st_mtime_ns


@pytest.fixture
def bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Bench:
    ready = Bench(tmp_path, monkeypatch)
    write_registry(ready.registry_root)
    return ready


# --- the bound and the one-pass-per-family rule --------------------------------


def test_a_family_in_flight_is_never_started_again() -> None:
    """Contract 05 §2 has one writer per document, and every step of a pass
    is written to be safe to REPEAT, not to run beside itself."""
    with Passes(bound=4) as passes:
        gate = threading.Event()
        assert passes.start("chat", lambda: gate.wait(WAIT_S)) is True
        assert passes.start("chat", lambda: None) is False
        gate.set()
        assert passes.drain(WAIT_S) is True


def test_the_bound_holds() -> None:
    gate = threading.Event()
    with Passes(bound=2) as passes:
        started = tuple(name for name in ("a", "b", "c", "d") if passes.start(name, gate.wait))
        assert started == ("a", "b")
        gate.set()
        assert passes.drain(WAIT_S) is True


def test_a_finished_pass_frees_its_slot() -> None:
    with Passes(bound=1) as passes:
        assert passes.start("chat", lambda: None) is True
        assert passes.drain(WAIT_S) is True
        assert passes.start("ops", lambda: None) is True
        assert passes.drain(WAIT_S) is True


def test_a_pass_that_raises_frees_its_slot_and_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A worker that died silently would leave the family's slot taken for
    ever, and the family would never converge again."""

    def angry() -> None:
        raise RuntimeError("the state root went away")

    with Passes(bound=1) as passes, caplog.at_level("ERROR"):
        assert passes.start("chat", angry) is True
        assert passes.drain(WAIT_S) is True
        assert passes.start("chat", lambda: None) is True
        assert passes.drain(WAIT_S) is True

    assert "chat" in caplog.text


def test_a_pass_that_cannot_start_frees_its_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    """The name of a family is its lock. A thread that does not start runs
    no pass, so nothing would release the name, and the family would never
    converge again."""

    def refuse(self: threading.Thread) -> None:
        del self
        raise RuntimeError("can't start new thread")

    with Passes(bound=1, grace_s=SHORT_GRACE_S) as passes:
        with monkeypatch.context() as patch:
            patch.setattr(threading.Thread, "start", refuse)
            with pytest.raises(RuntimeError):
                passes.start("chat", lambda: None)

        assert passes.running() == frozenset()
        assert passes.start("chat", lambda: None) is True
        assert passes.drain(WAIT_S) is True


# --- families converge independently -------------------------------------------


def test_a_held_family_does_not_hold_another(bench: Bench) -> None:
    """The whole rule. `chat` is stuck inside a step; `ops` must still
    converge and publish."""
    write_registry(bench.registry_root, name="ops")
    held = bench.hold("chat")
    with Passes(bound=4) as passes:
        assert bench.look(passes) == ("chat", "ops")
        assert passes.wait_done("ops", WAIT_S) is True
        assert bench.status_of("ops")["state"] == FamilyState.IN_SYNC
        assert "chat" in passes.running()

        held.release.set()
        assert passes.drain(WAIT_S) is True

    assert bench.status_of("chat")["state"] == FamilyState.IN_SYNC


def test_one_family_never_has_two_passes_at_once(bench: Bench) -> None:
    held = bench.hold("chat")
    with Passes(bound=4) as passes:
        bench.look(passes)
        held.wait_inside(1)
        for _ in range(5):
            assert bench.look(passes, heartbeat_s=0.0) == ()

        assert held.family_peak == 1

        held.release.set()
        assert passes.drain(WAIT_S) is True


def test_the_loop_starts_no_more_than_the_bound(bench: Bench) -> None:
    for name in ("ops", "code", "docs"):
        write_registry(bench.registry_root, name=name)

    held = bench.hold("chat", "ops", "code", "docs")
    with Passes(bound=2) as passes:
        assert bench.look(passes) == ("chat", "code")
        held.wait_inside(2)
        assert len(passes.running()) == 2
        assert held.peak == 2

        held.release.set()
        assert passes.drain(WAIT_S) is True


def test_every_family_gets_a_heartbeat_pass_past_the_bound(bench: Bench) -> None:
    """Six families, two slots, and every pass outlasts the sweep. A sweep
    in name order gave the same two families every slot, and the other four
    only ever got a restamp: old content, new `written_at` (the host,
    2026-09-28). Oldest pass first reaches all six in three heartbeats."""
    for name in ("code", "docs", "ops", "vault", "web"):
        write_registry(bench.registry_root, name=name)

    passed: set[str] = set()
    with Passes(bound=2) as passes:
        for _ in range(3):
            passed.update(bench.look(passes, heartbeat_s=0.0))
            assert passes.drain(WAIT_S) is True

    assert passed == {"chat", "code", "docs", "ops", "vault", "web"}


# --- every document stays fresh --------------------------------------------------


def test_a_held_family_s_document_is_rewritten_while_it_waits(bench: Bench) -> None:
    """Contract 05 §2 rule 4. A held pass publishes nothing of its own until
    it ends, so the loop restamps the document it already wrote."""
    bench.converge()
    held = bench.hold("chat")
    with Passes(bound=4) as passes:
        assert bench.look(passes, heartbeat_s=0.0) == ("chat",)
        held.wait_inside(1)
        first = bench.written_ns("chat")
        assert bench.look(passes, heartbeat_s=0.0) == ()
        assert bench.written_ns("chat") > first

        held.release.set()
        assert passes.drain(WAIT_S) is True


def test_a_fresh_document_is_not_restamped(bench: Bench) -> None:
    """The restamp is for a document nothing else will rewrite in time, not
    a second writer racing the pass."""
    bench.converge()
    held = bench.hold("chat")
    with Passes(bound=4) as passes:
        bench.look(passes, heartbeat_s=0.0)
        held.wait_inside(1)
        first = bench.written_ns("chat")
        bench.look(passes)
        assert bench.written_ns("chat") == first

        held.release.set()
        assert passes.drain(WAIT_S) is True


def _restart(bench: Bench) -> dict[str, int]:
    """Three families the previous process converged, then a restart.
    Answers when each document was last written."""
    for name in ("ops", "code"):
        write_registry(bench.registry_root, name=name)

    bench.converge()
    before = {name: bench.written_ns(name) for name in ("chat", "code", "ops")}
    bench.state = LoopState()
    forget_published()
    return before


def test_a_restamp_never_touches_a_document_this_process_never_wrote(bench: Bench) -> None:
    """Contract 05 §2 rule 8. A document from before a restart is the
    verdict of another process. A new `written_at` is all a restamp would
    give it."""
    _restart(bench)

    for name in ("chat", "code", "ops"):
        path = paths.status_path(bench.state_root, name)
        assert restamp_status(path, older_than_s=0.0) is False


def test_a_family_that_waits_for_a_slot_after_a_restart_is_looked_at(bench: Bench) -> None:
    """The fault of 2026-10-04. A release restarted the manager while the
    slots were full. Each family behind them kept the old process's
    document, which rule 8 forbids a restamp of, so it went stale and the
    verify hook failed on `heartbeat`.

    The waiting family now gets a look: its own pass, beyond the bound. The
    document that follows is this process's own."""
    before = _restart(bench)
    held = bench.hold("chat")
    with Passes(bound=1) as passes:
        assert bench.look(passes, heartbeat_s=0.0) == ("chat",)
        held.wait_inside(1)

        for name in ("code", "ops"):
            assert passes.wait_done(name, WAIT_S) is True
            assert bench.written_ns(name) > before[name]
            assert bench.status_of(name)["state"] == FamilyState.IN_SYNC

        assert passes.running() == frozenset({"chat"})

        held.release.set()
        assert passes.drain(WAIT_S) is True


def test_a_family_is_looked_at_once(bench: Bench) -> None:
    """The look published this process's own document, so a restamp keeps
    it fresh from then on (rule 8). A second look would run the pass again,
    every two seconds, for as long as the slots are full."""
    _restart(bench)
    held = bench.hold("chat")
    with Passes(bound=1) as passes:
        bench.look(passes, heartbeat_s=0.0)
        held.wait_inside(1)
        for name in ("code", "ops"):
            assert passes.wait_done(name, WAIT_S) is True

        looked = {name: bench.written_ns(name) for name in ("code", "ops")}
        # A heartbeat far away: nothing is due a restamp, so any write here
        # would be a second look.
        assert bench.look(passes, heartbeat_s=3600.0) == ()
        assert passes.running() == frozenset({"chat"})
        assert {name: bench.written_ns(name) for name in ("code", "ops")} == looked

        held.release.set()
        assert passes.drain(WAIT_S) is True


def test_a_family_waiting_out_a_backoff_keeps_a_fresh_document(bench: Bench) -> None:
    """A backoff reaches 300 seconds and §2 rule 5 calls a document older
    than 90 seconds `unknown`. A family nobody is converging is still a
    family `caregiver` is watching."""
    bench.converge()
    bench.break_timers()
    bench.look(heartbeat_s=0.0)
    first = bench.written_ns("chat")
    bench.look(heartbeat_s=0.0)
    assert bench.written_ns("chat") > first
    assert bench.state.of("chat").backoff is not None


# --- the poll stays cheap ----------------------------------------------------------


def count_reads(monkeypatch: pytest.MonkeyPatch) -> Callable[[], int]:
    """How many times a look opened the registry. A family that is behind
    and CANNOT act on it must not turn every two-second poll into a full
    registry read."""
    reads = 0
    real = loop_module.load_registry

    def counted(root: Path, host: object = None, fences: object = None) -> Registry:
        nonlocal reads
        reads += 1
        return real(root, host, fences)  # type: ignore[arg-type]

    monkeypatch.setattr(loop_module, "load_registry", counted)
    return lambda: reads


def test_a_held_family_does_not_open_the_registry_every_tick(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The held family's document is fresh, so the looks after the one that
    started its pass cost one content hash each."""
    bench.converge()
    held = bench.hold("chat")
    with Passes(bound=4) as passes:
        bench.look(passes, heartbeat_s=0.0)
        held.wait_inside(1)
        reads = count_reads(monkeypatch)
        for _ in range(5):
            bench.look(passes)

        assert reads() == 0

        held.release.set()
        assert passes.drain(WAIT_S) is True


def test_a_backed_off_family_does_not_open_the_registry_every_tick(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    bench.break_timers()
    bench.look()
    reads = count_reads(monkeypatch)
    for _ in range(5):
        bench.look()

    assert reads() == 0


def test_an_edit_opens_the_registry_even_while_a_family_is_held(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cheap half of the gate is the content hash, and it still fires
    for a fleet whose slowest family is stuck."""
    bench.converge()
    held = bench.hold("chat")
    with Passes(bound=4) as passes:
        bench.look(passes, heartbeat_s=0.0)
        held.wait_inside(1)
        reads = count_reads(monkeypatch)
        write_registry(bench.registry_root, description="Edited while it was busy.")
        bench.look(passes)
        assert reads() == 1

        held.release.set()
        assert passes.drain(WAIT_S) is True


# --- a registry edit during a slow pass -------------------------------------------


def test_an_edit_reaches_a_family_that_is_not_mid_pass(bench: Bench) -> None:
    """Invariant 9, which the sequential loop broke."""
    write_registry(bench.registry_root, name="ops")
    held = bench.hold("chat")
    with Passes(bound=4) as passes:
        bench.look(passes)
        assert passes.wait_done("ops", WAIT_S) is True
        held.wait_inside(1)
        before = bench.status_of("ops")["registry_rev"]

        write_registry(bench.registry_root, name="ops", egress=["docs.python.org:443"])
        assert bench.look(passes) == ("ops",)
        assert passes.wait_done("ops", WAIT_S) is True
        assert bench.status_of("ops")["registry_rev"] != before

        held.release.set()
        assert passes.drain(WAIT_S) is True


def test_an_edit_lands_on_the_held_family_as_soon_as_its_pass_ends(bench: Bench) -> None:
    """Not one heartbeat later: the pass records the revision it RAN
    against, so the very next look sees the family is behind."""
    held = bench.hold("chat")
    with Passes(bound=4) as passes:
        bench.look(passes)
        held.wait_inside(1)
        write_registry(bench.registry_root, description="Edited while it was busy.")
        assert bench.look(passes) == ()

        held.release.set()
        assert passes.wait_done("chat", WAIT_S) is True
        assert bench.look(passes) == ("chat",)
        assert passes.drain(WAIT_S) is True


# --- stop -------------------------------------------------------------------------


class Sigterm:
    """A `Control` the test stops by hand, so the stop lands while the
    pass is in flight and not before it started.

    `FixedControl` cannot do that: it stops after a counted number of
    looks, which is a race against the thread it just dispatched."""

    def __init__(self) -> None:
        self.raised = threading.Event()

    def stopped(self) -> bool:
        return self.raised.is_set()

    def wait(self, seconds: float) -> None:
        self.raised.wait(seconds)


def _serving(bench: Bench, control: Sigterm, **overrides: object) -> threading.Event:
    """`serve` in its own thread. The event says it returned."""
    returned = threading.Event()

    def run() -> None:
        serve(bench.config(poll_interval_s=0.05, **overrides), bench.actors(), control)
        returned.set()

    threading.Thread(target=run, name="serve", daemon=True).start()
    return returned


def test_a_stop_waits_for_the_pass_in_flight(bench: Bench) -> None:
    """The unit sends SIGTERM. `serve` returns when the pass in flight has
    ended, not while it is still running."""
    held = bench.hold("chat")
    control = Sigterm()
    returned = _serving(bench, control, stop_grace_s=WAIT_S)

    held.wait_inside(1)
    control.raised.set()
    assert returned.is_set() is False

    held.release.set()
    assert returned.wait(WAIT_S) is True


def test_a_stop_gives_up_on_a_pass_that_outruns_the_grace(bench: Bench) -> None:
    """A grace that never ends would hold the whole shutdown. Every step is
    idempotent, so `serve` returns and the next start repeats the pass."""
    held = bench.hold("chat")
    control = Sigterm()
    returned = _serving(bench, control, stop_grace_s=SHORT_GRACE_S)

    held.wait_inside(1)
    control.raised.set()
    assert returned.wait(WAIT_S) is True
    assert held.release.is_set() is False

    held.release.set()
