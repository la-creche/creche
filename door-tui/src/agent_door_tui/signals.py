"""Give the writer lease up on SIGINT, SIGTERM and SIGHUP.

SIGTERM and SIGHUP end a Python process without running a `finally` block, so
a door that relied on `finally` alone would leave its lease held for the full
60-second TTL every time a terminal was closed with the window rather than
with a quit.

While pi runs it owns the terminal, and the terminal driver already delivered
the same signal to it. The door therefore does NOT stop pi: it releases the
lease, lets pi finish, and still reports pi's own exit code. Killing pi here
would throw away whatever the person was in the middle of.
"""

from __future__ import annotations

import signal
import threading
from collections.abc import Callable
from types import FrameType

#: The three signals that give the lease up.
WATCHED = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


class Interrupted(Exception):
    """A watched signal arrived while no child owned the terminal."""

    def __init__(self, signum: int) -> None:
        super().__init__(f"interrupted by {signal.Signals(signum).name}")
        self.signum = signum


class SignalGuard:
    """Installs the three handlers, and puts the old ones back on exit."""

    def __init__(self, on_signal: Callable[[int], None]) -> None:
        self._on_signal = on_signal
        self._previous: dict[int, object] = {}
        self.caught: int | None = None
        self.installed = False

    def __enter__(self) -> SignalGuard:
        self.install()

        return self

    def __exit__(self, *_: object) -> None:
        self.restore()

    def install(self) -> None:
        """Install the three handlers, when this thread may have them.

        `signal.signal` works in the main thread of the main interpreter and
        nowhere else. The door always runs there. A harness that drives it
        from a worker thread still gets a terminal, because the lease is
        released by `TuiDoor.run`'s own `finally` either way: this guard is
        what covers SIGTERM and SIGHUP, which run no `finally` at all.
        """
        if not _is_main_thread():
            return

        for number in WATCHED:
            self._previous[number] = signal.getsignal(number)
            signal.signal(number, self.handle)

        self.installed = True

    def restore(self) -> None:
        for number, previous in self._previous.items():
            # `getsignal` returns exactly what `signal` accepts back.
            signal.signal(number, previous)  # pyright: ignore[reportArgumentType]

        self._previous.clear()
        self.installed = False

    def handle(self, signum: int, _frame: FrameType | None = None) -> None:
        """Record the signal and tell the door. Never kills the child."""
        self.caught = signum
        self._on_signal(signum)


def _is_main_thread() -> bool:
    return threading.current_thread() is threading.main_thread()
