"""One job over ACP: `session/new`, one `session/prompt`, a deadline.

The delegate door and the dispatch door both run a job this way (phase 2
plan, rows 18 and 19). The facade mints the conversation's id. The job's own
id (`job-<ulid>` or `auto-<ulid>`) rides on `session/new` in `_meta`, so the
facade can name the conversation by it.

```
  new_session --> prompt --+--> stop reason ---------> JobEnd
                           |
          deadline passes -+--> session/cancel --> wait --> TIMEOUT
```

Nothing here raises past the caller. A refusal, a lost link and a deadline
each end in a `JobEnd`.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .acp_link import AcpCallFailed, AcpLink, AcpLinkLost, PromptEnd, StopReason
from .errors import TurnReason

# The three `_meta` keys of a job: `creche/job` on `session/new`, and
# `creche/delegation` or `creche/trigger` on `session/prompt`. The facade's
# vector file (`session_meta`) holds each key, its method and its shape, and
# `tests/test_acp_meta_vectors.py` holds these names equal to it.
META_JOB = "creche/job"
META_DELEGATION = "creche/delegation"
META_TRIGGER = "creche/trigger"

_MODEL_ERROR = TurnReason.MODEL_ERROR.value

#: How long a job waits for the `cancelled` answer after its deadline.
CANCEL_WAIT_S = 5.0


class EndKind(StrEnum):
    """How one job ended, before a door maps it to its own words."""

    OK = "ok"
    TIMEOUT = "timeout"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(slots=True, frozen=True)
class JobEnd:
    """What one job left: its end, its text, and the conversation's id."""

    kind: EndKind
    text: str = ""
    error: str | None = None
    acp_session: str | None = None


@dataclass(slots=True, frozen=True)
class JobPlan:
    """Everything one job needs. The message is data: the caller wrote it."""

    job: str
    cwd: str
    message: str
    deadline_s: float
    prompt_meta: dict[str, Any] | None = None
    cancel_wait_s: float = CANCEL_WAIT_S


async def run_job(
    link: AcpLink, plan: JobPlan, on_session: Callable[[str], None] | None = None
) -> JobEnd:
    """Run one job to its end. `on_session` hears the facade's id first."""
    try:
        session = await link.new_session(plan.cwd, {META_JOB: plan.job})
    except AcpCallFailed as error:
        return JobEnd(EndKind.FAILED, error=f"{_MODEL_ERROR}: session/new refused: {error}")
    except AcpLinkLost as error:
        return _lost(error, None)

    if on_session is not None:
        on_session(session)

    try:
        return await _prompt(link, plan, session)
    finally:
        link.end_session(session)


async def _prompt(link: AcpLink, plan: JobPlan, session: str) -> JobEnd:
    asked = asyncio.ensure_future(link.prompt(session, plan.message, plan.prompt_meta))

    try:
        async with asyncio.timeout(plan.deadline_s):
            end = await asyncio.shield(asked)
    except TimeoutError:
        await _cancel(link, session, asked, plan.cancel_wait_s)
        error = f"{TurnReason.TURN_TIMEOUT.value}: no answer within {plan.deadline_s:g} s"
        return JobEnd(EndKind.TIMEOUT, error=error, acp_session=session)
    except AcpCallFailed as error:
        return JobEnd(EndKind.FAILED, error=f"{_MODEL_ERROR}: {error}", acp_session=session)
    except AcpLinkLost as error:
        return _lost(error, session)
    except asyncio.CancelledError:
        asked.cancel()
        raise

    return _ended(end, session)


async def _cancel(
    link: AcpLink, session: str, asked: asyncio.Future[PromptEnd], wait_s: float
) -> None:
    """`session/cancel`, then wait for the prompt to answer, up to `wait_s`."""
    await link.cancel(session)

    with contextlib.suppress(TimeoutError, AcpCallFailed, AcpLinkLost):
        async with asyncio.timeout(wait_s):
            await asyncio.shield(asked)

    if not asked.done():
        asked.cancel()

    with contextlib.suppress(asyncio.CancelledError, AcpCallFailed, AcpLinkLost):
        await asked


def _ended(end: PromptEnd, session: str) -> JobEnd:
    """The stop reason's job end. Only `end_turn` is success."""
    if end.stop_reason is StopReason.END_TURN:
        return JobEnd(EndKind.OK, text=end.text, acp_session=session)

    if end.stop_reason is StopReason.CANCELLED:
        return JobEnd(
            EndKind.CANCELLED, error="cancelled: the facade cancelled the turn", acp_session=session
        )

    error = f"{end.stop_reason.value}: the turn ended before its answer"
    return JobEnd(EndKind.FAILED, text=end.text, error=error, acp_session=session)


def _lost(error: AcpLinkLost, session: str | None) -> JobEnd:
    reason = TurnReason.CHANNEL_LOST.value
    return JobEnd(EndKind.FAILED, error=f"{reason}: {error}", acp_session=session)
