"""A `AttendanceClient` test double, scoped to what this door calls: one
`POST /trigger`. Not a wire-level fake: `test_trigger_fire.py`'s own
HTTP-transport tests cover `HttpAttendance`.

Named `trigger_fake_attendance.py`, not `fake_attendance.py`: that basename is
already `door-owui/tests/fake_attendance.py`, and this workspace's rootless
pytest import mode needs a unique module basename repo-wide
(`door-owui/AGENTS.md`).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agent_door_trigger.attendance import EnqueuedJob, JobStatus, TriggerJob
from agent_door_trigger.errors import AttendanceError
from agent_door_trigger.ulid import new_ulid


@dataclass
class FakeAttendance:
    """One script, played back to whichever job arrives next.

    Settable before a call:
      `status` -- the status each answer carries.
      `error`  -- a AttendanceError raised by the next `enqueue` call, then
                  cleared.

    Recorded for assertions:
      `jobs` -- every `TriggerJob` an `enqueue` call saw, in order.
    """

    status: JobStatus = JobStatus.QUEUED
    error: AttendanceError | None = None

    jobs: list[TriggerJob] = field(default_factory=list[TriggerJob])

    def enqueue(self, job: TriggerJob) -> EnqueuedJob:
        self.jobs.append(job)
        if self.error is not None:
            error, self.error = self.error, None
            raise error

        return EnqueuedJob(session=f"auto-{new_ulid()}", status=self.status, created=True)
