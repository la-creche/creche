"""One family's slow step never delays another family.

A sandbox replacement costs 1 to 3.5 minutes on the host. A loop that walked
eleven families one at a time would take twenty minutes to land a new image:
twenty minutes in which an edit to the eleventh family waits for the other
ten, and healthy families publish a document older than 90 seconds, which
contract 05 §2 rule 5 makes a reader call `unknown`.

These tests hold the rule.

    look -> dispatch chat ---(held inside sbx create)-------------> publish
         -> dispatch ops  --> publish            (ops never waits for chat)

No `sleep` synchronises anything here. A driver that must be caught inside
`create` sets a condition when it gets there, and every wait is bounded, so
a test that would hang fails instead."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, Final

import pytest
from agent_family import FamilyState, Registry
from caregiver.apply import apply_once
from caregiver.driver import DriverError, FakeDriver, SandboxSpec
from caregiver.egress import EgressConfig
from caregiver.litellm_keys import FakeLiteLLMKeys
from caregiver.loop import LoopConfig, LoopState, Passes, look, serve
from caregiver.reconcile import Actors
from caregiver.status import SandboxLifecycle, forget_published, restamp_status
from caregiver.switch import FakeSwitchClient
from caregiver.timers import FakeUnits
from caregiver_helpers import write_registry

from caregiver import loop as loop_module
from caregiver import paths, sandboxes

IMAGE: Final = "sha256:deadbeef"

#: Every wait here is bounded. Long enough that a loaded machine does not
#: trip it, short enough that a broken build fails inside a minute.
WAIT_S: Final = 20.0

#: A grace short enough to prove `serve` gives up on a step that outruns it,
#: without the test waiting out a real one.
SHORT_GRACE_S: Final = 0.2


NEWER_IMAGE = "127.0.0.1:5000/agent-sandbox@sha256:" + "9" * 64


class HoldingDriver(FakeDriver):
    """A `FakeDriver` that holds the named families inside `sbx create`.

    It answers the two questions a counter must answer: how
    many creates ran at once, and whether one family ever had two passes at
    the same time."""

    def __init__(self, hold: tuple[str, ...] = ()) -> None:
        super().__init__()
        self._hold = hold
        self._inside: dict[str, int] = {}
        self._state = threading.Condition()
        self.release = threading.Event()
        self.peak = 0
        self.family_peak = 0

    def create(self, spec: SandboxSpec) -> None:
        family = _family_of(spec.name)
        with self._state:
            self._inside[family] = self._inside.get(family, 0) + 1
            self.peak = max(self.peak, sum(self._inside.values()))
            self.family_peak = max(self.family_peak, self._inside[family])
            self._state.notify_all()

        if family in self._hold:
            self.release.wait(WAIT_S)

        with self._state:
            self._inside[family] -= 1

        super().create(spec)

    def wait_inside(self, count: int) -> None:
        """Block until `count` creates are inside at once."""
        with self._state:
            reached = self._state.wait_for(
                lambda: sum(self._inside.values()) >= count, timeout=WAIT_S
            )

        assert reached, f"only {self.peak} create(s) ever ran at once, wanted {count}"


def _family_of(sandbox: str) -> str:
    """`chat-s3` is the third sandbox of `chat` (contract 05 §4.1)."""
    return sandbox.rsplit("-s", 1)[0]


class Bench:
    """A registry, a state root and the four fakes, wired for the loop."""

    def __init__(self, tmp_path: Path) -> None:
        self.registry_root = tmp_path / "registry"
        self.state_root = tmp_path / "state"
        self.driver: FakeDriver = FakeDriver()
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

    def look(self, passes: Passes | None = None, **overrides: object) -> tuple[str, ...]:
        return look(self.config(**overrides), self.actors(), self.state, passes)

    def status_of(self, family: str) -> dict[str, Any]:
        raw = paths.status_path(self.state_root, family).read_text(encoding="utf-8")
        return json.loads(raw)

    def written_ns(self, family: str) -> int:
        """When the document was last written, at the filesystem's own
        resolution. `written_at` counts whole seconds, which is too coarse
        to tell two writes of one look apart."""
        return paths.status_path(self.state_root, family).stat().st_mtime_ns

    def live_ids(self, family: str) -> tuple[str, ...]:
        return tuple(
            one.id
            for one in sandboxes.read_ledger(self.state_root, family)
            if one.state in sandboxes.LIVE_STATES
        )


@pytest.fixture
def bench(tmp_path: Path) -> Bench:
    ready = Bench(tmp_path)
    write_registry(ready.registry_root)
    return ready


# --- the bound and the one-pass-per-family rule --------------------------------


def test_a_family_in_flight_is_never_started_again() -> None:
    """Contract 05 §2 has one writer per document, and every step of §4.3 is
    written to be safe to REPEAT, not to run beside itself."""
    with Passes(bound=4) as passes:
        gate = threading.Event()
        assert passes.start("chat", lambda: gate.wait(WAIT_S)) is True
        assert passes.start("chat", lambda: None) is False
        gate.set()
        assert passes.drain(WAIT_S) is True


def test_the_bound_holds() -> None:
    """Eleven first image pulls at once is not a plan. The bound is what
    keeps a fleet-wide replacement from asking for all of them."""
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
    """The whole rule. `chat` is stuck inside `sbx create`; `ops` must
    still converge and publish."""
    write_registry(bench.registry_root, name="ops")
    bench.driver = HoldingDriver(hold=("chat",))
    with Passes(bound=4) as passes:
        assert bench.look(passes) == ("chat", "ops")
        assert passes.wait_done("ops", WAIT_S) is True
        assert bench.status_of("ops")["state"] == FamilyState.IN_SYNC
        assert "chat" in passes.running()

        bench.driver.release.set()
        assert passes.drain(WAIT_S) is True

    assert bench.status_of("chat")["state"] == FamilyState.IN_SYNC


def test_one_family_never_has_two_passes_at_once(bench: Bench) -> None:
    """A second pass would take a second id, and an id is never reused
    (contract 05 §4.1)."""
    bench.driver = HoldingDriver(hold=("chat",))
    held = bench.driver
    with Passes(bound=4) as passes:
        bench.look(passes)
        held.wait_inside(1)
        for _ in range(5):
            assert bench.look(passes, heartbeat_s=0.0) == ()

        assert held.family_peak == 1
        assert paths.read_sandbox_sequence(bench.state_root, "chat") == 1

        held.release.set()
        assert passes.drain(WAIT_S) is True


def test_the_loop_starts_no_more_than_the_bound(bench: Bench) -> None:
    for name in ("ops", "code", "docs"):
        write_registry(bench.registry_root, name=name)

    bench.driver = HoldingDriver(hold=("chat", "ops", "code", "docs"))
    held = bench.driver
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


def test_a_held_family_publishes_that_it_is_converging(bench: Bench) -> None:
    """Contract 05 §3.4: while a pass runs, the document says `reconciling`
    and names the step. Without it the document of a family under
    replacement would be the OLD one until the replacement is over."""
    bench.driver = HoldingDriver(hold=("chat",))
    held = bench.driver
    with Passes(bound=4) as passes:
        bench.look(passes)
        held.wait_inside(1)
        doc = bench.status_of("chat")
        assert doc["state"] == FamilyState.RECONCILING
        assert doc["reconcile"]["step"] == "create_sandbox"

        held.release.set()
        assert passes.drain(WAIT_S) is True


def test_a_held_family_s_document_is_rewritten_while_it_waits(bench: Bench) -> None:
    """Contract 05 §2 rule 4. A three-minute `sbx create` publishes nothing
    of its own, so the loop restamps the document it already wrote."""
    bench.driver = HoldingDriver(hold=("chat",))
    held = bench.driver
    with Passes(bound=4) as passes:
        bench.look(passes, heartbeat_s=0.0)
        held.wait_inside(1)
        first = bench.written_ns("chat")
        assert bench.look(passes, heartbeat_s=0.0) == ()
        assert bench.written_ns("chat") > first

        held.release.set()
        assert passes.drain(WAIT_S) is True


def test_a_fresh_document_is_not_restamped(bench: Bench) -> None:
    """The restamp is for a document nothing else will rewrite in time, not
    a second writer racing the pass."""
    bench.driver = HoldingDriver(hold=("chat",))
    held = bench.driver
    with Passes(bound=4) as passes:
        bench.look(passes)
        held.wait_inside(1)
        first = bench.written_ns("chat")
        bench.look(passes)
        assert bench.written_ns("chat") == first

        held.release.set()
        assert passes.drain(WAIT_S) is True


def _restart_into_a_new_image(bench: Bench) -> dict[str, int]:
    """Three families the previous process converged, then a restart whose
    every create is held. Answers when each document was last written."""
    for name in ("ops", "code"):
        write_registry(bench.registry_root, name=name)

    with Passes(bound=4) as earlier:
        bench.look(earlier)
        assert earlier.drain(WAIT_S) is True

    before = {name: bench.written_ns(name) for name in ("chat", "code", "ops")}
    bench.state = LoopState()
    forget_published()
    bench.driver = HoldingDriver(hold=("chat", "code", "ops"))

    return before


def test_a_restamp_never_touches_a_document_this_process_never_wrote(bench: Bench) -> None:
    """Contract 05 §2 rule 8. A document from before a restart says
    `in_sync` and `ready` about the OLD image. A new `written_at` is all a
    restamp would give it, and `rework-cutover.sh up` reads fresh, `in_sync`
    and `ready` as a converged fleet."""
    _restart_into_a_new_image(bench)

    for name in ("chat", "code", "ops"):
        path = paths.status_path(bench.state_root, name)
        assert restamp_status(path, older_than_s=0.0) is False


def test_a_family_that_waits_for_a_slot_after_a_restart_is_looked_at(bench: Bench) -> None:
    """The fault of 2026-10-04. A release restarted the manager while the
    fleet moved onto a new image. Four creates held the four slots for
    minutes. Each family behind them kept the old process's document, which
    rule 8 forbids a restamp of, so it went stale and the verify hook failed
    on `heartbeat`.

    The waiting family now gets a look: every cheap step of its pass, and a
    stop before the first slow one. The document that follows is this
    process's own. It is never `in_sync`, because the look found a create
    to do and did not do it."""
    before = _restart_into_a_new_image(bench)
    held = bench.driver
    assert isinstance(held, HoldingDriver)
    with Passes(bound=1) as passes:
        assert bench.look(passes, image=NEWER_IMAGE, heartbeat_s=0.0) == ("chat",)
        held.wait_inside(1)

        for name in ("code", "ops"):
            assert passes.wait_done(name, WAIT_S) is True
            assert bench.written_ns(name) > before[name]
            document = bench.status_of(name)
            assert document["state"] == FamilyState.RECONCILING
            assert document["reconcile"] is not None
            # The sandbox the old process left is still the one that serves.
            assert [one["state"] for one in document["sandboxes"]] == [SandboxLifecycle.READY]

        # The look created nothing: the one slot is still `chat`'s.
        assert held.peak == 1
        assert bench.status_of("chat")["state"] == FamilyState.RECONCILING

        held.release.set()
        assert passes.drain(WAIT_S) is True


def test_a_look_is_not_the_pass(bench: Bench) -> None:
    """The family stays behind after its look. It takes the first slot that
    comes free, and the pass that follows does the create."""
    _restart_into_a_new_image(bench)
    held = bench.driver
    assert isinstance(held, HoldingDriver)
    old = {name: bench.live_ids(name) for name in ("code", "ops")}
    with Passes(bound=1) as passes:
        bench.look(passes, image=NEWER_IMAGE, heartbeat_s=0.0)
        held.wait_inside(1)
        for name in ("code", "ops"):
            assert passes.wait_done(name, WAIT_S) is True

        held.release.set()
        assert passes.drain(WAIT_S) is True
        for _ in range(4):
            bench.look(passes, image=NEWER_IMAGE, heartbeat_s=0.0)
            assert passes.drain(WAIT_S) is True

    for name in ("code", "ops"):
        assert bench.live_ids(name) != old[name]
        assert bench.status_of(name)["state"] == FamilyState.IN_SYNC


def test_a_family_is_looked_at_once(bench: Bench) -> None:
    """The look published this process's own document, so a restamp keeps
    it fresh from then on (rule 8). A second look would run every cheap
    step again, every two seconds, for as long as the slots are full."""
    _restart_into_a_new_image(bench)
    held = bench.driver
    assert isinstance(held, HoldingDriver)
    with Passes(bound=1) as passes:
        bench.look(passes, image=NEWER_IMAGE, heartbeat_s=0.0)
        held.wait_inside(1)
        for name in ("code", "ops"):
            assert passes.wait_done(name, WAIT_S) is True

        looked = {name: bench.written_ns(name) for name in ("code", "ops")}
        # A heartbeat far away: nothing is due a restamp, so any write here
        # would be a second look.
        assert bench.look(passes, image=NEWER_IMAGE, heartbeat_s=3600.0) == ()
        assert passes.running() == frozenset({"chat"})
        assert {name: bench.written_ns(name) for name in ("code", "ops")} == looked

        held.release.set()
        assert passes.drain(WAIT_S) is True


def test_a_waiting_family_with_nothing_to_do_reads_in_sync(bench: Bench) -> None:
    """A look runs the whole pass of a family that needs no slow step. Only
    `chat` moved here, so `ops` is converged, and its look says so."""
    write_registry(bench.registry_root, name="ops")
    with Passes(bound=4) as earlier:
        bench.look(earlier)
        assert earlier.drain(WAIT_S) is True

    bench.state = LoopState()
    forget_published()
    bench.driver = HoldingDriver(hold=("chat",))
    held = bench.driver
    # An edit that replaces `chat`'s sandbox and leaves `ops` alone.
    write_registry(bench.registry_root, sandbox={"cpus": 3, "memory": "3g"})
    with Passes(bound=1) as passes:
        assert bench.look(passes, heartbeat_s=0.0) == ("chat",)
        held.wait_inside(1)
        assert passes.wait_done("ops", WAIT_S) is True

        assert bench.status_of("ops")["state"] == FamilyState.IN_SYNC
        assert bench.state.of("ops").revision != ""

        held.release.set()
        assert passes.drain(WAIT_S) is True


def test_a_family_waiting_out_a_backoff_keeps_a_fresh_document(bench: Bench) -> None:
    """A create backoff reaches 300 seconds and §2 rule 5 calls a document
    older than 90 seconds `unknown`. A family nobody is converging is still
    a family `caregiver` is watching."""

    class FailingCreate(FakeDriver):
        def create(self, spec: SandboxSpec) -> None:
            super().create(spec)
            raise RuntimeError(f"sbx create {spec.name} failed: no such image")

    bench.driver = FailingCreate()
    bench.look(heartbeat_s=0.0)
    first = bench.written_ns("chat")
    bench.look(heartbeat_s=0.0)
    assert bench.written_ns("chat") > first
    assert paths.read_sandbox_sequence(bench.state_root, "chat") == 1


# --- the poll stays cheap ----------------------------------------------------------


def count_reads(bench: Bench, monkeypatch: pytest.MonkeyPatch) -> Callable[[], int]:
    """How many times a look opened the registry. A family that is behind
    and CANNOT act on it must not turn every two-second poll into a full
    registry read."""
    reads = 0
    real = loop_module.load_registry

    def counted(root: Path, host: object = None) -> Registry:
        nonlocal reads
        reads += 1
        return real(root, host)  # type: ignore[arg-type]

    monkeypatch.setattr(loop_module, "load_registry", counted)
    del bench
    return lambda: reads


def test_a_held_family_does_not_open_the_registry_every_tick(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One look opens the registry, finds the held family's document fresh
    and records that. The looks after it cost one content hash each. A
    three-minute create is 90 ticks, and reading eleven family files on
    each of them buys nothing."""
    bench.driver = HoldingDriver(hold=("chat",))
    held = bench.driver
    with Passes(bound=4) as passes:
        bench.look(passes)
        held.wait_inside(1)
        reads = count_reads(bench, monkeypatch)
        for _ in range(5):
            bench.look(passes)

        assert reads() == 1

        held.release.set()
        assert passes.drain(WAIT_S) is True


