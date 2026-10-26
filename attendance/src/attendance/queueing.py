"""The turn limit and the queue, autonomous families only (contract 02 §13).

Only an autonomous family has either. An attended family is a human waiting
at a keyboard, and a thin job is a call already blocking at the PEP: making
either one wait behind a timer's backlog would be a worse answer than running
it (`docs/rework/spec.md` §7.4). So `slot_for` says RUN for both, whatever
the family's published limits claim.

```
  run turn --> slot_for --> RUN   --> pick a sandbox, start_turn
                       \
                        `-> QUEUE --> turn_queued on disk, then the FIFO
                                          |
                    a running turn ends --'  the head starts
```

The queue lives in memory. A restart therefore ends every turn waiting in it
(§13.3), which `attendance` records as `queue_lost` rather than resuming three
hours of stale firings.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from .branching import Decision
from .errors import ApiError, ErrorCode
from .persona import Persona
from .requests import RunTurnRequest
from .states import SessionKind
from .turns import LiveTurn

# Contract 02 §13 rule 4. `attendance`'s own constant, not a family file field.
# A runaway timer must not grow an unbounded queue.
MAX_QUEUED_TURNS = 100


class Slot(Enum):
    """Whether a new turn runs now or waits (contract 02 §13 rules 2 and 3)."""

    RUN = "run"
    QUEUE = "queue"


@dataclass(slots=True)
class Waiting:
    """One turn whose prompt is on disk and whose slot has not come.

    It carries the request and the persona because starting it later needs
    exactly what starting it now would have needed, and nothing re-reads a
    door's request once the door has its `202`.
    """

    live: LiveTurn
    request: RunTurnRequest
    persona: Persona
    branch: Decision


def slot_for(kind: SessionKind, max_running: int | None, running: int) -> Slot:
    """Contract 02 §13 rules 2, 3 and 5. Autonomous families only."""
    if kind is not SessionKind.AUTONOMOUS:
        return Slot.RUN

    if max_running is None or running < max_running:
        return Slot.RUN

    return Slot.QUEUE


class TurnQueue:
    """One FIFO per family, in memory (contract 02 §13.3)."""

    def __init__(self, max_queued: int = MAX_QUEUED_TURNS) -> None:
        self._max_queued = max_queued
        self._waiting: dict[str, deque[Waiting]] = {}

    @property
    def max_queued(self) -> int:
        return self._max_queued

    def depth(self, family: str) -> int:
        return len(self._waiting.get(family, ()))

    def check_room(self, family: str) -> None:
        """Raise `queue_full` before anything is written (§13 rule 4).

        It runs before the turn is minted, so a refused turn leaves no
        record, no journal line and no entry to clean up.
        """
        if self.depth(family) < self._max_queued:
            return

        raise ApiError(
            ErrorCode.QUEUE_FULL,
            f"family {family} already has {self._max_queued} queued turns",
            family=family,
            detail={"max_queued_turns": self._max_queued},
        )

    def add(self, family: str, waiting: Waiting) -> None:
        """Put one turn at the back. `check_room` decides whether it fits."""
        self._waiting.setdefault(family, deque()).append(waiting)

    def take(self, family: str) -> Waiting | None:
        """The head, or None when nothing waits."""
        queue = self._waiting.get(family)

        if not queue:
            return None

        return queue.popleft()

    def drop(self, family: str, turn: str) -> None:
        """Forget one turn. A stopped or deleted turn never reaches a slot."""
        self._keep(family, lambda one: one.live.record.turn != turn)

    def drop_session(self, family: str, session: str) -> None:
        """Forget one session's waiting turns. A delete calls this."""
        self._keep(family, lambda one: one.live.record.session != session)

    def _keep(self, family: str, wanted: Callable[[Waiting], bool]) -> None:
        queue = self._waiting.get(family)

        if not queue:
            return

        self._waiting[family] = deque(one for one in queue if wanted(one))

    def families(self) -> list[str]:
        """Every family with something waiting."""
        return [family for family, queue in self._waiting.items() if queue]


class JobQueue[JobT]:
    """The dispatch door's limit and FIFO, per family (contract 02 §13).

    A job over ACP has no session in this service, so it has its own book:
    how many run, which wait, and the live status of each by its id. Until
    the session API goes (phase 2 plan, row 21), a family's door-trigger
    turns and its dispatched jobs each count only themselves.
    """

    def __init__(self, max_queued: int = MAX_QUEUED_TURNS) -> None:
        self._max_queued = max_queued
        self._running: dict[str, set[str]] = {}
        self._waiting: dict[str, deque[tuple[str, JobT]]] = {}

    def depth(self, family: str) -> int:
        return len(self._waiting.get(family, ()))

    def running(self, family: str) -> int:
        return len(self._running.get(family, ()))

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
        if slot_for(SessionKind.AUTONOMOUS, max_running, self.running(family)) is Slot.QUEUE:
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
