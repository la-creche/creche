"""The client of `attendance`'s trigger door, as `door-trigger` uses it.

`POST /trigger` enqueues one job in an autonomous family and answers at
once: the job's `auto-<ulid>` id and its status. `POST /trigger/jobs` reads
the jobs of one family back, with the outcome record of each job that
ended. This door never waits for a job, and never retries a refusal in a
loop.

Synchronous on purpose. `fire` is a one-shot CLI process with no event
loop to share, and `serve`'s route handler offloads this blocking client
to a thread (`webhooks.py`), so one client implementation serves both.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

import httpx

from .config import AttendanceTarget
from .errors import AttendanceError
from .untrusted import as_object, field_text, is_list

_LOG = logging.getLogger(__name__)

_OK = 200

CONNECT_TIMEOUT_S = 5.0
# Each call is fast and host-local: neither waits on a sandbox or a model
# reply. `attendance` answers a job as soon as it is queued or started.
READ_TIMEOUT_S = 15.0

#: The most jobs one read asks for: `attendance`'s own cap of a page.
JOBS_LIMIT = 200


class TriggerKind(StrEnum):
    """Contract 02 §13.2's `trigger.kind`. A cron firing is a `timer`. Only a
    webhook firing carries a `name` (contract 01 §3.13's two trigger forms)."""

    TIMER = "timer"
    WEBHOOK = "webhook"


class JobStatus(StrEnum):
    """The status of a job, as `attendance` answers it."""

    QUEUED = "queued"
    RUNNING = "running"
    ENDED = "ended"


#: A job of these statuses is still live.
LIVE_STATUSES = frozenset({JobStatus.QUEUED, JobStatus.RUNNING})


@dataclass(frozen=True)
class TriggerJob:
    """The body of one `POST /trigger`."""

    family: str
    kind: TriggerKind
    name: str | None
    message: str
    idempotency_key: str

    def body(self) -> dict[str, object]:
        return {
            "family": self.family,
            "kind": self.kind.value,
            "name": self.name,
            "message": self.message,
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True)
class EnqueuedJob:
    """`attendance`'s answer to one `POST /trigger`."""

    session: str
    status: JobStatus
    #: False when the idempotency key found an earlier job.
    created: bool


class AttendanceClient(Protocol):
    """What a firing needs from `attendance` (`fire.py`), and nothing more."""

    def enqueue(self, job: TriggerJob) -> EnqueuedJob:
        """Enqueue one job and return at once."""
        ...


class HttpAttendance:
    """`AttendanceClient` over HTTP, on a Unix socket or the LAN address."""

    def __init__(self, target: AttendanceTarget, client: httpx.Client | None = None) -> None:
        self._client = client if client is not None else _build_client(target)
        self._auth = {"Authorization": f"Bearer {target.token}"}

    def close(self) -> None:
        self._client.close()

    def enqueue(self, job: TriggerJob) -> EnqueuedJob:
        response = self._post("/trigger", job.body())

        if response.status_code != _OK:
            raise _error_of(response.status_code, response.text)

        return _enqueued_of(response.text)

    def any_live(self, family: str) -> bool | None:
        """Whether a job this door started in `family` is queued or running.
        None when `attendance` would not say."""
        response = self._post("/trigger/jobs", {"family": family, "limit": JOBS_LIMIT})

        if response.status_code != _OK:
            _LOG.warning("attendance answered %d to a job list", response.status_code)
            return None

        jobs = _json_object(response.text).get("jobs")

        if not is_list(jobs):
            return None

        return any(_status_of(as_object(one)) in LIVE_STATUSES for one in jobs)

    def _post(self, path: str, body: dict[str, object]) -> httpx.Response:
        return self._client.post(
            path,
            json=body,
            headers=self._auth,
            timeout=httpx.Timeout(CONNECT_TIMEOUT_S, read=READ_TIMEOUT_S),
        )


def _build_client(target: AttendanceTarget) -> httpx.Client:
    transport = httpx.HTTPTransport(uds=str(target.socket)) if target.socket is not None else None

    return httpx.Client(base_url=target.url, transport=transport)


def _enqueued_of(raw: str) -> EnqueuedJob:
    body = _json_object(raw)
    session = field_text(body, "session_id")
    status = _status_of(body)
    created = body.get("created")

    if not session or status is None or not isinstance(created, bool):
        raise AttendanceError("internal", "attendance's job answer is malformed", _OK)

    return EnqueuedJob(session=session, status=status, created=created)


def _status_of(body: dict[str, object]) -> JobStatus | None:
    known = {status.value: status for status in JobStatus}
    return known.get(field_text(body, "status"))


def _error_of(status: int, raw: str) -> AttendanceError:
    """Read contract 02 §14's error body. A missing or malformed one still
    fails, as `internal`, rather than swallowing the refusal."""
    error = as_object(_json_object(raw).get("error"))
    code = field_text(error, "code") or "internal"
    message = field_text(error, "message") or f"attendance answered {status}"

    return AttendanceError(code, message, status)


def _json_object(raw: str) -> dict[str, object]:
    try:
        parsed: object = json.loads(raw)
    except (ValueError, RecursionError):
        # RecursionError: an answer that nests too deep is not a ValueError.
        return {}

    return as_object(parsed)