def test_a_backed_off_family_does_not_open_the_registry_every_tick(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FailingCreate(FakeDriver):
        def create(self, spec: SandboxSpec) -> None:
            super().create(spec)
            raise DriverError(f"sbx create {spec.name} failed: no such image")

    bench.driver = FailingCreate()
    bench.look()
    reads = count_reads(bench, monkeypatch)
    for _ in range(5):
        bench.look()

    assert reads() == 0


def test_an_edit_opens_the_registry_even_while_a_family_is_held(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cheap half of the gate is the content hash, and it still fires
    for a fleet whose slowest family is stuck."""
    bench.driver = HoldingDriver(hold=("chat",))
    held = bench.driver
    with Passes(bound=4) as passes:
        bench.look(passes)
        held.wait_inside(1)
        reads = count_reads(bench, monkeypatch)
        write_registry(bench.registry_root, description="Edited while it was busy.")
        bench.look(passes)
        assert reads() == 1

        held.release.set()
        assert passes.drain(WAIT_S) is True


# --- a registry edit during a slow pass -------------------------------------------


def test_an_edit_reaches_a_family_that_is_not_mid_pass(bench: Bench) -> None:
    """Invariant 9, which the sequential loop broke for twenty minutes."""
    write_registry(bench.registry_root, name="ops")
    bench.driver = HoldingDriver(hold=("chat",))
    held = bench.driver
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
    bench.driver = HoldingDriver(hold=("chat",))
    held = bench.driver
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
    pass is inside a step and not before it started one.

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


def test_a_stop_waits_for_the_step_in_flight(bench: Bench) -> None:
    """The unit sends SIGTERM. `serve` returns when the step in flight has
    finished, not while `sbx create` is still running."""
    bench.driver = HoldingDriver(hold=("chat",))
    held = bench.driver
    control = Sigterm()
    returned = _serving(bench, control, stop_grace_s=WAIT_S)

    held.wait_inside(1)
    control.raised.set()
    assert returned.is_set() is False

    held.release.set()
    assert returned.wait(WAIT_S) is True


def test_a_stop_gives_up_on_a_step_that_outruns_the_grace(bench: Bench) -> None:
    """A grace that never ends would hold the whole shutdown. What the step
    leaves behind is recoverable, so `serve` returns and says so."""
    bench.driver = HoldingDriver(hold=("chat",))
    held = bench.driver
    control = Sigterm()
    returned = _serving(bench, control, stop_grace_s=SHORT_GRACE_S)

    held.wait_inside(1)
    control.raised.set()
    assert returned.wait(WAIT_S) is True
    assert held.release.is_set() is False

    held.release.set()


def test_a_stop_runs_no_slow_step_after_the_one_in_flight(bench: Bench) -> None:
    """Contract 05 §4.3: a replacement is create, then switch, then destroy.
    A stop that arrived during the create must not go on to call
    `attendance`."""
    bench.look()
    write_registry(bench.registry_root, sandbox={"cpus": 4})
    bench.switch = FakeSwitchClient()
    bench.driver = HoldingDriver(hold=("chat",))
    held = bench.driver
    control = Sigterm()
    returned = _serving(bench, control, stop_grace_s=WAIT_S)

    held.wait_inside(1)
    control.raised.set()
    held.release.set()
    assert returned.wait(WAIT_S) is True

    assert bench.switch.requests == []
    assert set(bench.live_ids("chat")) == {"chat-s1", "chat-s2"}


def test_what_a_stop_left_behind_converges_on_the_next_start(bench: Bench) -> None:
    """The half-done replacement above, finished by the first look of a
    fresh process: a new `LoopState` is what a restart gives."""
    bench.look()
    write_registry(bench.registry_root, sandbox={"cpus": 4})
    bench.switch = FakeSwitchClient()
    bench.driver = HoldingDriver(hold=("chat",))
    held = bench.driver
    control = Sigterm()
    returned = _serving(bench, control, stop_grace_s=WAIT_S)

    held.wait_inside(1)
    control.raised.set()
    held.release.set()
    assert returned.wait(WAIT_S) is True
    assert set(bench.live_ids("chat")) == {"chat-s1", "chat-s2"}

    bench.driver = FakeDriver()
    bench.state = LoopState()
    bench.look()
    assert bench.live_ids("chat") == ("chat-s2",)
    assert bench.status_of("chat")["state"] == FamilyState.IN_SYNC


# --- what a SIGKILL in the middle of a create leaves behind ------------------------


def test_a_planned_sandbox_left_by_a_kill_is_never_adopted(bench: Bench) -> None:
    """A kill between the ledger's `planned` row and the end of §4.3 leaves
    a row for a sandbox whose egress was never proved. Adopting it would
    hand `attendance` a hole in deny-by-default (invariant 11)."""
    bench.look()
    serving = sandboxes.read_ledger(bench.state_root, "chat")[0]
    leftover = replace(serving, id="chat-s2", state=SandboxLifecycle.PLANNED, ready_at=None)
    sandboxes.write_ledger(bench.state_root, "chat", (serving, leftover))
    paths.sandbox_seq_path(bench.state_root, "chat").write_text("2\n", encoding="utf-8")

    bench.look(heartbeat_s=0.0)
    states = {one.id: one.state for one in sandboxes.read_ledger(bench.state_root, "chat")}
    assert states.get("chat-s2") is not SandboxLifecycle.PLANNED
    assert "chat-s2" not in bench.live_ids("chat")


def test_apply_once_never_adopts_a_planned_sandbox_either(bench: Bench) -> None:
    """`up` runs `apply-once` before the reconciler, and it too checks only
    that the VM exists. A VM whose canary probe never ran exists."""
    bench.look()
    serving = sandboxes.read_ledger(bench.state_root, "chat")[0]
    leftover = replace(serving, id="chat-s2", state=SandboxLifecycle.PLANNED, ready_at=None)
    sandboxes.write_ledger(bench.state_root, "chat", (leftover,))
    paths.sandbox_seq_path(bench.state_root, "chat").write_text("2\n", encoding="utf-8")
    bench.driver.create(sandbox_spec("chat-s2", bench))

    apply_once(
        bench.registry_root,
        "chat",
        state_root=bench.state_root,
        image=IMAGE,
        driver=bench.driver,
        litellm=bench.litellm,
    )
    assert bench.live_ids("chat") == ("chat-s3",)


def sandbox_spec(name: str, bench: Bench) -> SandboxSpec:
    """A VM under that name, so `driver.list_names()` reports it the way a
    kill after `sbx create` would."""
    return SandboxSpec(name=name, image=IMAGE, mounts=(), cpus=1, memory="1G")


def test_the_leftover_s_virtual_machine_is_destroyed(bench: Bench) -> None:
    """Whatever `sbx create` already built under that name goes with it. The
    id stays burnt: contract 05 §4.1 never reuses one."""
    bench.look()
    serving = sandboxes.read_ledger(bench.state_root, "chat")[0]
    leftover = replace(serving, id="chat-s2", state=SandboxLifecycle.PLANNED, ready_at=None)
    sandboxes.write_ledger(bench.state_root, "chat", (serving, leftover))
    paths.sandbox_seq_path(bench.state_root, "chat").write_text("2\n", encoding="utf-8")

    bench.look(heartbeat_s=0.0)
    destroyed = [call.args[0] for call in bench.driver.calls if call.op == "destroy"]
    assert "chat-s2" in destroyed
    assert paths.read_sandbox_sequence(bench.state_root, "chat") == 2
