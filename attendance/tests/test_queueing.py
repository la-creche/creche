"""The dispatch door's limit and FIFO (contract 02 §13), and the place a job
keeps when it meets `host_full` (contract 02 §6 rule 4, decision CH21-3)."""

from __future__ import annotations

from attendance.queueing import JobQueue

FAMILY = "issue-worker"


def _order(queue: JobQueue[str]) -> list[str]:
    taken: list[str] = []

    while (head := queue.head(FAMILY)) is not None:
        taken.append(head[0])
        queue.pop(FAMILY)

    return taken


def test_a_requeued_job_waits_in_its_place() -> None:
    """Jobs that meet `host_full` in any order go back in their order of
    arrival, in front of each job that arrived after them."""
    queue: JobQueue[str] = JobQueue()

    for job in ("a", "b"):
        assert queue.take_slot(FAMILY, job, max_running=2)

    queue.add(FAMILY, "c", "c")
    queue.requeue(FAMILY, "b", "b")
    queue.requeue(FAMILY, "a", "a")

    assert queue.running(FAMILY) == 0
    assert _order(queue) == ["a", "b", "c"]


def test_a_requeue_passes_the_cap() -> None:
    """`check_room` counted the job when it arrived (§13 rule 4)."""
    queue: JobQueue[str] = JobQueue(max_queued=1)
    assert queue.take_slot(FAMILY, "a", max_running=1)
    queue.add(FAMILY, "b", "b")
    queue.requeue(FAMILY, "a", "a")

    assert queue.depth(FAMILY) == 2
    assert _order(queue) == ["a", "b"]
