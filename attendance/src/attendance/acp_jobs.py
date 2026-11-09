"""One job over ACP: one session on the vmm, one prompt, a deadline.

The delegate door and the dispatch door both run a job this way (contract
02 §6). Each job is one session with one row and one VM (decision CH1).
attendance mints the session id and makes the row with the typed origin of
the job (decisions CH21-4, CH25 rule 2). The job's own id (`job-<ulid>` or
`auto-<ulid>`) rides in the origin, never in `_meta`.

```
  create --> acp stream --> initialize --> prompt --+--> stop reason --> close --> drop --> JobEnd
               |                                    |
               +--> host_full --> drop --> HostFull +--> deadline --> session/cancel --> TIMEOUT
```

The facade opens its one session from the handshake id of guest init, so
the job sends no `session/new` (decision CH21-8). The session drops at the
end of the job (decision CH21-2).

The start of each job, its stream and its handshake, ends within the
deadline of the job, counted from the job's start. A start that does not
end in time is `TIMEOUT`. The vmm never queues: it answers `host_full` at
once (decision CH21-3). A job that waits for room, a delegate job, asks
for the stream again until the deadline, then ends in `HostFull`. Its
prompt gets what is left of the same deadline, so one clock holds the
whole job (contract 02 §12 rules 3 and 4, contract 04 §7.5). Any other
job ends in `HostFull` at once, and its door queues it (contract 02 §6
rule 4). Its prompt gets its whole deadline, counted from the prompt.

The start of a dispatch or a trigger job, its stream open and its
handshake, ends within the deadline of the job (decision CH84 rule 8). So a
hung start frees its queue slot, and the prompt keeps the deadline that it
had before the vmm. Decision CH50 rule 1 gives the start of a VM no outer
bound of its own.

Nothing here raises past the caller. A refusal, a lost link and a deadline
each end in a `JobEnd` or a `HostFull`.
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
from .channel import Channel, ChannelClosed
from .clock import now_ms
from .errors import TurnReason
from .ids import new_session_id
from .vmm import HOST_FULL, Vmm, VmmRefused

_LOG = logging.getLogger("attendance")

_MODEL_ERROR = TurnReason.MODEL_ERROR.value

#: How long a job waits for the `cancelled` answer after its deadline.
CANCEL_WAIT_S = 5.0

#: How long a job that waits for room pauses after a `host_full`.
#:
#: The pause between two tries is one second, the tick of the dispatch
#: queue pump, and a try starts only before the job deadline (decision CH84
#: rule 7). Decision CH21-3 gives the retry loop.
HOST_FULL_PAUSE_S = 1.0


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
class HostFull:
    """The vmm had no room for the VM of the job (decision CH21-3). The job
    sent no prompt, and its session dropped."""

    error: str


@dataclass(slots=True, frozen=True)
class JobPlan:
    """Everything one job needs. The message is data: the caller wrote it.

    A job with `waits_for_room` asks again after a `host_full` until its
    deadline, and `deadline_s` holds the whole job (contract 02 §12 rules 3
    and 4). A job without it ends at the first `host_full`, and its door
    queues it (contract 02 §6 rule 4). Its `deadline_s` holds its start and,
    once more, its prompt.
    """

    family: str
    origin: SessionOrigin
    cwd: str
    message: str
    deadline_s: float
    waits_for_room: bool
    cancel_wait_s: float = CANCEL_WAIT_S


async def run_job(
    vmm: Vmm, plan: JobPlan, on_session: Callable[[str], None] | None = None
) -> JobEnd | HostFull:
    """Run one job to its end. `on_session` hears the session id once the
    vmm holds its row."""
    start_by = asyncio.get_running_loop().time() + plan.deadline_s
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
        return await _on_session(vmm, plan, session, start_by)
    finally:
        await drop_job_session(vmm, plan.family, session)


async def _on_session(vmm: Vmm, plan: JobPlan, session: str, start_by: float) -> JobEnd | HostFull:
    """The stream of the session and its handshake, both by `start_by`,
    then the one prompt."""
    opening = asyncio.timeout_at(start_by)

    try:
        async with opening:
            stream = await _open_stream(vmm, plan, session, start_by)
    except VmmRefused as error:
        if error.code == HOST_FULL:
            return HostFull(error=str(error))

        return JobEnd(EndKind.FAILED, error=f"{error.code}: {error}", acp_session=session)
    except TimeoutError:
        if not opening.expired():
            raise

        return _not_started(plan, session)

    link = AcpLink(plan.family, session, stream)
    starting = asyncio.timeout_at(start_by)

    try:
        try:
            async with starting:
                await link.start()
        except TimeoutError:
            if not starting.expired():
                raise

            return _not_started(plan, session)

        loop = asyncio.get_running_loop()
        deadline = start_by if plan.waits_for_room else loop.time() + plan.deadline_s
        return await _prompt(link, plan, deadline)
    except (AcpHandshakeError, ChannelClosed) as error:
        reason = TurnReason.CHANNEL_LOST.value
        return JobEnd(EndKind.FAILED, error=f"{reason}: {error}", acp_session=session)
    finally:
        await link.close()


def _not_started(plan: JobPlan, session: str) -> JobEnd:
    """A start that did not end by the deadline of the job."""
    reason = TurnReason.TURN_TIMEOUT.value
    error = f"{reason}: the session did not start within {plan.deadline_s:g} s"
    return JobEnd(EndKind.TIMEOUT, error=error, acp_session=session)


async def _open_stream(vmm: Vmm, plan: JobPlan, session: str, start_by: float) -> Channel:
    """The vmm stream of the session. After a `host_full`, a job that waits
    for room asks again each `HOST_FULL_PAUSE_S` while the next try starts
    before `start_by` (decision CH21-3). The row stays between tries."""
    loop = asyncio.get_running_loop()

    while True:
        try:
            return await vmm.open_acp(plan.family, session, MAX_ACP_LINE_BYTES)
        except VmmRefused as error:
            again = plan.waits_for_room and loop.time() + HOST_FULL_PAUSE_S < start_by

            if error.code != HOST_FULL or not again:
                raise

        await asyncio.sleep(HOST_FULL_PAUSE_S)


async def drop_job_session(vmm: Vmm, family: str, session: str) -> None:
    """Drop the session of a job (decision CH21-2). A failed drop leaves
    the row, and the log names it."""
    try:
        await vmm.drop(family, session)
    except VmmRefused as error:
        _LOG.warning("family=%s session=%s the job session stays: %s", family, session, error)


async def _prompt(link: AcpLink, plan: JobPlan, deadline: float) -> JobEnd:
    session = link.session
    asked = asyncio.ensure_future(link.prompt(plan.message))

    try:
        async with asyncio.timeout_at(deadline):
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
