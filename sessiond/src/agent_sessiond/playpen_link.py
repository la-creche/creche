"""The host side of one channel to one family's sandbox (contract 03).

One sandbox per family, one channel per sandbox. This module owns the
handshake, the ping deadline, the demultiplexing of every session's events off
the one stdout, and the refusal rules of §13.

Everything that arrives here is untrusted. The playpen runs inside the
sandbox, so a line is a claim until it passes shape, addressing and ordering
(invariant 12). Nothing on this channel grants anything.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from . import wire
from .atomic import read_json
from .channel import Channel, ChannelClosed, SandboxDial
from .faults import FaultCode, FaultReporter
from .paths import playpen_lock_file
from .states import SessionKind
from .wire import (
    CHANNEL_IDLE_TTL_ATTENDED_S,
    CHANNEL_IDLE_TTL_OTHER_S,
    HOST_DEADLINE_S,
    LOCK_POLL_S,
    LOCK_STALE_S,
    MISSED_PONGS_ALLOWED,
    PI_IDLE_TTL_ATTENDED_S,
    PI_IDLE_TTL_OTHER_S,
    PING_INTERVAL_S,
    PROTOCOL_MAJOR,
    REFUSAL_BUDGET,
    REFUSAL_WINDOW_S,
    STOP_GRACE_MS,
    EntriesLine,
    EventLine,
    FailedLine,
    FatalLine,
    LogLine,
    OpenedLine,
    PongLine,
    ProcessExitLine,
    Ready,
    Refusal,
    SettledLine,
    encode,
    parse,
)

_LOG = logging.getLogger("sessiond")

_NONCE_BYTES = 4
FIRST_TURN_SEQ = 1

# Contract 03 §4.6: `shutdown` stops every process within its grace, then the
# playpen exits. An idle channel holds no process, so the exit is quick.
# This bounds a playpen that never goes.
EXIT_WAIT_S = STOP_GRACE_MS / 1_000


class HandshakeError(Exception):
    """The channel opened but the far side is not a usable playpen."""


class OrphanPlaypen(Exception):
    """An old playpen may still be alive in the VM (contract 03 §11.4)."""


class PlaypenFatal(HandshakeError):
    """The playpen said it cannot serve at all (contract 03 §5.7).

    A subclass of `HandshakeError` because it can arrive in place of `ready`:
    the control mount is read before the first protocol line, so the cause of
    the failure precedes the handshake it prevents.
    """


class Violation(Enum):
    """Why the host failed a turn on its own (contract 03 §13)."""

    SEQUENCE_GAP = "sequence_gap"


class LinkEvents(Protocol):
    """What the link hands upward. The service implements this."""

    async def on_event(self, session: str, turn: str, event: dict[str, Any]) -> None: ...

    async def on_settled(self, message: SettledLine) -> None: ...

    async def on_failed(self, message: FailedLine) -> None: ...

    async def on_violation(self, session: str, turn: str, why: Violation) -> None: ...

    async def on_process_exit(self, message: ProcessExitLine) -> None: ...

    async def on_log(self, message: LogLine) -> None: ...

    async def on_session_opened(self, message: OpenedLine) -> None: ...

    async def on_entries(self, message: EntriesLine) -> None: ...

    async def on_channel_lost(self, sandbox: str) -> None: ...


ChannelFactory = Callable[[SandboxDial], Channel]

# How many of the family's turns wait for a slot (contract 02 §13 rule 3).
QueueDepth = Callable[[], int]


def _nothing_queued() -> int:
    return 0


@dataclass(slots=True)
class _InFlight:
    """One turn the host currently has running on this sandbox."""

    session: str
    turn: str
    last_seq: int = 0


class _RefusalBudget:
    """Ten refused lines in 60 seconds closes the channel (§13 rule 2)."""

    def __init__(self, budget: int = REFUSAL_BUDGET, window_s: float = REFUSAL_WINDOW_S) -> None:
        self._budget = budget
        self._window_s = window_s
        self._marks: list[float] = []

    def count(self) -> bool:
        """Record one refusal. True when the budget is spent."""
        moment = time.monotonic()
        self._marks = [mark for mark in self._marks if moment - mark <= self._window_s]
        self._marks.append(moment)
        return len(self._marks) >= self._budget

    def reset(self) -> None:
        self._marks.clear()

    @property
    def spent(self) -> int:
        return len(self._marks)


class _BeatWatch:
    """The lock's counter, timed by the HOST's clock (contract 03 §11.4 rule 4).

    The host cannot test the playpen's pid, which lives in the VM's pid
    namespace, and it cannot compare `started_at`, which the sandbox's clock
    wrote. Two integers read a second apart are the one comparison that means
    the same thing on both sides.
    """

    def __init__(self, stale_s: float) -> None:
        self._stale_s = stale_s
        self._beat: int | None = None
        self._changed_at = time.monotonic()

    def observe(self, beat: int | None) -> bool:
        """Record one read. True when nothing has written for `stale_s`.

        A read that produced no counter — an empty, half-written or malformed
        file — decides nothing on its own (rule 3). It simply does not reset
        the clock, so a file that never reads cleanly for a whole window is
        treated the same as one nobody is writing.
        """
        if beat is not None and beat != self._beat:
            self._beat = beat
            self._changed_at = time.monotonic()
            return False

        return time.monotonic() - self._changed_at >= self._stale_s


def _lock_beat(path: Path) -> int | None:
    """The counter in the lock file, or None when it could not be read."""
    raw = read_json(path)

    if raw is None:
        return None

    beat = raw.get("beat")

    if isinstance(beat, bool) or not isinstance(beat, int) or beat < 0:
        return None

    return beat


def pi_idle_ttl_s(kind: SessionKind) -> int:
    """How long a pi process stays resident (contract 03 §6 rule 4)."""
    if kind is SessionKind.ATTENDED:
        return PI_IDLE_TTL_ATTENDED_S

    return PI_IDLE_TTL_OTHER_S


def channel_idle_ttl_s(kind: SessionKind) -> int:
    """When to close an idle channel (contract 03 §10 rules 2 and 3)."""
    if kind is SessionKind.ATTENDED:
        return CHANNEL_IDLE_TTL_ATTENDED_S

    return CHANNEL_IDLE_TTL_OTHER_S


class PlaypenLink:
    """One family's channel: handshake, demux, ping deadline, refusals."""

    def __init__(
        self,
        family: str,
        kind: SessionKind,
        events: LinkEvents,
        factory: ChannelFactory,
        faults: FaultReporter,
        state_root: Path,
        ping_interval_s: float = PING_INTERVAL_S,
        host_deadline_s: float = HOST_DEADLINE_S,
        lock_stale_s: float = LOCK_STALE_S,
        lock_poll_s: float = LOCK_POLL_S,
        idle_ttl_s: float = CHANNEL_IDLE_TTL_OTHER_S,
        exit_wait_s: float = EXIT_WAIT_S,
        queued: QueueDepth = _nothing_queued,
    ) -> None:
        self._family = family
        self._kind = kind
        self._events = events
        self._factory = factory
        self._faults = faults
        self._state_root = state_root
        self._ping_interval_s = ping_interval_s
        self._host_deadline_s = host_deadline_s
        self._lock_stale_s = lock_stale_s
        self._lock_poll_s = lock_poll_s
        # Contract 03 §10 rule 2: the kind decides whether an idle channel
        # closes at all, and `idle_ttl_s` only how soon. 0 never closes.
        self._idle_ttl_s = idle_ttl_s if channel_idle_ttl_s(kind) > 0 else 0.0
        self._exit_wait_s = exit_wait_s
        self._queued = queued

        self._channel: Channel | None = None
        self._ready: Ready | None = None
        # The sandbox this link last dialled. It outlives `close()`, because
        # a drop has to name the sandbox whose turns died (contract 05 §5.3
        # rule 2 keeps a second link open on the incoming sandbox).
        self._dialled: str | None = None
        # Sessions this host asked the playpen to hold a process for, on
        # the CURRENT channel. A switch closes each one before it shuts the
        # channel down (contract 05 §4.4 step 2), and a new channel starts
        # the set empty because no process crosses a sandbox (§5.3 rule 5).
        self._opened: set[str] = set()
        self._turns: dict[tuple[str, str], _InFlight] = {}
        self._violated: set[tuple[str, str]] = set()
        # Sessions the playpen says hold a pi process now (§5.2, §5.4,
        # §5.6). Any of them keeps the channel, and so the VM, up (§10 rule 3).
        self._resident: set[str] = set()
        # When this channel last had work: a dial, a turn, a pre-start or a
        # process coming or going. The idle window counts from here.
        self._active_at = time.monotonic()
        self._refusals = _RefusalBudget()
        self._reader: asyncio.Task[None] | None = None
        self._pinger: asyncio.Task[None] | None = None
        self._idler: asyncio.Task[None] | None = None
        self._unanswered = 0
        self._send_lock = asyncio.Lock()
        self._dial_lock = asyncio.Lock()

    @property
    def family(self) -> str:
        return self._family

    @property
    def sandbox(self) -> str | None:
        return self._channel.sandbox if self._channel is not None else None

    @property
    def is_open(self) -> bool:
        return self._channel is not None and self._channel.alive

    @property
    def ready(self) -> Ready | None:
        return self._ready

    async def ensure_open(self, dial: SandboxDial, epoch: int) -> None:
        """Open the channel to `dial.sandbox` when none is open there yet.

        One dial at a time. Contract 03 §1 rule 1 gives a family ONE channel,
        and concurrent turns all arrive here in the same moment, as five chats
        at once do. Dialling in parallel starts one `sbx exec`
        per turn, and every loser exits on the playpen lock (§11.4 rule 4)
        taking its turns' answers with it, so the lock is what makes the rule
        true rather than merely intended.
        """
        async with self._dial_lock:
            if self.is_open and self.sandbox == dial.sandbox:
                return

            if self._channel is not None:
                await self.close()

            await self._prove_no_orphan(dial.sandbox)
            await self._open(dial, epoch)

    async def close(self) -> None:
        """Drop the channel. In-flight turns are the caller's problem."""
        await self._stop_loops()

        if self._channel is not None:
            await self._channel.close()
            self._channel = None

        self._ready = None
        self._opened.clear()
        self._turns.clear()
        self._violated.clear()
        self._resident.clear()
        self._unanswered = 0

    async def _stop_loops(self) -> None:
        # Every loop can end up here, through `_lost` or an idle close, so one
        # of these tasks can be the task running this line. Awaiting itself
        # raises, and it does not need cancelling: its caller returns from the
        # loop immediately.
        #
        # All are cancelled before any is awaited. The first loop here stops
        # the others before it yields, so no two loops ever await each other:
        # asyncio recurses between two tasks that do.
        current = asyncio.current_task()
        loops = [
            task
            for task in (self._reader, self._pinger, self._idler)
            if task is not None and task is not current
        ]
        self._reader = None
        self._pinger = None
        self._idler = None

        for task in loops:
            task.cancel()

        for task in loops:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def register(self, session: str, turn: str) -> None:
        """Tell the link which turn to expect lines for (§13 rule 3)."""
        self._turns[(session, turn)] = _InFlight(session=session, turn=turn)
        self._touch()

    def unregister(self, session: str, turn: str) -> None:
        self._turns.pop((session, turn), None)
        self._violated.discard((session, turn))
        self._touch()

    def note_opened(self, session: str) -> None:
        """Record that this session may now hold a process on this channel."""
        self._opened.add(session)
        # A pre-start means a turn is coming (contract 03 §4.7 rule 8).
        self._touch()

    def opened_sessions(self) -> list[str]:
        """Every session that may hold a process here, oldest first."""
        return sorted(self._opened)

    def has_opened(self, session: str) -> bool:
        """True when this session may hold a process on this channel."""
        return session in self._opened

    def in_flight(self) -> list[tuple[str, str]]:
        return list(self._turns.keys())

    async def send(self, message: dict[str, Any]) -> None:
        """Write one host message. Raises ChannelClosed when there is none."""
        channel = self._channel

        if channel is None:
            raise ChannelClosed(f"family {self._family} has no channel")

        async with self._send_lock:
            await channel.send(encode(message))

    async def _open(self, dial: SandboxDial, epoch: int) -> None:
        channel = self._factory(dial)
        await channel.start()
        self._channel = channel
        self._dialled = dial.sandbox

        try:
            ready = await self._handshake(dial.sandbox, epoch)
        except BaseException:
            await channel.close()
            self._channel = None
            raise

        self._ready = ready
        self._refusals.reset()
        self._unanswered = 0
        self._touch()
        self._reader = asyncio.create_task(self._read_loop())
        self._pinger = asyncio.create_task(self._ping_loop())

        if self._idle_ttl_s > 0:
            self._idler = asyncio.create_task(self._idle_loop())

    async def _handshake(self, sandbox: str, epoch: int) -> Ready:
        """Contract 03 §3. The host sends nothing before `ready` arrives."""
        channel = self._channel

        if channel is None:
            raise HandshakeError("channel vanished before the handshake")

        raw = await channel.receive()

        if raw is None or raw.text is None:
            raise HandshakeError("no ready line")

        message = parse(raw.text)

        # §5.7 rule 3. The lock is taken before the first protocol line, so a
        # playpen that cannot write the control mount says so INSTEAD of
        # `ready`. Without this the host would see a bare `channel_lost`.
        if isinstance(message, FatalLine):
            self._report_fatal(message, sandbox)
            raise PlaypenFatal(f"playpen fatal: {message.reason.value}")

        if not isinstance(message, Ready):
            raise HandshakeError("first line was not ready")

        # A major mismatch is an image problem, not a restart problem. The fix
        # is a sandbox image release (§3, version rule 1).
        if message.major != PROTOCOL_MAJOR:
            self._faults.raise_fault(
                self._family,
                FaultCode.PROTOCOL_MISMATCH,
                f"playpen speaks protocol {message.protocol}",
                sandbox,
            )
            raise HandshakeError(f"protocol {message.protocol} against {PROTOCOL_MAJOR}.x")

        # A different sandbox id means a stale exec left over from a switch.
        if message.sandbox != sandbox:
            raise HandshakeError(f"ready names sandbox {message.sandbox}, dialled {sandbox}")

        # A completed handshake is evidence that this writer's last complaint
        # no longer holds, so every fault it raised clears before a fresh one
        # is raised. The file holds current open faults, not a history
        # (contract 05 §3.3.1).
        self._faults.clear_all(self._family)

        # Orphans are the expected case after a drop, not a surprise: a
        # host-side kill does not reach the in-VM process (probe 0a, A5).
        if message.foreign_pi_processes > 0:
            self._faults.raise_fault(
                self._family,
                FaultCode.ORPHAN_PROCESSES,
                f"ready reported foreign_pi_processes={message.foreign_pi_processes}",
                sandbox,
            )

        cap = min(message.max_resident_processes, wire.MAX_RESIDENT_PROCESSES)
        await channel.send(
            encode(
                wire.hello(
                    family=self._family,
                    sandbox=sandbox,
                    epoch=epoch,
                    pi_idle_ttl_s=pi_idle_ttl_s(self._kind),
                    max_resident_processes=cap,
                    channel_idle_ttl_s=channel_idle_ttl_s(self._kind),
                )
            )
        )
        return message

    async def _prove_no_orphan(self, sandbox: str) -> None:
        """Contract 03 §11.4 rule 4: prove the old playpen is gone.

        The lock file comes from inside the sandbox, so it is a claim. It can
        delay a new channel. It can never grant one.

        The proof is the counter. A playpen rewrites its lock every
        `lock_beat_s` with a beat one higher (§11.1 rule 3), so a counter this
        host watches stop moving for `lock_stale_s` means nobody is writing the
        file. Nothing else crosses the boundary: the pid is in the VM's pid
        namespace and the two clocks are not the same clock.

        The lock belongs to the sandbox being dialled, never to the family:
        during a switch the outgoing sandbox's playpen is alive and
        beating its own lock, and waiting that one out would refuse the
        handshake the switch asked for (contract 03 §7.1).
        """
        path = playpen_lock_file(self._state_root, self._family, sandbox)
        watch = _BeatWatch(self._lock_stale_s)
        ceiling = time.monotonic() + self._host_deadline_s

        while True:
            # A file the last playpen removed as it exited is the clean
            # case, and the common one. Dial at once.
            if not path.exists():
                return

            if watch.observe(_lock_beat(path)):
                self._clear_stale_lock(path)
                return

            # A counter that keeps moving is a live playpen. Its own
            # deadline ends it within `host_deadline_s` (§11.1 rule 1), so
            # past that the sandbox is `managerd`'s to replace, not this
            # service's to dial (§11.4 rule 6).
            if time.monotonic() >= ceiling:
                self._faults.raise_fault(
                    self._family,
                    FaultCode.ORPHAN_PROCESSES,
                    "supervisor.lock is still being written by a live playpen",
                    sandbox,
                )
                raise OrphanPlaypen(f"sandbox {sandbox} may still hold a playpen")

            await asyncio.sleep(self._lock_poll_s)

    def _clear_stale_lock(self, path: Path) -> None:
        """Remove a lock nobody is writing (§11.4 rule 4, step 2).

        A failure here is not worth a turn. The playpen that left the file
        is gone either way, and the next dial waits the same window again.
        """
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)

    async def _read_loop(self) -> None:
        channel = self._channel

        if channel is None:
            return

        while True:
            try:
                raw = await channel.receive()
            except ChannelClosed:
                raw = None

            if raw is None:
                await self._lost()
                return

            if raw.refusal is not None:
                if await self._refuse(raw.refusal):
                    return

                continue

            if raw.text is None:
                continue

            message = parse(raw.text)

            if isinstance(message, Refusal):
                if await self._refuse(message):
                    return

                continue

            await self._dispatch(message)

    async def _ping_loop(self) -> None:
        """Contract 03 §10 rule 4. Two missed answers mean the channel is gone."""
        while True:
            await asyncio.sleep(self._ping_interval_s)

            if self._unanswered >= MISSED_PONGS_ALLOWED:
                await self._lost()
                return

            self._unanswered += 1

            try:
                await self.send(wire.ping(secrets.token_hex(_NONCE_BYTES)))
            except (ChannelClosed, ValueError):
                await self._lost()
                return

    async def _idle_loop(self) -> None:
        """Contract 03 §10 rule 3. Close the channel once nothing needs it.

        The VM auto-stops about 30 seconds after its last `sbx exec` ends
        (§10), so an open channel is what holds a thin or autonomous family's
        memory between jobs (`spec.md` §3.4). The next turn dials again and
        pays the cold start (§10 rule 5).
        """
        while True:
            await asyncio.sleep(self._idle_left())

            if await self._close_if_idle():
                return

    def _idle_left(self) -> float:
        """Seconds until the idle window has passed. A whole window while busy.

        Busy is a turn in flight, a turn queued for a slot, or a pi process
        the playpen still holds.

        CONTRACT-QUESTION: contract 03 §10 rule 3 names a running turn and a
        resident process, not a queued turn. A queued turn is about to start
        here, so it holds the channel too: the stricter reading. It costs
        nothing in practice, because a family queues only while its turns
        run (contract 02 §13 rule 3).
        """
        if self._turns or self._resident or self._queued() > 0:
            return self._idle_ttl_s

        return max(self._active_at + self._idle_ttl_s - time.monotonic(), 0.0)

    async def _close_if_idle(self) -> bool:
        """Shut the playpen down, wait for it to go, then drop the channel.

        Under the dial lock, so a turn that arrives meanwhile waits and then
        dials a fresh channel rather than writing to this one.

        `shutdown` first, and its exit awaited: a killed `sbx exec` does not
        reach the in-VM process (probe 0a, A5). A playpen left beating its
        lock would hold the VM up and make the next dial wait it out
        (§11.4 rule 4).
        """
        async with self._dial_lock:
            channel = self._channel

            if channel is None or not channel.alive or self._idle_left() > 0:
                return False

            _LOG.info("family=%s sandbox=%s channel idle, closing", self._family, channel.sandbox)
            await self._stop_loops()

            with contextlib.suppress(ChannelClosed, ValueError):
                await self.send(wire.shutdown())

            await _await_exit(channel, self._exit_wait_s)
            await self.close()
            return True

    def _touch(self) -> None:
        """Restart the idle window: this channel just had work."""
        self._active_at = time.monotonic()

    def _track(self, message: OpenedLine | SettledLine | ProcessExitLine) -> None:
        """Whether the playpen still holds this session's pi process.

        A claim can only delay a close. One for a session this host never
        opened on this channel holds nothing, so naming sessions at random
        cannot keep the VM up.
        """
        self._touch()
        held = isinstance(message, (OpenedLine, SettledLine)) and message.resident

        if held and message.session in self._opened:
            self._resident.add(message.session)
            return

        self._resident.discard(message.session)

    async def _refuse(self, refusal: Refusal) -> bool:
        """Count one refused line. True when the channel must close."""
        if not self._refusals.count():
            return False

        self._faults.raise_fault(
            self._family,
            FaultCode.PROTOCOL_VIOLATION,
            f"{self._refusals.spent} refused lines inside the window ({refusal.value})",
            self.sandbox,
        )
        await self._lost()
        return True

    async def _lost(self) -> None:
        """The channel dropped. Sessions survive, turns in flight do not.

        The sandbox is read before the close, because the service fails the
        turns of THIS sandbox and a draining family has another link open.
        """
        sandbox = self._dialled
        await self.close()

        if sandbox is None:
            return

        await self._events.on_channel_lost(sandbox)

    def _report_fatal(self, message: FatalLine, sandbox: str | None) -> None:
        """Contract 03 §5.7 rule 4. The cause reaches the status document.

        The playpen never reached `ready`, and contract 05 §4.2 defines
        `ready` as the handshake this service runs, so this service is the one
        that can report it. Dialling again would not fix a mount.
        """
        self._faults.raise_fault(
            self._family,
            FaultCode.SANDBOX_START_FAILED,
            f"playpen fatal: {message.reason.value}: {message.message}",
            sandbox,
        )

    async def _dispatch(self, message: object) -> None:
        if isinstance(message, PongLine):
            self._unanswered = 0
            return

        if isinstance(message, LogLine):
            await self._events.on_log(message)
            return

        if isinstance(message, ProcessExitLine):
            self._track(message)
            await self._events.on_process_exit(message)
            return

        if isinstance(message, OpenedLine):
            # §5.6. Nothing is acted on. It is known so that ten pre-starts
            # cannot spend the refusal budget on an unknown type. Its
            # `resident` only keeps the channel open (§10 rule 3).
            self._track(message)
            await self._events.on_session_opened(message)
            return

        if isinstance(message, EntriesLine):
            # §5.8. It names no turn, so §13 rule 3's addressing check does
            # not apply. The service drops a `request` it never sent.
            await self._events.on_entries(message)
            return

        if isinstance(message, FatalLine):
            # §5.7 rule 2: a last line, never a state. The exec ends behind it.
            self._report_fatal(message, self.sandbox)
            await self._lost()
            return

        if isinstance(message, Ready):
            # A second `ready` on a live channel is not addressed to anything.
            await self._refuse(Refusal.UNKNOWN_TYPE)
            return

        await self._dispatch_turn(message)

    async def _dispatch_turn(self, message: object) -> None:
        if not isinstance(message, (EventLine, SettledLine, FailedLine)):
            return

        key = (message.session, message.turn)

        # A turn the host already failed keeps arriving for a while. Dropping
        # those lines silently is deliberate: counting them would spend the
        # refusal budget and degrade a whole family over one bad turn.
        if key in self._violated:
            return

        flight = self._turns.get(key)

        if flight is None:
            await self._refuse(Refusal.UNKNOWN_ADDRESS)
            return

        expected = flight.last_seq + 1

        if message.turn_seq < expected:
            return

        if message.turn_seq > expected:
            self._violated.add(key)
            self._turns.pop(key, None)
            await self._refuse(Refusal.SEQUENCE_GAP)
            await self._events.on_violation(message.session, message.turn, Violation.SEQUENCE_GAP)
            return

        flight.last_seq = message.turn_seq
        await self._deliver(message)

    async def _deliver(self, message: EventLine | SettledLine | FailedLine) -> None:
        if isinstance(message, EventLine):
            await self._events.on_event(message.session, message.turn, message.event)
            return

        if isinstance(message, SettledLine):
            self._track(message)
            await self._events.on_settled(message)
            return

        await self._events.on_failed(message)


async def _await_exit(channel: Channel, wait_s: float) -> None:
    """Drop what the channel still says until its stream ends, or time runs out.

    Nothing is in flight on an idle channel, so no line read here is owed to
    anyone.
    """
    with contextlib.suppress(TimeoutError, ChannelClosed):
        async with asyncio.timeout(wait_s):
            while await channel.receive() is not None:
                continue
