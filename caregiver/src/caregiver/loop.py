"""The watch loop: look at the registry, converge every family, repeat
(contract 05 §1 "a reconciler, not a command the operator runs").

    tick -> content hash moved? -> load the registry
         -> per family: behind or its document going stale?
                        -> start ITS pass, in its own thread
                        -> already in flight? restamp its document instead
         -> wait out the poll interval

## Why the families do not take turns

A pass that replaces a sandbox costs 1 to 3.5 minutes. One pass at a
time, eleven families take twenty minutes after a new image, and for all
of it an edit to the family at the back of the queue waits for the ten
in front of it (invariant 9 says one action and no manual apply), and
the waiting documents keep a `written_at` that contract 05 §2 rule 5
makes a reader publish as `unknown`.

## The mechanism, and why this one

One thread per pass, a counting bound on how many run at once, and a set
of the families that have a pass in flight (`Passes`). Membership in
that set IS the per-family lock: a name in it is never started again, so
two passes of one family cannot overlap, and neither can a pass and the
delete of a family whose file went away.

The alternative was a worker pool with a queue. A queue adds a second
place where a family can be waiting and a second reason it is not
converging, and answers a question this loop already answers every two
seconds: what should run next. There is no queue. A family that finds
the bound full is started by the next look, which is also where every
other dispatch decision is made.

Every actor a pass touches is therefore shared between threads.
`httpx.Client` is thread-safe, so `HttpLiteLLMKeys` and
`HttpSwitchClient` are; `SbxDriver` keeps no state between calls;
`EgressConfig` is frozen; `UserUnits` takes a lock of its own around the
write-then-`systemctl` pair. Everything else a pass writes is under that
family's own directory, and `status.py` holds one lock per document
because the loop restamps a document whose pass is inside a slow step.

It sits beside `reconcile.py` rather than under it: it calls three
siblings (`reconcile`, `delete`, `applied`), which a leaf may not do
(`AGENTS.md`, layers)."""

from __future__ import annotations

import logging
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from types import FrameType
from typing import Any, Final, Protocol

from agent_family import HostFacts, Registry, load_registry, revision_of

from . import paths
from .applied import read_applied
from .chaperone_watch import PepReport, PepWatch, unwatched
from .delete import delete_family
from .images import SandboxImages
from .mcp_release import McpPaths
from .mcp_wire import McpReport, mcp_pass
from .reconcile import Actors, SpendRead, reconcile_family
from .released import ReleasedImages
from .rotate import settle
from .status import restamp_status
from .timers import remove_timers

log = logging.getLogger("caregiver.loop")

#: How often the loop reads the registry's content hash. Short, because a
#: permission change must need one action and no manual apply (invariant 9).
POLL_INTERVAL_S: Final = 2.0

#: Contract 05 §2 rule 4: the document is rewritten at least every 30 s, and
#: §2 rule 5 makes a reader call the family `unknown` past 90 s. Republishing
#: well inside the first bound keeps the second from ever firing on a
#: manager that is in fact running.
HEARTBEAT_S: Final = 20.0

#: Contract 05 §7: LiteLLM is the authority for spend. One read per family
#: per minute, not one per tick — spend moves with turns, not with ticks.
SPEND_INTERVAL_S: Final = 60.0

#: A failed create burns an id, and an id is never reused (contract 05
#: §4.1). Retrying every tick would burn one every two seconds, so each
#: failure doubles the wait up to the ceiling.
BACKOFF_FIRST_S: Final = 5.0
BACKOFF_MAX_S: Final = 300.0

#: The fault a create raises. A family carrying it waits out a backoff.
SANDBOX_START_FAILED: Final = "sandbox_start_failed"

#: A monotonic reading before any tick, so the first look is always a full
#: pass.
NEVER: Final = float("-inf")

#: A revision `revision_of` can never answer, so every family compares as
#: behind and takes a pass at the next look (`LoopState.force_pass`).
FORCE_PASS: Final = "\x00force-pass"

#: How many families may have a pass in flight at once.
#:
#: Not eleven. The host has cores and memory to spare, so neither is the
#: constraint; the shared image store is. The one expensive step of a
#: fleet-wide replacement is the FIRST pull of a new image (3.4 min on
#: 2026-09-21, against about 1 min for every family after it), and asking
#: eleven `sbx create` calls to pull the same image at once buys nothing
#: that the second one does not already get from the store. Four keeps
#: that to at most four first pulls, and still turns twenty minutes of
#: replacements into about five. `--max-concurrent-passes` moves it.
MAX_CONCURRENT_PASSES: Final = 4

#: How long a repeated complaint waits before it is summarised again.
#:
#: Ten minutes. A fault can last hours: at one look every two seconds, five
#: hours of a guard that writes a traceback per pass is nine thousand of
#: them, which teaches a reader to skip the log — the same silence by
#: another route.
#: One traceback, then one counted line every ten minutes.
REPEAT_EVERY_S: Final = 600.0

