"""The three handlers that give the writer lease up.

SIGTERM and SIGHUP end a Python process without running a `finally` block,
so `finally` alone would leave the lease held for the whole 60-second TTL
every time a terminal window is closed.
"""

from __future__ import annotations

import signal
import threading

import pytest
from agent_door_tui.signals import WATCHED, Interrupted, SignalGuard


def test_the_guard_watches_three_signals() -> None:
    assert set(WATCHED) == {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}


def test_the_guard_installs_and_restores() -> None:
    before = {number: signal.getsignal(number) for number in WATCHED}
    seen: list[int] = []

    with SignalGuard(seen.append):
        assert all(signal.getsignal(number) is not before[number] for number in WATCHED)

    assert all(signal.getsignal(number) is before[number] for number in WATCHED)


@pytest.mark.parametrize("signum", list(WATCHED))
def test_a_real_signal_reaches_the_callback(signum: int) -> None:
    import os

    seen: list[int] = []

    with SignalGuard(seen.append):
        os.kill(os.getpid(), signum)

    assert seen == [signum]


def test_the_signal_is_recorded() -> None:
    guard = SignalGuard(lambda _signum: None)

    guard.handle(signal.SIGHUP)

    assert guard.caught == signal.SIGHUP


def test_a_callback_that_raises_reaches_the_caller() -> None:
    def refuse(signum: int) -> None:
        raise Interrupted(signum)

    with pytest.raises(Interrupted):
        SignalGuard(refuse).handle(signal.SIGINT)


def test_installing_off_the_main_thread_does_not_raise() -> None:
    """`signal.signal` works in the main thread only.

    The door always runs there. A harness that drives it from a worker
    thread must still get a terminal, not a `ValueError` from the standard
    library — the lease is released by `TuiDoor.run`'s own `finally` either
    way, so the guard is an extra, never the mechanism.
    """
    failure: list[BaseException] = []

    def install_and_restore() -> None:
        try:
            with SignalGuard(lambda _signum: None) as guard:
                assert not guard.installed
        except BaseException as error:
            failure.append(error)

    worker = threading.Thread(target=install_and_restore)
    worker.start()
    worker.join()

    assert failure == []


def test_the_main_thread_still_installs() -> None:
    with SignalGuard(lambda _signum: None) as guard:
        assert guard.installed
