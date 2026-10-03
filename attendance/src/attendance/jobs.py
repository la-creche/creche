"""Thin-job shapes: the job session id, the delegation, and the answer.

A thin job is one session that lives for one job (contract 02 §12, invariant
15). The PEP calls the delegate door, this service runs exactly one turn, and
the answer travels back in the response because the journal dies with the
session.

Everything here is a record or a pure function. The lifecycle itself belongs
to the service, which is the only layer that owns sessions and turns.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .errors import TurnReason
from .ids import SessionPrefix, new_ulid
from .models import Turn
from .states import TurnState

# Contract 02 §12 rule 3 and contract 04 §7.5 rule 4. The family file's
# `job.timeout` overrides it; this is what a document without one means.
DEFAULT_JOB_TIMEOUT_S = 120

# Contract 04 §7.1. The message the calling agent sends.
MESSAGE_MIN_BYTES = 1
MESSAGE_MAX_BYTES = 65_536

# Contract 02 §13.1 uses the same bound for an outcome's error text.
ERROR_MAX_CHARS = 4_096


class JobStatus(StrEnum):
    """Contract 04 §7.3's three answers."""

    OK = "ok"
    TIMEOUT = "timeout"
    FAILED = "failed"


@dataclass(slots=True, frozen=True)
class Delegation:
    """One delegate call, as contract 04 §7.3 describes it.

    `caller_family` is trusted: the PEP read it from the family token.
    `caller_session` is advisory and travels for the audit and the noticeboard.
    """

    delegation_id: str
    caller_family: str
    caller_session: str | None = None

    def to_channel(self) -> dict[str, Any]:
        """What rides on `start_turn` (contract 03 §7.4 rule 4 item 5).

        Two fields, and no third: the playpen's reader keeps `id` and
        `caller_session`, and no header carries a family. Always both keys,
        `caller_session` possibly null: contract 04 §7.3 makes
        `claimed_session_id` advisory, so the PEP may send none, and that is
        a delegation with no caller, not a malformed one. Leaving the whole
        object out on a null `caller_session` would lose the delegation id
        too, and run the chain (contract 04 §6.3) one hop short for a call
        the PEP chose not to attribute.
        """
        return {"id": self.delegation_id, "caller_session": self.caller_session}


def new_job_session() -> str:
    """A fresh `job-<ulid>` id (contract 02 §2)."""
    return f"{SessionPrefix.JOB.value}{new_ulid()}"


def job_timeout_s(family_timeout_s: int | None) -> int:
    """The turn's deadline for one job (contract 02 §12 rule 3)."""
    if family_timeout_s is None or family_timeout_s <= 0:
        return DEFAULT_JOB_TIMEOUT_S

    return family_timeout_s


def status_of(turn: Turn) -> JobStatus:
    """Contract 04 §7.3's status, from the turn that ended.

    Only `settled` is success (contract 02 §4.3). A turn that ran out of time
    is its own answer, because contract 04 §7.5 makes the caller give up on
    the same clock and a timeout is not a failure of the agent.
    """
    if turn.state is TurnState.SETTLED:
        return JobStatus.OK

    if turn.reason is TurnReason.TURN_TIMEOUT:
        return JobStatus.TIMEOUT

    return JobStatus.FAILED


def answer(session: str, turn: Turn, text: str) -> dict[str, Any]:
    """Contract 04 §7.3's body, plus the turn's usage.

    `content` is present only on `ok`, and `error` only otherwise. Both keys
    are always there, so one reader shape serves every outcome.
    """
    status = status_of(turn)
    ok = status is JobStatus.OK

    return {
        "status": status.value,
        "session_id": session,
        "content": text if ok else None,
        "error": None if ok else _error_text(turn),
        "usage": turn.usage.to_api(),
    }


def _error_text(turn: Turn) -> str:
    """Why the job did not answer. The reason first, so a grep finds it."""
    reason = turn.reason.value if turn.reason is not None else TurnState.FAILED.value
    return f"{reason}: the job ended in {turn.state.value}"[:ERROR_MAX_CHARS]