#: How many distinct complaints the ledger holds. A bounded dictionary,
#: because an error whose message carries a changing path would otherwise
#: grow one entry per pass for as long as it lasted.
MAX_COMPLAINTS: Final = 64

#: How long `serve` waits for the steps in flight after a stop.
#:
#: The longest single step is a create: `driver.CREATE_TIMEOUT_S` gives up
#: on `sbx create` at 300 s, and the same step's `sbx policy` calls add
#: `POLICY_TIMEOUT_S` each. The unit's `TimeoutStopSec` is 360 s, so this
#: leaves 30 s for the last publish and the exit: the service reports its
#: own stop rather than being killed halfway through one.
STOP_GRACE_S: Final = 330.0


class Control(Protocol):
    """What ends a wait, and what ends the loop. `SignalControl` is the
    real one. A test uses `FixedControl` and never sleeps."""

    def stopped(self) -> bool: ...
    def wait(self, seconds: float) -> None: ...


@dataclass
class _Complaint:
    """How often one distinct error has been caught, and when it was last
    written out."""

    count: int = 0
    said_at: float = NEVER


class Said:
    """The loop's complaint ledger: nothing is silent, nothing floods.

    `caregiver` runs as the operator, so a directory root leaves `root:root 0750`
    makes a `Path.is_dir` raise EACCES where it swallows ENOENT. An
    exception that leaves the loop ends the reconciler, and every status
    document goes on saying `in_sync`.

    So every filesystem read the LOOP THREAD makes is either mapped
    where it happens or caught here, and either way it is SAID: the first
    sighting of a distinct error gets its traceback, and every repeat is
    counted and summarised at most once per `REPEAT_EVERY_S`.

    Distinct means where it was caught, the error's class and its own
    message. A message carries the path and the errno, so two bad
    directories are two things to fix and each gets its own traceback.

    A mutex, because a family's pass runs in its own thread (`Passes`)
    and the loop thread writes here as well."""

    def __init__(self, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._monotonic = monotonic
        self._mutex = threading.Lock()
        self._seen: dict[str, _Complaint] = {}

    def say(self, where: str, exc: BaseException) -> None:
        """Write this error out, or count it and stay quiet."""
        key = f"{where}: {type(exc).__name__}: {exc}"
        count = self._note(key)
        if count == 0:
            return

        if count == 1:
            log.error("%s; the loop went on", key, exc_info=exc)
            return

        log.error("%s; %d times now, the loop goes on", key, count)

    def _note(self, key: str) -> int:
        """Count this sighting. Answers the count to write out, or 0 to
        stay quiet this time."""
        now = self._monotonic()
        with self._mutex:
            one = self._seen.get(key)
            if one is None:
                if len(self._seen) >= MAX_COMPLAINTS:
                    # The ledger is full of distinct messages, which is
                    # itself the fault. Say nothing rather than grow.
                    return 0

                one = _Complaint()
                self._seen[key] = one

            one.count += 1
            if one.count > 1 and now - one.said_at < REPEAT_EVERY_S:
                return 0

            one.said_at = now
            return one.count


class Passes:
    """The families that have a pass in flight, and the bound on how many.

    A name in `_running` is that family's lock. `start` refuses a name that
    is already there, so two passes of one family never overlap and neither
    does a pass and that family's delete. The bound is a count, not a
    queue: a family that does not fit is started by the next look."""

    def __init__(self, bound: int, grace_s: float = STOP_GRACE_S) -> None:
        self._bound = max(1, bound)
        self._grace_s = grace_s
        self._done = threading.Condition()
        self._running: set[str] = set()

    def __enter__(self) -> Passes:
        return self

    def __exit__(self, *_: object) -> None:
        """The one place a stop waits. Leaving with a pass still inside its
        step says so and returns: `serve` reporting its own stop beats
        systemd killing it mid-step."""
        if self.drain(self._grace_s):
            return

        log.warning(
            "stopping with %s still inside a step after %.0fs: %s",
            len(self._running),
            self._grace_s,
            ", ".join(sorted(self.running())) or "-",
        )

    def start(self, name: str, work: Callable[[], None]) -> bool:
        """Run `work` for `name` in its own thread. Answers False when a
        pass of `name` is already in flight, or the bound is full.

        The thread is a daemon: a step that outruns the stop grace is one
        `serve` has already decided to abandon, and what it leaves behind
        is what the next start recovers from (contract 05 §4.2)."""
        with self._done:
            if name in self._running or len(self._running) >= self._bound:
                return False

            self._running.add(name)

        thread = threading.Thread(
            target=self._run, args=(name, work), name=f"pass-{name}", daemon=True
        )
        try:
            thread.start()
        except Exception:
            # A thread that did not start runs no pass, so nothing else
            # releases the name. The caller's guard says the error.
            self._release(name)
            raise

        return True

    def running(self) -> frozenset[str]:
        with self._done:
            return frozenset(self._running)

    def drain(self, timeout_s: float) -> bool:
        """Wait for every pass in flight. Answers False on the timeout."""
        deadline = time.monotonic() + timeout_s
        with self._done:
            while self._running:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False

                self._done.wait(left)

        return True

    def wait_done(self, name: str, timeout_s: float) -> bool:
        """Wait for one family's pass. Answers False on the timeout."""
        with self._done:
            return self._done.wait_for(lambda: name not in self._running, timeout=timeout_s)

    def _run(self, name: str, work: Callable[[], None]) -> None:
        try:
            work()
        except Exception:
            # A worker that died quietly would hold this family's name for
            # ever, and the family would never converge again.
            log.exception("%s: pass raised", name)
        finally:
            self._release(name)

    def _release(self, name: str) -> None:
        with self._done:
            self._running.discard(name)
            self._done.notify_all()


@dataclass(frozen=True)
class LoopConfig:
    """Where the loop reads, where it writes, and its four intervals."""

    registry_root: Path
    state_root: Path
    image: str
    #: The second flavor's reference (contract 01 §3.9). Empty means this
    #: host was given none, and a family that asks for it is faulted
    #: rather than built from `base`.
    python_image: str = ""
    #: What a `playpen` release installed (`released.py`). When it has read
    #: one, its references win over the two above. None means this manager
    #: reads no released file, which is what a scratch run wants.
    released: ReleasedImages | None = None
    poll_interval_s: float = POLL_INTERVAL_S
    heartbeat_s: float = HEARTBEAT_S
    spend_interval_s: float = SPEND_INTERVAL_S
    host: HostFacts | None = None
    #: `stage7-releases.md` §4.1 step 3's paths. None means this manager
    #: files no MCP release request: the spool is root's and is created by
    #: hand. A default that
    #: pointed at the real paths would have a manager on a host without
    #: them writing into a directory that is not there, every two seconds,
    #: for nobody.
    mcp: McpPaths | None = None
    #: Wall clock, for the request's `ts` and the marker's age. The loop's
    #: own intervals use `time.monotonic` and are unaffected: a request the
    #: executor reads has to carry a time a human can compare with a ledger.
    clock: Callable[[], float] = time.time
    #: How many passes may run at once, and how long a stop waits for the steps
    #: already in flight. Both are `serve` flags: the right number depends
    #: on the host, not on this file.
    max_concurrent_passes: int = MAX_CONCURRENT_PASSES
    stop_grace_s: float = STOP_GRACE_S
    #: The fleet's one PEP watch (contract 05 §3.3). None means
    #: this manager was given no PEP address, the watch is OFF, and every
    #: document SAYS `pep.watch: off` — never nothing, because a watch that
    #: is silently off lets a PEP outage go unnoticed.
    chaperone: PepWatch | None = None


@dataclass(frozen=True)
class Backoff:
    """When this family may try to create a sandbox again, and how long it
    waited to get here."""

    next_at: float
    delay_s: float


@dataclass(frozen=True)
class FamilyLoop:
    """What the loop remembers about ONE family between looks: the revision
    its last pass ran against, when its document was last written, when its
    spend was last read, and how long it must wait after a burnt id.

    Per family, not per fleet: a family behind by one revision must not
    wait for another family's create."""

    revision: str = ""
    published_at: float = NEVER
    #: When its last real pass STARTED. A restamp moves `published_at`
    #: and not this, so the sweep can hand the next slot to the family
    #: that has waited longest for one.
    passed_at: float = NEVER
    spend_at: float = NEVER
    backoff: Backoff | None = None


class LoopState:
    """Everything the loop remembers between looks. It is a cache, not a
    record: losing it costs one extra pass and one extra spend read, and
    every durable fact lives in a file.

    A mutex, because a pass runs in its own thread and writes its own
    family's record when it ends, while the loop thread reads every
    record to decide what to start next."""

    def __init__(self) -> None:
        #: The registry revision the last look read. Fleet-wide: it is the
        #: content hash of the whole tree.
        self.revision: str = ""
        #: What the last MCP pass said (`stage7-releases.md` §4.1 step 3).
        #: It is the log line's source and the one thing a caller can read
        #: back, so a test reads what the REAL pass did instead of running
        #: a second one.
        self.mcp: McpReport = McpReport()
        #: The MCP line the loop last logged, so the same state is logged
        #: once, when it began, and not on every pass it lasts.
        self.mcp_line: str = ""
        #: Every error a guard caught, and how often. It lives here
        #: because it is exactly what this class is for: something the
        #: loop remembers between looks, and losing it costs one extra
        #: traceback. Every guard writes through this one ledger, so a
        #: path that two of them read is still one complaint.
        self.said: Said = Said()
        self._mutex = threading.Lock()
        self._families: dict[str, FamilyLoop] = {}

    def of(self, name: str) -> FamilyLoop:
        with self._mutex:
            return self._families.get(name, FamilyLoop())

    def note(self, name: str, **changes: Any) -> None:
        """Move some of one family's fields. Read and write under the one
        mutex, because the loop thread and that family's own pass both
        write here."""
        with self._mutex:
            current = self._families.get(name, FamilyLoop())
            self._families[name] = replace(current, **changes)

    def forget(self, name: str) -> None:
        with self._mutex:
            self._families.pop(name, None)

    def force_pass(self) -> None:
        """Every family is behind now, whatever the registry did.

        The PEP verdict moved, and a verdict lives in the status
        document. Only a PASS rewrites content: `restamp_status` carries
        the document it already published forward with nothing but a new
        time (contract 05 §2 rule 8). Without this a raised fault and a
        cleared one would each sit unsaid for up to one heartbeat, and a
        fault that is computed and never published is a fault nobody
        sees."""
        with self._mutex:
            self._families = {
                name: replace(one, revision=FORCE_PASS) for name, one in self._families.items()
            }

    def clear_backoff(self) -> None:
        """The operator acted. Every family waiting out a create backoff
        gets its retry now, because the edit may be the fix."""
        with self._mutex:
            self._families = {
                name: replace(one, backoff=None) for name, one in self._families.items()
            }

    def due(self, revision: str, heartbeat_s: float, busy: frozenset[str]) -> bool:
        """Is any family behind `revision` and able to act on it, or is any
        document due to be rewritten? A loop that knows no family yet has
        not looked, so it is due as well.

        The revision half is how an edit made DURING a family's pass lands
        as soon as that pass ends: the pass records the revision it
        converged to, which is now behind, and this says so on the very
        next tick rather than one heartbeat later.

        It ignores a family that cannot act on being behind — one whose
        pass is still in flight, one waiting out a create backoff — or a
        single broken family would turn every two-second poll into a full
        registry read for as long as it stayed broken. Neither loses
        anything: an edit moves the content hash, which is the other half
        of the gate, and the document keeps its own heartbeat here.

        This is what keeps the cheap poll cheap: it needs no registry, and
        most ticks it answers False and the look reads one content hash
        and stops."""
        with self._mutex:
            if not self._families:
                return True

            behind = (
                name
                for name, one in self._families.items()
                if one.revision != revision and name not in busy and _may_run(one)
            )
            if next(behind, None) is not None:
                return True

            oldest = min(one.published_at for one in self._families.values())

        return time.monotonic() - oldest >= heartbeat_s


def images_now(config: LoopConfig) -> SandboxImages:
    """The references this pass creates a sandbox from: a `playpen` release's
    when the manager has read one, the flags' when it has not. Read once per
    pass, so a release reaches every family within one heartbeat."""
    given = SandboxImages(base=config.image, python=config.python_image)
    if config.released is None:
        return given

    return config.released.current(given)


def serve(config: LoopConfig, actors: Actors, control: Control) -> LoopState:
    """Look, converge, wait, repeat, until `control` says stop.

    A stop starts no new pass — the loop has left — and the passes in
    flight see the same moment through `stopping`: each finishes the step
    it is inside and returns between steps."""
    state = LoopState()
    stopping = threading.Event()
    log.info(
        "caregiver: watching %s, state %s, images base=%s python=%s, up to %d passes at once, "
        "chaperone watch %s",
        config.registry_root,
        config.state_root,
        config.image,
        config.python_image or "(none)",
        config.max_concurrent_passes,
        _pep_report(config).url or "OFF (no --pep-url: nothing watches the PEP)",
    )
    with Passes(config.max_concurrent_passes, grace_s=config.stop_grace_s) as passes:
        while not control.stopped():
            _one_look(config, actors, state, passes, stopping.is_set)
            control.wait(config.poll_interval_s)

        stopping.set()

    log.info("caregiver: stopped")
    return state


def _one_look(
    config: LoopConfig,
    actors: Actors,
    state: LoopState,
    passes: Passes,
    stop: Callable[[], bool],
) -> None:
    """THE ONE GUARD AT THE LOOP'S TOP.

    Every read a look makes is either mapped where it happens — the four
    below this — or lands here. Nothing a pass READS may end this
    process: a directory whose mode is wrong for twenty minutes is a
    transient fault, and one read that escapes turns it into an outage.

    `Exception`, not `OSError`. The guards below name the error class
    they expect, and this one is the net under all of them: a bug in the
    loop's own arithmetic is a reason to say so and look again, never a
    reason for the fleet to stop converging.

    `serve` only. A one-shot verb (`reconcile-once`) still raises into
    its caller, because a command a human typed must fail where he can
    see it.
    """
    try:
        look(config, actors, state, passes, stop=stop)
    except Exception as exc:
        state.said.say("look", exc)


def look(
    config: LoopConfig,
    actors: Actors,
    state: LoopState,
    passes: Passes | None = None,
    *,
    stop: Callable[[], bool] = lambda: False,
) -> tuple[str, ...]:
    """One look at the registry. Answers the families whose pass it
    STARTED, which is empty when nothing moved and no document is due.

    Without `passes` the look is self-contained: it makes a set of its
    own and the call returns when every pass it started has finished. A
    one-shot caller and a test both want that. `serve` passes its own,
    and then a look dispatches and returns while the passes run on."""
    if passes is not None:
        return _look(config, actors, state, passes, stop)

    with Passes(config.max_concurrent_passes, grace_s=config.stop_grace_s) as own:
        return _one_shot(config, actors, state, own, stop)


def _one_shot(
    config: LoopConfig, actors: Actors, state: LoopState, passes: Passes, stop: Callable[[], bool]
) -> tuple[str, ...]:
    """One look, waited out. Every due family gets exactly ONE pass.

    A sweep starts at most the bound, so a fleet larger than the bound
    needs more than one. `done` is what makes it one pass each: a family
    this look already ran is skipped by the next sweep, however stale its
    document now looks. The tail — deletes, the MCP pass, the revision —
    runs once, after the last sweep, exactly as it does in `serve`."""
    ready = _begin(config, state, passes)
    if ready is None:
        return ()

    revision, registry = ready
    done: set[str] = set()
    while True:
        wave = _sweep(config, actors, registry, state, passes, stop, frozenset(done))
        passes.drain(config.stop_grace_s)
        if not wave:
            break

        done.update(wave)

    _finish(config, actors, registry, state, passes, revision)
    passes.drain(config.stop_grace_s)
    return tuple(sorted(done))


def _look(
    config: LoopConfig,
    actors: Actors,
    state: LoopState,
    passes: Passes,
    stop: Callable[[], bool],
) -> tuple[str, ...]:
    ready = _begin(config, state, passes)
    if ready is None:
        return ()

    revision, registry = ready
    started = _sweep(config, actors, registry, state, passes, stop, frozenset())
    _finish(config, actors, registry, state, passes, revision)
    return started


def _begin(config: LoopConfig, state: LoopState, passes: Passes) -> tuple[str, Registry] | None:
    """The cheap gate, then the registry. None when there is nothing to do.

    Most ticks this reads one content hash and stops, which is what keeps
    a two-second poll affordable. The PEP probe runs before the gate, at
    most once per its own interval, so a fleet that is fully converged
    still finds out the PEP has gone."""
    if _watch_pep(config):
        # A moved verdict makes every family behind, so the gate below
        # lets this look through and each pass republishes.
        state.force_pass()

    revision = revision_of(config.registry_root)
    moved = revision != state.revision
    if not moved and not state.due(revision, config.heartbeat_s, passes.running()):
        return None

    if moved:
        state.clear_backoff()

    return revision, load_registry(config.registry_root, config.host)


def _watch_pep(config: LoopConfig) -> bool:
    """One `/healthz` probe for the WHOLE fleet, at most once per the
    watch's interval. Answers whether the verdict moved.

    It runs on the LOOP thread, and a family's pass runs in its own
    (`Passes`), so the most it can cost is one probe timeout of dispatch
    latency once per interval — and only while the PEP is not answering,
    which is when a fault matters more than a second of latency."""
    if config.chaperone is None:
        return False

    return config.chaperone.poll()


def _pep_report(config: LoopConfig) -> PepReport:
    """What every pass of this look publishes. `off` when this manager
    was given no address: the document says so rather than nothing."""
    if config.chaperone is None:
        return unwatched()

    return config.chaperone.report()


def _sweep(
    config: LoopConfig,
    actors: Actors,
    registry: Registry,
    state: LoopState,
    passes: Passes,
    stop: Callable[[], bool],
    skip: frozenset[str],
) -> tuple[str, ...]:
    return tuple(
        name
        for name in _oldest_first(registry, state)
        if name not in skip and _dispatch(config, actors, registry, name, state, passes, stop)
    )


def _oldest_first(registry: Registry, state: LoopState) -> list[str]:
    """The family that waited longest for a real pass sweeps first.

    Every family falls due on the same heartbeat, and a pass outlasts the
    sweep, so the first `max_concurrent_passes` names take every slot. In
    name order those were the same names each time, and the rest only got
    a restamp: old content, new `written_at`, for 24 hours on 2026-09-28.
    Oldest first rotates the slots, so N families each pass at least once
    every ceil(N / bound) heartbeats."""
    return sorted(registry.reports, key=lambda name: (state.of(name).passed_at, name))


def _finish(
    config: LoopConfig,
    actors: Actors,
    registry: Registry,
    state: LoopState,
    passes: Passes,
    revision: str,
) -> None:
    """The tail of a look: the deletes, the MCP pass, the revision.

    Each of the two steps has a guard of its own, so one that raises does
    not stop the other. The revision is recorded whatever they did. A look
    that left it unrecorded would read every later tick as an edit, and an
    edit reads the whole registry and clears each create backoff."""
    try:
        _forget_deleted(config, actors, registry, state, passes)
    except Exception as exc:
        state.said.say("deleted families", exc)

    try:
        _mcp_servers(config, registry, state)
    except Exception as exc:
        state.said.say("mcp", exc)

    state.revision = revision


def _dispatch(
    config: LoopConfig,
    actors: Actors,
    registry: Registry,
    name: str,
    state: LoopState,
    passes: Passes,
    stop: Callable[[], bool],
) -> bool:
    """One family's dispatch, and its own guard.

    PER FAMILY, and that is the whole reason it is here rather than only
    at the loop's top: `_keep_fresh` reads and rewrites one family's
    document, and one family's unreadable directory must cost that
    family and no other. A guard only at the top would abandon the
    sweep, so ten healthy families would stop converging for the
    eleventh's bad mode.

    `Exception`, not `OSError`, for the same reason. An error of another
    class from one family's document or thread would end the sweep in the
    same way.
    """
    try:
        return _dispatch_one(config, actors, registry, name, state, passes, stop)
    except Exception as exc:
        state.said.say(f"{name}: dispatch", exc)
        return False


def _dispatch_one(
    config: LoopConfig,
    actors: Actors,
    registry: Registry,
    name: str,
    state: LoopState,
    passes: Passes,
    stop: Callable[[], bool],
) -> bool:
    """Start this family's pass, or keep its document fresh instead.

    A family gets a pass when it is behind the revision its last pass ran
    against, or when its document is going stale. It gets a restamp when
    it is due but cannot run: a pass of its own is still inside a step, the
    bound is full, or a burnt id has it waiting out a backoff. Either way
    the document is rewritten, which is what contract 05 §2 rule 4 asks
    for and §2 rule 5 punishes."""
    slot = state.of(name)
    if _due(config, slot, registry.revision) and _may_run(slot):
        spend = _spend_due(config, slot)
        work = _pass(config, actors, registry, name, state, spend, stop)
        if passes.start(name, work):
            state.note(name, passed_at=time.monotonic())
            if spend is SpendRead.READ:
                state.note(name, spend_at=time.monotonic())

            return True

    if _keep_fresh(config, name):
        # The document is fresh, so this family is not due on the heartbeat
        # half until the next one comes round. Without this the loop would
        # read the whole registry every two seconds for as long as one
        # family sat inside a three-minute create.
        state.note(name, published_at=time.monotonic())

    return False


def _due(config: LoopConfig, slot: FamilyLoop, revision: str) -> bool:
    if slot.revision != revision:
        return True

    return time.monotonic() - slot.published_at >= config.heartbeat_s


def _may_run(slot: FamilyLoop) -> bool:
    """A create that failed burnt an id, and an id is never reused
    (contract 05 §4.1). Retrying every tick would burn one every two
    seconds."""
    return slot.backoff is None or time.monotonic() >= slot.backoff.next_at


def _keep_fresh(config: LoopConfig, name: str) -> bool:
    """Contract 05 §2 rule 4 for a family nothing is about to publish.
    Answers whether the document is fresh now.

    A `sbx create` runs for minutes and writes no document of its own, and
    §2 rule 5 makes a reader call a document older than 90 seconds
    `unknown`. Without a restamp, a healthy family reads `unknown` for as
    long as that runs.

    A restamp that did not fire means the document is younger than the
    bound, which is the same answer: fresh. Only a family with no document
    at all is not, and that one is due a pass, not a restamp."""
    path = paths.status_path(config.state_root, name)
    if restamp_status(path, older_than_s=config.heartbeat_s):
        return True

    return path.is_file()


def _mcp_servers(config: LoopConfig, registry: Registry, state: LoopState) -> None:
    """Invariant 18's near end (`stage7-releases.md` §4.1 step 3).

    It runs AFTER the families, and its result changes nothing the loop
    returns: an MCP server is not a family, and a request that could not be
    filed must not hold up the pass that converges five of them.

    One thing it does change: a HELD request is contract 05 §3.3's
    `mcp_install_held`, which lives in every family's document. A verdict
    that moved therefore forces the next look to pass every family, for
    the reason `_watch_pep` gives — `restamp_status` carries the old
    content forward, so a fault left to the heartbeat is a fault nobody
    sees. A held verdict is stable between passes (its `since` is a time
    on disk), so this fires when it appears and when it clears, never in
    between.
    """
    if config.mcp is None:
        return

    report = mcp_pass(registry, config.mcp, config.clock())
    held_moved = report.held != state.mcp.held
    state.mcp = report
    if held_moved:
        state.force_pass()

    line = report.log_line()
    if line == state.mcp_line:
        # The same line as the last pass says nothing new: ten open gaps
        # would log the same `secret gaps open` line every twenty seconds,
        # 4300 times a day, while the gaps wait on ten taps of a phone. A
        # change is logged; a state that stays is one line at the moment
        # it began.
        return

    state.mcp_line = line
    if not line:
        return

    if report.problems:
        log.warning("mcp: %s", line)

        return

    log.info("mcp: %s", line)


def _spend_due(config: LoopConfig, slot: FamilyLoop) -> SpendRead:
    """Contract 05 §7: one spend read per family per minute. Per family,
    because the families do not share a look."""
    if time.monotonic() - slot.spend_at < config.spend_interval_s:
        return SpendRead.SKIP

    return SpendRead.READ


def _pass(
    config: LoopConfig,
    actors: Actors,
    registry: Registry,
    name: str,
    state: LoopState,
    spend: SpendRead,
    stop: Callable[[], bool],
) -> Callable[[], None]:
    """One family's pass, ready to run in its own thread.

    It closes over the registry the look READ, not the one on disk when
    the thread gets there: the revision it records is the revision it
    converged to, so the next look can tell that an edit made during the
    pass has still to land.

    A pass that raises is logged and skipped. One family's bad day is not a
    reason to stop converging the other ten (contract 01 §7 rule 5, the
    same reading applied to the loop)."""

    def run() -> None:
        try:
            _settle_rotation(config, name)
            images = images_now(config)
            result = reconcile_family(
                registry,
                name,
                state_root=config.state_root,
                image=images.base,
                python_image=images.python,
                actors=actors,
                spend=spend,
                stop=stop,
                chaperone=_pep_report(config),
                mcp=state.mcp,
            )
        except Exception as exc:
            # `Exception`, not `(OSError, RuntimeError)`. Anything else
            # would fall through to `Passes._run`, which logs and
            # releases the name but records NO backoff, so a family whose
            # pass raised a `ValueError` would retry every two seconds for
            # as long as it lasted. One line per pass here, and the first
            # traceback through the ledger.
            #
            # CONTRACT-QUESTION: contract 05 §3.3 fixes the fault codes and
            # has none for a pass that raised. This handler publishes no
            # fault: the log holds the error, and the document keeps what
            # the pass last wrote. A new code would say it in the document.
            # Each reader with a closed set of codes then refuses that
            # document until it knows the code.
            log.error("%s: pass raised %s: %s", name, type(exc).__name__, exc)
            state.said.say(f"{name}: pass", exc)
            # No `revision`: nothing converged. A wait, because a pass that
            # raises every two seconds is a busy loop against whatever is
            # already broken.
            state.note(name, published_at=time.monotonic(), backoff=_backoff(state.of(name), name))
            return

        # One line per family per pass, the family name first, so two
        # interleaved families are still followable.
        log.info("%s", result.log_line())
        failed = any(one.code == SANDBOX_START_FAILED for one in result.status.faults)
        state.note(
            name,
            revision=registry.revision,
            published_at=time.monotonic(),
            backoff=_backoff(state.of(name), name) if failed else None,
        )

    return run


def _settle_rotation(config: LoopConfig, name: str) -> None:
    """Drop a rotation overlap whose grace has run out (contract 05 §6.3
    step 5). A grace period is a fact over time, which is exactly what a
    one-shot verb cannot hold and this loop can.

    It runs BEFORE the pass, so the pass's own `write_grants` and this
    never disagree about which digests the file should carry.

    CONTRACT-QUESTION: contract 05 §6.3 step 5 ends the overlap when its
    grace runs out, and §3.1 touches nothing while a file is invalid. This
    reads §3.1 as the family's DEFINITION: the settle moves the digests
    and no grant, so it runs whatever the file says. Reading §3.1 as every
    file would keep the previous token accepted until the file is valid."""
    settle(name, state_root=config.state_root)


def _backoff(slot: FamilyLoop, name: str) -> Backoff:
    """A create that failed burnt an id. Doubling the wait is what keeps a
    family whose image does not exist from spending the whole id space
    before anybody reads the fault. A pass that raised gets the same wait
    for the same reason: two seconds is too soon to try again."""
    previous = slot.backoff
    delay = BACKOFF_FIRST_S if previous is None else min(previous.delay_s * 2, BACKOFF_MAX_S)
    log.warning("%s: pass did not converge, next attempt in %.0fs", name, delay)
    return Backoff(next_at=time.monotonic() + delay, delay_s=delay)


def _forget_deleted(
    config: LoopConfig, actors: Actors, registry: Registry, state: LoopState, passes: Passes
) -> tuple[str, ...]:
    """A family file that is gone is a family that is gone: credentials
    first, then its sandboxes, then its state (invariant 13, contract 05
    §4.4).

    The delete runs through `Passes` like a pass does, and for the same
    reason: it destroys the sandboxes a pass of that family may be in the
    middle of creating. A family whose own pass is still in flight keeps
    its name, so the delete waits for the next look.

    **An empty registry deletes nothing.** A vanished mount and a deleted
    fleet look identical from here, and one of the two readings destroys
    every family's credentials. `caregiver delete` is still there for
    the day the last family really does go."""
    if not registry.reports:
        return ()

    gone: list[str] = []
    for name in _families_with_state(config.state_root, state.said):
        if name in registry.reports:
            continue

        if passes.start(name, _delete_work(config, actors, name, state)):
            gone.append(name)

    return tuple(gone)


def _delete_work(
    config: LoopConfig, actors: Actors, name: str, state: LoopState
) -> Callable[[], None]:
    def run() -> None:
        try:
            _delete_one(config, actors, name)
        except Exception as exc:
            # `Exception`, not `(OSError, RuntimeError)`, and every step of
            # the delete is inside it. The state of the family stays, so
            # the next look starts the delete again. Through the ledger,
            # because that look comes two seconds later.
            state.said.say(f"{name}: delete", exc)
            return

        state.forget(name)
        log.info("%s: family file gone, deleted credentials then sandboxes", name)

    return run


def _delete_one(config: LoopConfig, actors: Actors, name: str) -> None:
    """The last applied file names the egress rows this family's sandboxes
    carry, and those rows must come out with them (contract 05 §4.4 step
    3).

    The timers go first of all. A schedule that outlives its family would
    keep asking for a session for a family with no key."""
    remove_timers(name, actors.units)
    applied = read_applied(config.state_root, name)
    delete_family(
        name,
        state_root=config.state_root,
        driver=actors.driver,
        litellm=actors.litellm,
        egress=tuple(applied.family.egress) if applied is not None else (),
        egress_config=actors.egress,
    )


def _families_with_state(state_root: Path, said: Said) -> tuple[str, ...]:
    """Every family this host holds state for, or none.

    A listing that cannot be read answers NONE, and `_forget_deleted`
    then deletes nothing: "I cannot list the families" must never become
    "no family has state", which is the branch that removes credentials.
    The same reading `_forget_deleted` already gives an empty registry.
    """
    families = state_root / paths.FAMILIES_DIR
    try:
        if not families.is_dir():
            return ()

        return tuple(sorted(one.name for one in families.iterdir() if one.is_dir()))
    except OSError as exc:
        said.say("families", exc)
        return ()


class SignalControl:
    """SIGHUP is "look now". SIGTERM and SIGINT are "stop".

    systemd sends the first on `systemctl --user reload` and the second on
    stop, so the unit needs no socket and no pid file of its own. The
    handlers only set an event: a handler runs between bytecodes, and doing
    real work there would put a half-written status document on disk."""

    def __init__(self) -> None:
        self._look = threading.Event()
        self._stop = threading.Event()

    def install(self) -> None:
        signal.signal(signal.SIGHUP, self._on_look)
        signal.signal(signal.SIGTERM, self._on_stop)
        signal.signal(signal.SIGINT, self._on_stop)

    def stopped(self) -> bool:
        return self._stop.is_set()

    def wait(self, seconds: float) -> None:
        """Ends early on a look-now or a stop. `Event.set` wakes the waiter,
        so a SIGHUP costs one tick of latency at most, not a full poll."""
        self._look.wait(seconds)
        self._look.clear()

    def _on_look(self, signum: int, frame: FrameType | None) -> None:
        del signum, frame
        self._look.set()

    def _on_stop(self, signum: int, frame: FrameType | None) -> None:
        del signum, frame
        self._stop.set()
        self._look.set()


class FixedControl:
    """Runs a known number of looks and never sleeps. Tests use it so a
    loop test costs no wall clock."""

    def __init__(self, looks: int) -> None:
        self.left = looks
        self.waits: list[float] = []

    def stopped(self) -> bool:
        if self.left <= 0:
            return True

        self.left -= 1
        return False

    def wait(self, seconds: float) -> None:
        self.waits.append(seconds)
