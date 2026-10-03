"""The writer lease this terminal holds while pi runs (contract 02 §7).

A session has at most one writer and any number of readers. `attendance`
enforces that, because pi enforces nothing: two concurrent pi writers on one
session store cross-contaminate context, orphan a branch and both report
success with no error anywhere (contract 02 §7, measured).

So the door takes the lease BEFORE pi starts, renews it while the terminal
runs, and gives it up on the way out.

Contention refuses. The door never sets `force`, never queues and never
steals. A `session_busy` becomes one line naming the holder and a non-zero
exit.

Contract 02 §5.10 releases the lease, so `release()` is one call and the
session is writable the moment the terminal closes, not 60 seconds later.

Contract 02 §7.4 fixes no renewal interval, and says a third of the
60-second TTL leaves room for two lost calls. This door renews every 20
seconds, so two lost renewals still land inside the TTL.
"""

from __future__ import annotations

import logging
import threading

from .attendance import (
    CODE_LEASE_TAKEN_OVER,
    AttendanceClient,
    AttendanceError,
    Intent,
    Takeover,
    raise_refusal,
)

_LOG = logging.getLogger(__name__)

#: Contract 02 §7.1's TTL. The lease expires this long after its last renewal.
LEASE_TTL_S = 60

#: A third of the TTL: two lost renewals still land inside it.
RENEW_INTERVAL_S = 20

#: How long `release` waits for the renewal thread. It only ever sleeps.
_JOIN_TIMEOUT_S = 5.0


class WriterLease:
    """One session's writer lease, held for the life of one terminal."""

    def __init__(
        self,
        door: AttendanceClient,
        family: str,
        session: str,
        door_instance: str,
    ) -> None:
        self._door = door
        self._family = family
        self._session = session
        self._instance = door_instance
        self._held = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.renewal_failures = 0

    @property
    def held(self) -> bool:
        """True once `attendance` granted the lease and before it was released."""
        return self._held

    @property
    def renewing(self) -> bool:
        """True while the renewal thread runs."""
        thread = self._thread

        return thread is not None and thread.is_alive()

    def take(self, takeover: Takeover = Takeover.POLITE) -> None:
        """Take the lease, or refuse with the holder named (contract 02 §7.2).

        `POLITE` is the default and the only value the door picks on its own.
        `FORCE` comes from the operator's own `--force`, and `attendance` still refuses
        it while a turn is in flight (contract 02 §7.3 rule 4).
        """
        try:
            self._door.take_writer(self._family, self._session, self._instance, takeover)
        except AttendanceError as error:
            raise raise_refusal(error) from error

        self._held = True

    def start_renewal(self) -> None:
        """Renew in the background while the terminal owns the foreground.

        The thread is a daemon so a door that dies hard never wedges the
        shell, and `release` joins it so the thread outlives nothing.
        """
        if not self._held or self._thread is not None:
            return

        self._stop.clear()
        self._thread = threading.Thread(target=self._renew_loop, name="tui-lease", daemon=True)
        self._thread.start()

    def renew_once(self) -> None:
        """One renewal. A failure is counted and logged, never raised.

        A human is typing into pi. Ending the terminal because `attendance`
        missed one call would lose that work for nothing: the lease has 60
        seconds of TTL and the next renewal may well succeed.

        It renews with `intent: renew` (contract 02 §7.4), so it can never
        take a lease back. `lease_taken_over` is the one answer that ends the
        renewals: the session belongs to another door now, and asking again
        every 20 seconds would fight a human.
        """
        if not self._held:
            return

        try:
            self._door.take_writer(
                self._family, self._session, self._instance, Takeover.POLITE, Intent.RENEW
            )
        except AttendanceError as error:
            self._after_failed_renewal(error)

    def release(self) -> None:
        """Stop renewing and hand the lease back (contract 02 §5.10).

        Idempotent, and a refusal here is logged rather than raised: this
        runs on the way out, often from a signal handler, and the TTL still
        ends a lease `attendance` would not take back.
        """
        self._stop.set()
        thread = self._thread
        self._thread = None

        if thread is not None and thread.is_alive():
            thread.join(_JOIN_TIMEOUT_S)

        if self._held:
            self._give_back()

        self._held = False

    def release_line(self) -> str:
        """What the door prints when it gives the session up."""
        return (
            f"The writer lease on {self._family}/{self._session} is released. "
            "Open WebUI can write to it now."
        )

    def _give_back(self) -> None:
        try:
            self._door.release_writer(self._family, self._session, self._instance)
        except AttendanceError as error:
            _LOG.warning("the writer lease was not released: %s", error.code)

    def _after_failed_renewal(self, error: AttendanceError) -> None:
        self.renewal_failures += 1

        if error.code == CODE_LEASE_TAKEN_OVER:
            # The session moved to another door while this terminal idled.
            # Nothing here may take it back (§7.4), so the renewals stop and
            # the release on the way out has nothing left to give back.
            self._held = False
            self._stop.set()
            _LOG.warning("another door took this session: %s", self._session)
            return

        _LOG.warning("the writer lease was not renewed: %s", error.code)

    def _renew_loop(self) -> None:
        while not self._stop.wait(RENEW_INTERVAL_S):
            self.renew_once()
