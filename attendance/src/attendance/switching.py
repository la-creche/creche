"""The bookkeeping behind one sandbox switch (contract 05 §5).

`managerd` makes the call. This module holds the two facts the call needs and
the service cannot keep anywhere else:

1. Which sandbox a family's new turns go to, from the instant the call is
   accepted (§5.3 rule 1).
2. What a repeat of the same call must answer (§5.3 rule 7).

Both live in memory on purpose. A switch that a crash interrupts converges on
the status document, which is the truth about which sandbox serves: `managerd`
has by then published the incoming sandbox as the one to dial, and this
service reads that document on every turn.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

# One entry per (family, from, to) the process has ever answered. A switch is
# a sandbox replacement, so the real rate is a handful a day; the cap is here
# because a table with no bound is a leak whatever the rate.
MAX_REMEMBERED = 256


class SwitchOutcome(StrEnum):
    """Contract 05 §5.2's three outcomes. They describe the OLD sandbox."""

    DRAINED = "drained"
    INTERRUPTED = "interrupted"
    DEADLINE_HIT = "deadline_hit"


@dataclass(slots=True)
class SwitchTally:
    """What one switch did, counted as it happened (contract 05 §5.2)."""

    running_at_start: int = 0
    finished: int = 0
    aborted: int = 0
    sessions: int = 0

    def to_api(self, outcome: SwitchOutcome) -> dict[str, Any]:
        """`switched` is always true: a body exists only once it was accepted."""
        return {
            "switched": True,
            "outcome": outcome.value,
            "turns_running_at_start": self.running_at_start,
            "turns_finished": self.finished,
            "turns_aborted": self.aborted,
            "sessions": self.sessions,
        }


SwitchKey = tuple[str, str, str]
SwitchRun = asyncio.Task[dict[str, Any]]


class SwitchBook:
    """Every switch this process has run, and the sandbox each family now uses."""

    def __init__(self, max_remembered: int = MAX_REMEMBERED) -> None:
        self._max = max_remembered
        self._runs: dict[SwitchKey, SwitchRun] = {}
        self._pinned: dict[str, str] = {}

    def pinned(self, family: str) -> str | None:
        """The sandbox this family's new turns must use, or None."""
        return self._pinned.get(family)

    def pin(self, family: str, sandbox: str) -> None:
        """Contract 05 §5.3 rule 1. Every new turn goes to `to` from here."""
        self._pinned[family] = sandbox

    def unpin(self, family: str) -> None:
        """Drop a pin the status document no longer offers.

        The document outranks the pin. After a teardown, a crash or a second
        replacement, the pinned sandbox can be gone, and following it then
        would refuse every turn of a family that has a working sandbox.
        """
        self._pinned.pop(family, None)

    def find(self, key: SwitchKey) -> SwitchRun | None:
        """The run this exact call already started, or None."""
        return self._runs.get(key)

    def remember(self, key: SwitchKey, run: SwitchRun) -> None:
        self._evict()
        self._runs[key] = run

    def forget(self, key: SwitchKey) -> None:
        """Drop a run that did not switch.

        §5.3 rule 7 makes a completed switch idempotent: a repeat answers
        the same and does nothing. A refused one has no outcome to repeat,
        and `managerd` retries the same call on its next pass. Keeping the
        failed run would answer every one of those retries with the first
        refusal, so the family could never converge.
        """
        self._runs.pop(key, None)

    def cancel_all(self) -> None:
        """Drop every run in flight, for `SessionService.close()`.

        Nothing about a switch is on disk. A restart reads the status document
        and converges on it, so a drain is dropped exactly as a pre-start is.
        A task left pending outlives the service it holds a reference to.
        """
        for run in self._runs.values():
            if not run.done():
                run.cancel()

        self._runs.clear()
        self._pinned.clear()

    def _evict(self) -> None:
        """Forget the oldest FINISHED runs once the table is full.

        A run still in flight is never evicted: a retry of it must join the
        run it names rather than start a second teardown of one sandbox.
        """
        if len(self._runs) < self._max:
            return

        for key, run in list(self._runs.items()):
            if len(self._runs) < self._max:
                return

            if run.done():
                del self._runs[key]


def switch_key(family: str, outgoing: str | None, to: str) -> SwitchKey:
    """§5.3 rule 7's identity: the family, the outgoing id and the incoming one."""
    return (family, outgoing if outgoing is not None else "", to)
