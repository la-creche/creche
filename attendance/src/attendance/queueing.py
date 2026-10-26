"""The job limit and the queue of the dispatch door (contract 02 §13).

Only an autonomous family has either, and only the dispatch door reaches an
autonomous family. A job runs at once while the family has a free slot.
Otherwise it waits in the family's FIFO, and the head starts when a running
job ends.

The queue lives in memory. A restart therefore forgets every job waiting in
it, and the ledger entry of such a job reads `ended` with a null outcome.
"""

from __future__ import annotations

from collections import deque

from .errors import ApiError, ErrorCode

# Contract 02 §13 rule 4. `attendance`'s own constant, not a family file field.
# A runaway caller must not grow an unbounded queue.
MAX_QUEUED_TURNS = 100


class JobQueue[JobT]:
    """The dispatch door's limit and FIFO, per family (contract 02 §13).

    The book says how many jobs run, which wait, and the live status of
    each by its id.
    """

    def __init__(self, max_queued: int = MAX_QUEUED_TURNS) -> None:
        self._max_queued = max_queued
        self._running: dict[str, set[str]] = {}
        self._waiting: dict[str, deque[tuple[str, JobT]]] = {}

    def depth(self, family: str) -> int:
        return len(self._waiting.get(family, ()))

    def running(self, family: str) -> int:
        return len(self._running.get(family, ()))

    def running_total(self) -> int:
        """How many jobs run, over every family."""
        return sum(len(jobs) for jobs in self._running.values())

    def waiting_total(self) -> int:
        """How many jobs wait for a slot, over every family."""
        return sum(len(queue) for queue in self._waiting.values())

    def is_running(self, job: str) -> bool:
        return any(job in jobs for jobs in self._running.values())

    def is_waiting(self, job: str) -> bool:
        return any(job == one for queue in self._waiting.values() for one, _ in queue)

    def check_room(self, family: str) -> None:
        """Raise `queue_full` before anything is written (§13 rule 4)."""
        if self.depth(family) < self._max_queued:
            return

        raise ApiError(
            ErrorCode.QUEUE_FULL,
            f"family {family} already has {self._max_queued} queued turns",
            family=family,
            detail={"max_queued_turns": self._max_queued},
        )

    def take_slot(self, family: str, job: str, max_running: int | None) -> bool:
        """Mark `job` running when the family has a free slot (§13 rule 2)."""
        if max_running is not None and self.running(family) >= max_running:
            return False

        self._running.setdefault(family, set()).add(job)
        return True

    def add(self, family: str, job: str, item: JobT) -> None:
        """Put one job at the back. `check_room` decides whether it fits."""
        self._waiting.setdefault(family, deque()).append((job, item))

    def head(self, family: str) -> tuple[str, JobT] | None:
        queue = self._waiting.get(family)
        return queue[0] if queue else None

    def pop(self, family: str) -> None:
        queue = self._waiting.get(family)

        if queue:
            queue.popleft()

    def release(self, family: str, job: str) -> None:
        """A running job ended. Its slot is free."""
        self._running.get(family, set()).discard(job)

    def families(self) -> list[str]:
        """Every family with something waiting."""
        return [family for family, queue in self._waiting.items() if queue]
