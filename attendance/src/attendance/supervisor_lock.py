"""The proof that no older supervisor holds a sandbox (contract 03 §11.4).

The process inside the sandbox rewrites `supervisor.lock` with a counter one
higher on each beat. The file comes from inside the sandbox, so it is a
claim. It can delay a dial. It can never grant one.

The ACP link reads the lock with this module.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path

from .atomic import read_json

# Contract 03 §11.4 rule 4. The supervisor rewrites `supervisor.lock` every
# `lock_beat_s` (5 by default) with a counter one higher. A counter that has
# not moved for this long means nothing is writing the file, so the host may
# remove it and dial. Four beat windows, so one missed rewrite never
# unlocks a live supervisor.
LOCK_STALE_S = 20.0
LOCK_POLL_S = 1.0


class BeatWatch:
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


def lock_beat(path: Path) -> int | None:
    """The counter in the lock file, or None when it could not be read."""
    raw = read_json(path)

    if raw is None:
        return None

    beat = raw.get("beat")

    if isinstance(beat, bool) or not isinstance(beat, int) or beat < 0:
        return None

    return beat


async def wait_out_lock(path: Path, stale_s: float, poll_s: float, deadline_s: float) -> bool:
    """Wait until nobody writes the lock. False when a writer outlives the deadline.

    A file that the last supervisor removed as it exited is the clean case,
    and the common one. A counter that stops for `stale_s` belongs to a
    supervisor that is gone: the file is removed (§11.4 rule 4, step 2). A
    counter that keeps moving past `deadline_s` is a live supervisor, and the
    caller raises the fault.
    """
    watch = BeatWatch(stale_s)
    ceiling = time.monotonic() + deadline_s

    while True:
        if not path.exists():
            return True

        if watch.observe(lock_beat(path)):
            # A failure here is not worth a turn. The supervisor that left
            # the file is gone either way, and the next dial waits again.
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)

            return True

        if time.monotonic() >= ceiling:
            return False

        await asyncio.sleep(poll_s)
