"""One job over ACP: one session on the vmm, one prompt, a deadline.

The delegate door and the dispatch door both run a job this way (contract
02 §6). Each job is one session with one row and one VM (decision CH1).
attendance mints the session id and makes the row with the typed origin of
the job (decisions CH21-4, CH25 rule 2). The job's own id (`job-<ulid>` or
`auto-<ulid>`) rides in the origin, never in `_meta`.

```
  create --> acp stream --> initialize --> prompt --+--> stop reason --> close --> drop --> JobEnd
                                                    |
                                  deadline passes --+--> session/cancel --> wait --> TIMEOUT
```

The facade opens its one session from the handshake id of guest init, so
the job sends no `session/new` (decision CH21-8). The session drops at the
end of the job (decision CH21-2).

Nothing here raises past the caller. A refusal, a lost link and a deadline
each end in a `JobEnd`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from creche_wire import CreateSession, FamilyName, SessionCwd, SessionId, SessionOrigin, UnixMillis

from .acp_link import (
    MAX_ACP_LINE_BYTES,
    AcpCallFailed,
    AcpHandshakeError,
    AcpLink,
    AcpLinkLost,
    PromptEnd,
    StopReason,
)
from .channel import ChannelClosed
from .clock import now_ms
from .errors import TurnReason
from .ids import new_session_id
from .vmm import Vmm, VmmRefused

_LOG = logging.getLogger("attendance")

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
    """What one job left: its end, its text, and the id of its session."""

    kind: EndKind
    text: str = ""
    error: str | None = None
    acp_session: str | None = None


@dataclass(slots=True, frozen=True)
class JobPlan:
    """Everything one job needs. The message is data: the caller wrote it."""

    family: str
    origin: SessionOrigin
    cwd: str
    message: str
    deadline_s: float
    cancel_wait_s: float = CANCEL_WAIT_S


async def run_job(
    vmm: Vmm, plan: JobPlan, on_session: Callable[[str], None] | None = None
) -> JobEnd:
    """Run one job to its end. `on_session` hears the session id once the
    vmm holds its row."""
    session = new_session_id()
    row = CreateSession(
        created_at=UnixMillis(now_ms()),
        cwd=SessionCwd(plan.cwd),
        family=FamilyName(plan.family),
        id=SessionId(session),
        origin=plan.origin,
    )

    try:
        await vmm.create(row)
    except VmmRefused as error:
        return JobEnd(EndKind.FAILED, error=f"{error.code}: the vmm made no session: {error}")

    if on_session is not None:
        on_session(session)

    try:
        return await _on_session(vmm, plan, session)
    finally:
        await drop_job_session(vmm, plan.family, session)


async def _on_session(vmm: Vmm, plan: JobPlan, session: str) -> JobEnd:
    """The stream of the session, its handshake and the one prompt."""
    try:
        stream = await vmm.open_acp(plan.family, session, MAX_ACP_LINE_BYTES)
    except VmmRefused as error:
        return JobEnd(EndKind.FAILED, error=f"{error.code}: {error}", acp_session=session)

    link = AcpLink(plan.family, session, stream)

    try:
        await link.start()
        return await _prompt(link, plan)
    except (AcpHandshakeError, ChannelClosed) as error:
        reason = TurnReason.CHANNEL_LOST.value
        return JobEnd(EndKind.FAILED, error=f"{reason}: {error}", acp_session=session)
    finally:
        await link.close()


async def drop_job_session(vmm: Vmm, family: str, session: str) -> None:
    """Drop the session of a job (decision CH21-2). A failed drop leaves
    the row, and the log names it."""
    try:
        await vmm.drop(family, session)
    except VmmRefused as error:
        _LOG.warning("family=%s session=%s the job session stays: %s", family, session, error)


async def _prompt(link: AcpLink, plan: JobPlan) -> JobEnd:
    session = link.session
    asked = asyncio.ensure_future(link.prompt(plan.message))

    try:
        async with asyncio.timeout(plan.deadline_s):
            end = await asyncio.shield(asked)
    except TimeoutError:
        await _cancel(link, asked, plan.cancel_wait_s)
        error = f"{TurnReason.TURN_TIMEOUT.value}: no answer within {plan.deadline_s:g} s"
        return JobEnd(EndKind.TIMEOUT, error=error, acp_session=session)
    except AcpCallFailed as error:
        return JobEnd(EndKind.FAILED, error=f"{_MODEL_ERROR}: {error}", acp_session=session)
    except AcpLinkLost as error:
        reason = TurnReason.CHANNEL_LOST.value
        return JobEnd(EndKind.FAILED, error=f"{reason}: {error}", acp_session=session)
    except asyncio.CancelledError:
        asked.cancel()
        raise

    return _ended(end, session)


async def _cancel(link: AcpLink, asked: asyncio.Future[PromptEnd], wait_s: float) -> None:
    """`session/cancel`, then wait for the prompt to answer, up to `wait_s`."""
    await link.cancel()

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
