"""Thin-job shapes: the job id, the deadline, and the answer.

A thin job is one conversation that lives for one job (contract 02 §12,
invariant 15). The PEP calls the delegate door, this service runs exactly one
prompt over ACP (`acp_jobs.py`), and the answer travels back in the response.

Everything here is a record or a pure function.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from .acp_jobs import EndKind, JobEnd
from .ids import SessionPrefix, new_ulid

# Contract 02 §12 rule 3 and contract 04 §7.5 rule 4. The family file's
# `job.timeout` overrides it; this is what a document without one means.
DEFAULT_JOB_TIMEOUT_S = 120

# Contract 04 §7.1. The message the calling agent sends.
MESSAGE_MAX_BYTES = 65_536

# Contract 02 §13.1 uses the same bound for an outcome's error text.
ERROR_MAX_CHARS = 4_096


class JobStatus(StrEnum):
    """Contract 04 §7.3's three answers."""

    OK = "ok"
    TIMEOUT = "timeout"
    FAILED = "failed"


def new_job_session() -> str:
    """A fresh `job-<ulid>` id (contract 02 §2)."""
    return f"{SessionPrefix.JOB.value}{new_ulid()}"


def job_timeout_s(family_timeout_s: int | None) -> int:
    """The prompt's deadline for one job (contract 02 §12 rule 3)."""
    if family_timeout_s is None or family_timeout_s <= 0:
        return DEFAULT_JOB_TIMEOUT_S

    return family_timeout_s


def status_of(end: JobEnd) -> JobStatus:
    """Contract 04 §7.3's status. Only `end_turn` is success.

    A job that ran out of time is its own answer, because contract 04 §7.5
    makes the caller give up on the same clock and a timeout is not a
    failure of the agent.
    """
    if end.kind is EndKind.OK:
        return JobStatus.OK

    if end.kind is EndKind.TIMEOUT:
        return JobStatus.TIMEOUT

    return JobStatus.FAILED


def answer(job: str, end: JobEnd) -> dict[str, Any]:
    """Contract 04 §7.3's body.

    `content` is present only on `ok`, and `error` only otherwise. Both keys
    are always there, so one reader shape serves every outcome. ACP reports
    no usage, so `usage` is null.
    """
    status = status_of(end)
    ok = status is JobStatus.OK
    error = end.error or f"{status.value}: the job did not answer"

    return {
        "status": status.value,
        "session_id": job,
        "content": end.text if ok else None,
        "error": None if ok else error[:ERROR_MAX_CHARS],
        "usage": None,
    }
