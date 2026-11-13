"""The service behind the routes (contract 02 §5 to §7, §12, §13.4).

Four operations run work in a family: a delegate call, a dispatched job, a
trigger's job and an ACP door connection. Each one reaches the facade of a
session over its vmm ACP stream (decisions CH1, CH20, CH21-7). The view
reads a family's sessions from the host session list, and `busy` counts
the work in flight.

`attendance` never calls `caregiver`. Everything it knows about a family comes
from the status document `caregiver` publishes (contract 05 §2). The vmm
owns each VM and the host session list (decision CH1).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from typing import Any, Final

from creche_wire import (
    Auto,
    AutoJobId,
    AutoOrigin,
    Delegate,
    DelegateOrigin,
    DelegationId,
    Dispatch,
    DispatchChain,
    DispatchTrigger,
    DoorOrigin,
    FamilyName,
    JobId,
    ListCursor,
    ListSessions,
    SessionOrigin,
    Timer,
    TimerTrigger,
    Trigger,
    Webhook,
    WebhookName,
    WebhookTrigger,
)
from creche_wire.routes import DROP_CLIENT_TIMEOUT_S
from starlette.websockets import WebSocket

from . import acp_door, acp_view, dispatch, jobs, outcomes
from .acp_door import DoorPrompts
from .acp_jobs import CANCEL_WAIT_S, HostFull, JobEnd, JobPlan, drop_job_session, run_job
from .atomic import read_json
from .auth import Access, Principal, check_access, check_family_kind
from .clock import now
from .config import Config
from .dispatch import DispatchJob
from .errors import ApiError, ErrorCode
from .family_status import FamilyStatus, StatusReader, check_may_serve, served_kind
from .faults import FaultReporter
from .ids import SESSION_CWD_MAX, SessionPrefix, is_session_cwd, is_session_id, new_ulid
from .models import DEFAULT_DEADLINE_S, TriggerKind
from .paths import outcome_file, session_cwd
from .queueing import MAX_QUEUED_TURNS, JobQueue
from .requests import DelegateRequest, DispatchRequest, JobQuery, TriggerRequest
from .tasks import report_failure
from .vmm import Vmm, VmmRefused

_LOG = logging.getLogger("attendance")

# How often the upkeep loop tries the head of each dispatch queue again.
PUMP_INTERVAL_S = 1.0

#: The most that the drop of the job rows at a start may take, in seconds.
#: Past it the start goes on, and the rows that are left stay for the next
#: start (decision CH84 rule 6, see `drop_job_rows`). It is the timeout
#: of one vmm call, so a vmm that hangs keeps the sockets of attendance
#: unbound for one call at most, whatever the count of families.
JOB_ROWS_BOUND_S: Final = DROP_CLIENT_TIMEOUT_S


class SessionService:
    """The jobs, the ACP door and the view of each family."""

    def __init__(
        self,
        config: Config,
        vmm: Vmm,
        faults: FaultReporter,
        cancel_wait_s: float = CANCEL_WAIT_S,
        max_queued_turns: int = MAX_QUEUED_TURNS,
    ) -> None:
        self._config = config
        self._vmm = vmm
        #: The one fault file writer. The vmm client raises and clears
        #: `vmm_protocol_mismatch` in it (contract 05 §3.3.1).
        self._faults = faults
        self._status = StatusReader(config.state_root)
        self._dispatches = dispatch.DispatchLedger(config.state_root)
        self._triggers = dispatch.DispatchLedger(config.state_root, dispatch.LedgerDoor.TRIGGER)
        self.door_prompts = DoorPrompts()
        #: The prompts of the jobs in flight, delegate and dispatched alike.
        self._job_prompts = 0
        self._cancel_wait_s = cancel_wait_s
        # Phase 2 plan, row 19. The dispatch door's jobs, their limit and FIFO.
        self._jobs: JobQueue[DispatchJob] = JobQueue(max_queued_turns)
        self._job_tasks: set[asyncio.Task[None]] = set()
        self._upkeep: asyncio.Task[None] | None = None

    # ---------------------------------------------------------------- life

    def start(self) -> None:
        """Clear this writer's faults (contract 05 §3.3.1 rule 8).

        The fault file holds this writer's current open faults, and a fresh
        process has observed none. The clear covers each file in the
        directory, so a family that `caregiver` removed keeps no fault.
        """
        self._faults.start()

    async def drop_job_rows(self) -> None:
        """Drop each job row of each family, before the first request
        (contract 02 §6 rule 5, decision CH83 rule 5).

        No job outlives a restart (§13.3), so a row whose origin is
        `delegate` or `auto` names a job that is over. Its drop also leaves
        no transcript of a thin job on a session disk (§12 rule 6). A `door`
        row stays. The drops run at once, so the stops of their VMs overlap.

        A vmm that does not answer at this start drop leaves the rows
        (decision CH84 rule 6). A list or a drop that the vmm refuses leaves
        the rows, and the log names the family. The families run at once,
        and the whole step ends within `JOB_ROWS_BOUND_S`. Past it the rows
        that are left stay, and the log names each family whose step did
        not end. The next start tries again.
        """
        families = self._status.families()
        pending = set(families)

        async def drop_family(family: str) -> None:
            try:
                sessions = await self._job_rows(family)
            except VmmRefused as error:
                _LOG.warning("family=%s the job rows stay: %s", family, error)
                sessions = []

            await asyncio.gather(
                *(drop_job_session(self._vmm, family, session) for session in sessions)
            )
            pending.discard(family)

        try:
            async with asyncio.timeout(JOB_ROWS_BOUND_S):
                await asyncio.gather(*(drop_family(family) for family in families))
        except TimeoutError:
            _LOG.warning(
                "families=%s the job rows may stay: the drop passed %g s",
                ",".join(sorted(pending)),
                JOB_ROWS_BOUND_S,
            )

    async def _job_rows(self, family: str) -> list[str]:
        """The id of each row of the family whose origin is not `door`. It
        reads every page before a drop, so no drop moves a cursor."""
        rows: list[str] = []
        cursor: ListCursor | None = None

        while True:
            query = ListSessions(cursor=cursor, cwd=None, family=FamilyName(family))
            page = await self._vmm.list_page(query)
            rows.extend(
                row.id.root for row in page.rows if not isinstance(row.origin.root, DoorOrigin)
            )

            if page.next is None:
                return rows

            cursor = page.next

    def start_upkeep(self) -> None:
        """Begin the queue pump. Call it inside the event loop: it owns a task."""
        if self._upkeep is None:
            self._upkeep = asyncio.create_task(self._upkeep_loop(), name="upkeep")
            self._upkeep.add_done_callback(report_failure)

    async def _upkeep_loop(self) -> None:
        """Try the head of each dispatch queue again, once a second."""
        while True:
            await asyncio.sleep(PUMP_INTERVAL_S)
            self._upkeep_step("the queue pump", self.pump_queues)

    def _upkeep_step(self, name: str, step: Callable[[], None]) -> None:
        """Run one step of the upkeep loop. A failure never ends the loop."""
        try:
            step()
        except Exception:
            _LOG.exception("upkeep: %s failed", name)

    def pump_queues(self) -> None:
        """Try the head of each dispatch queue again (contract 02 §13 rule 3).

        A job that ends starts the head of the queue of its family. That
        start does not happen while the status document is unreadable, and
        no later job of the family can end to try again. The upkeep loop
        calls this, so the head starts within `PUMP_INTERVAL_S` of a document
        that can be read. It is public so a test can drive it without a clock.
        """
        for family in self._jobs.families():
            try:
                self._pump_dispatch(family)
            except Exception:
                # Each tick tries the families in the same order. Without
                # this guard one family that fails hides each family after it.
                _LOG.exception("dispatch queue of family %s was not tried", family)

    async def close(self) -> None:
        """End every job. Each job drops its own session on the way out."""
        if self._upkeep is not None:
            self._upkeep.cancel()
            self._upkeep = None

        for job in list(self._job_tasks):
            job.cancel()

        await asyncio.gather(*self._job_tasks, return_exceptions=True)
        self._job_tasks.clear()

    # ------------------------------------------------------------ ACP door

    def check_door(self, principal: Principal, family: str) -> None:
        """The checks of a door connection, before it reads a frame
        (contract 02 §5.1 rule 2): the family must serve, and the door must
        reach its kind."""
        check_access(principal, Access.ACP)
        status = self._status.require(family)
        check_may_serve(status)
        check_family_kind(principal, family, served_kind(status))

    async def serve_door(self, websocket: WebSocket, family: str) -> None:
        """One door connection, after `check_door` (`acp_door.py`)."""
        await acp_door.serve(websocket, family, self._vmm, self.door_prompts)

    # ----------------------------------------------------------------- view

    async def view_sessions(
        self, principal: Principal, family: str, query: Mapping[str, str]
    ) -> dict[str, Any]:
        """One page of the family's host list, with no boot (contract 02 §7)."""
        _check_view(principal)
        cursor = acp_view.read_cursor(query)
        self._status.require(family)
        return await acp_view.list_sessions(self._vmm, family, cursor)

    async def view_session(
        self, principal: Principal, family: str, session: str, query: Mapping[str, str]
    ) -> dict[str, Any]:
        """One page of a transcript: `initialize`, `session/load`.

        An id that is not a session id, such as an sbx-era id, is not found
        before the vmm is called (decision CH10-9)."""
        _check_view(principal)
        page = acp_view.read_page(query)

        if not is_session_id(session):
            raise ApiError(ErrorCode.NOT_FOUND, "no such session", family=family)

        self._status.require(family)
        cwd = self._session_cwd(family)
        return await acp_view.load_transcript(self._vmm, family, session, cwd, page)

    def busy(self, principal: Principal) -> dict[str, Any]:
        """The work in flight, for the release tool's quiet window. No family.

        A prompt counts for each job in flight (delegate and dispatched) and
        on each ACP door connection. A dispatched job counts from its slot,
        so a job that boots counts before its prompt.
        """
        _check_view(principal)
        prompts = self._job_prompts + self.door_prompts.in_flight
        running = self._jobs.running_total()
        queued = self._jobs.waiting_total()
        return {
            "busy": prompts + running + queued > 0,
            "prompts_in_flight": prompts,
            "jobs_running": running,
            "jobs_queued": queued,
        }

    # ------------------------------------------------------------ thin jobs

    async def run_delegate(self, principal: Principal, request: DelegateRequest) -> dict[str, Any]:
        """Contract 04 §7.3 and contract 02 §12: one session, one prompt.

        The PEP is the one caller. The answer travels in this response. The
        session drops at the end of the job (decision CH21-2). A job that
        met `host_full` for its whole deadline answers `host_full`, 503 with
        no `retry_after_s` (contract 02 §12 rule 4, decisions CH21-3, CH83
        rule 2).
        """
        check_access(principal, Access.WRITE)
        self._check_delegate_door(principal)
        family = request.target_family
        status = self._status.require(family)
        # The delegate door's grant is `thin`, so a family of any other kind
        # is refused here before anything is made (contract 02 §3.1).
        check_family_kind(principal, family, served_kind(status))
        check_may_serve(status)

        cwd = self._session_cwd(family)
        job = jobs.new_job_session()
        delegate = Delegate(delegation=DelegationId(request.delegation_id), job=JobId(job))
        plan = JobPlan(
            family=family,
            origin=SessionOrigin(DelegateOrigin(delegate=delegate)),
            cwd=cwd,
            message=request.message,
            deadline_s=jobs.job_timeout_s(status.job_timeout_s),
            waits_for_room=True,
            cancel_wait_s=self._cancel_wait_s,
        )
        end = await self._run_job(plan)

        if isinstance(end, HostFull):
            raise ApiError(
                ErrorCode.HOST_FULL,
                f"the host had no room for the job within {plan.deadline_s:g} s: {end.error}",
                family=family,
            )

        return jobs.answer(job, end)

    async def _run_job(
        self, plan: JobPlan, on_session: Callable[[str], None] | None = None
    ) -> JobEnd | HostFull:
        """One job, counted in `busy` while it runs."""
        self._job_prompts += 1

        try:
            return await run_job(self._vmm, plan, on_session)
        finally:
            self._job_prompts -= 1

    def _session_cwd(self, family: str) -> str:
        """The `cwd` of one session: the family's sessions directory.

        `attendance` chooses it (contract 02 §6 rule 1). The row keeps it as
        a display value, never a guest path (decision CH21-4). Contract 01
        minor 18 drops `job.caller_root`, so no job works in a directory of
        its calling chat (contract 02 §12.1, decisions CH1, CH10-10). The
        config makes the cwd, so a cwd that the wire refuses (decision CH21)
        is an internal fault.
        """
        cwd = session_cwd(self._config.sessions_root, family)

        if not is_session_cwd(cwd):
            raise ApiError(
                ErrorCode.INTERNAL,
                f"the session cwd is not an absolute path of at most {SESSION_CWD_MAX}"
                " characters with no NUL",
                family=family,
                detail={"length": len(cwd)},
            )

        return cwd

    def _check_delegate_door(self, principal: Principal) -> None:
        """Contract 02 §1: the delegate door serves this one path.

        The write grant alone is not enough. Three other doors hold it, and
        none of them may mint a job session in a thin family.
        """
        if principal is Principal.CHAPERONE_DELEGATE:
            return

        raise ApiError(
            ErrorCode.FORBIDDEN,
            f"{principal.value} is not the delegate door",
            detail={"principal": principal.value},
        )

    # ------------------------------------------------------------ dispatch

    async def run_dispatch(self, principal: Principal, request: DispatchRequest) -> dict[str, Any]:
        """Contract 02 §13.4.1, over ACP (phase 2 plan, row 19).

        It never waits for the job. The answer is the `auto-<ulid>` id, so a
        lead can start four workers and carry on, and `read_jobs` says later
        what became of each one.
        """
        check_access(principal, Access.WRITE)
        _check_door(principal, Principal.CHAPERONE_DISPATCH)
        target = request.target_family
        status = self._status.require(target)
        # The dispatch door's grant is `autonomous`, so a thin or attended
        # target is refused before anything is written (contract 02 §3.1).
        check_family_kind(principal, target, served_kind(status))
        check_may_serve(status)
        self._check_dispatchable(status)

        repeat = self._repeat_dispatch(request)

        if repeat is not None:
            return repeat

        entry = self._new_dispatch(request)
        job = DispatchJob(entry=entry, message=request.message, origin=_auto_origin(entry))
        return await self._enqueue(job, status)

    async def run_trigger(self, principal: Principal, request: TriggerRequest) -> dict[str, Any]:
        """One firing of `door-trigger`: one `auto-<ulid>` job in the family.

        The job takes a slot of the family's `JobQueue`, beside the
        dispatched jobs, so `max_running_turns` holds for both. A repeat of
        the idempotency key answers the first job and enqueues nothing.
        """
        check_access(principal, Access.WRITE)
        _check_door(principal, Principal.DOOR_TRIGGER)
        family = request.family
        status = self._status.require(family)
        check_family_kind(principal, family, served_kind(status))
        check_may_serve(status)
        found = self._triggers.find_repeat(family, request.idempotency_key)

        if found is not None:
            return self._dispatch_answer(found, created=False)

        entry = dispatch.DispatchEntry(
            session=f"{SessionPrefix.AUTO.value}{new_ulid()}",
            caller_family=family,
            family=family,
            enqueued_at=now(),
            idempotency_key=request.idempotency_key,
            kind=request.kind,
            trigger_name=request.name,
        )
        job = DispatchJob(entry=entry, message=request.message, origin=_auto_origin(entry))
        return await self._enqueue(job, status)

    async def _enqueue(self, job: DispatchJob, status: FamilyStatus) -> dict[str, Any]:
        """Start the job, or queue it when the family has no free slot."""
        entry = job.entry
        target = entry.family
        ledger = self._ledger_of(entry)
        # Rule 4: the queue is the TARGET's, and a full one refuses before
        # anything is written.
        self._jobs.check_room(target)

        if not self._jobs.take_slot(target, entry.session, status.max_running_turns):
            ledger.write(entry)
            self._jobs.add(target, entry.session, job)
            return self._dispatch_answer(entry, created=True)

        ledger.write(entry)
        self._start_job(job)
        return self._dispatch_answer(entry, created=True)

    def read_jobs(self, principal: Principal, query: JobQuery) -> dict[str, Any]:
        """Contract 02 §13.4.2. The jobs ONE family dispatched, newest first.

        The scope is the ledger directory of `caller_family`, so an unknown
        session id and another family's session id are both simply absent
        (rule 2). No id ever leaks.
        """
        check_access(principal, Access.WRITE)
        _check_door(principal, Principal.CHAPERONE_DISPATCH)
        return self._read_ledger(self._dispatches, query)

    def read_trigger_jobs(self, principal: Principal, query: JobQuery) -> dict[str, Any]:
        """The jobs `door-trigger` started in one family, newest first."""
        check_access(principal, Access.WRITE)
        _check_door(principal, Principal.DOOR_TRIGGER)
        return self._read_ledger(self._triggers, query)

    def _read_ledger(self, ledger: dispatch.DispatchLedger, query: JobQuery) -> dict[str, Any]:
        if query.session is not None:
            one = ledger.read(query.caller_family, query.session)
            found = [one] if one is not None else []
        else:
            found = ledger.list(query.caller_family, query.since, query.limit)

        return {"jobs": [self._job_view(entry) for entry in found]}

    def _ledger_of(self, entry: dispatch.DispatchEntry) -> dispatch.DispatchLedger:
        """The ledger of the door that started this job."""
        return self._dispatches if entry.kind is TriggerKind.DISPATCH else self._triggers

    def dispatch_depth(self, family: str) -> int:
        """How many dispatched jobs of this family wait for a slot."""
        return self._jobs.depth(family)

    def _job_view(self, entry: dispatch.DispatchEntry) -> dict[str, Any]:
        """One job, live or finished. The live state comes from the job
        queue, and the outcome from the record on disk."""
        return dispatch.job_view(entry, self._job_state(entry.session), self._outcome_of(entry))

    def _job_state(self, job: str) -> dispatch.JobStatus | None:
        if self._jobs.is_running(job):
            return dispatch.JobStatus.RUNNING

        if self._jobs.is_waiting(job):
            return dispatch.JobStatus.QUEUED

        return None

    def _dispatch_answer(self, entry: dispatch.DispatchEntry, created: bool) -> dict[str, Any]:
        live = dispatch.status_of(self._job_state(entry.session), entry.outcome_id)
        return {"session_id": entry.session, "status": live.value, "created": created}

    def _outcome_of(self, entry: dispatch.DispatchEntry) -> dict[str, Any] | None:
        """§13.1's record for a finished job, or None while it runs.

        Rule 4: a noted outcome whose file cannot be read still means the job
        ended. `status_of` answers `ended` from the id alone.
        """
        if entry.outcome_id is None:
            return None

        return read_json(outcome_file(self._config.state_root, entry.family, entry.outcome_id))

    def _repeat_dispatch(self, request: DispatchRequest) -> dict[str, Any] | None:
        """Contract 02 §13.4.4. An hour of deduplication, per caller family."""
        key = request.idempotency_key

        if key is None:
            return None

        found = self._dispatches.find_repeat(request.caller_family, key)

        if found is None:
            return None

        if found.family != request.target_family:
            raise ApiError(
                ErrorCode.IDEMPOTENCY_MISMATCH,
                "this idempotency_key already dispatched to another family",
                family=request.target_family,
                detail={"session": found.session, "family": found.family},
            )

        return self._dispatch_answer(found, created=False)

    def _new_dispatch(self, request: DispatchRequest) -> dispatch.DispatchEntry:
        """The ledger entry of one job. The caller writes it (rule 5)."""
        return dispatch.DispatchEntry(
            session=f"{SessionPrefix.AUTO.value}{new_ulid()}",
            caller_family=request.caller_family,
            family=request.target_family,
            enqueued_at=now(),
            delegation_id=request.delegation_id,
            chain=request.chain,
            caller_session=request.claimed_session_id,
            idempotency_key=request.idempotency_key,
        )

    def _start_job(self, job: DispatchJob) -> None:
        """Run one job apart from the request."""
        task = asyncio.create_task(
            self._run_dispatch_job(job), name=f"dispatch {job.entry.session}"
        )
        task.add_done_callback(report_failure)
        task.add_done_callback(self._job_tasks.discard)
        self._job_tasks.add(task)

    async def _run_dispatch_job(self, job: DispatchJob) -> None:
        """One prompt, then the outcome record, then the next queued job.

        `message` becomes the prompt as DATA. The calling model wrote it, so
        nothing here reads it as an instruction (invariant 14). The trigger
        rides in the origin of the row, never in the prompt's text.

        A job that meets `host_full` writes no outcome. It goes back to the
        queue in its place, and the upkeep loop tries it again within
        `PUMP_INTERVAL_S` (contract 02 §6 rule 4, decision CH21-3). Its end
        starts no queued job, because the host has no room.
        """
        entry = job.entry
        started = now()
        end: JobEnd | HostFull | None = None

        try:
            end = await self._dispatch_prompt(job)

            if isinstance(end, JobEnd):
                record = outcomes.from_job(
                    entry.family, entry.session, entry.trigger(), started, end
                )
                outcomes.write(self._config.state_root, record)
                self._ledger_of(entry).note_outcome(entry.caller_family, entry.session, record.id)
        finally:
            if isinstance(end, HostFull):
                _LOG.info(
                    "family=%s job=%s waits in the queue: %s",
                    entry.family,
                    entry.session,
                    end.error,
                )
                self._jobs.requeue(entry.family, entry.session, job)
            else:
                self._jobs.release(entry.family, entry.session)
                self._upkeep_step("the dispatch pump", lambda: self._pump_dispatch(entry.family))

    async def _dispatch_prompt(self, job: DispatchJob) -> JobEnd | HostFull:
        entry = job.entry
        plan = JobPlan(
            family=entry.family,
            origin=job.origin,
            cwd=self._session_cwd(entry.family),
            message=job.message,
            deadline_s=DEFAULT_DEADLINE_S,
            waits_for_room=False,
            cancel_wait_s=self._cancel_wait_s,
        )
        return await self._run_job(
            plan,
            on_session=lambda session: self._ledger_of(entry).note_acp_session(
                entry.caller_family, entry.session, session
            ),
        )

    def _pump_dispatch(self, family: str) -> None:
        """Start the head of the family's queue while a slot is free (§13 rule 3).

        The session is made inside the job, so the pump never waits.
        """
        while (head := self._jobs.head(family)) is not None:
            status = self._status.read(family)

            if status is None:
                return

            job_id, job = head

            if not self._jobs.take_slot(family, job_id, status.max_running_turns):
                return

            self._jobs.pop(family)
            self._start_job(job)

    def _check_dispatchable(self, status: FamilyStatus) -> None:
        """Contract 02 §13.4.1 rule 3. The TARGET declares `enqueue: true`.

        Absence is denial (invariant 11). A caller's grant file says who may
        dispatch; only the target's own file says that it may be dispatched.
        """
        if status.accepts_dispatch:
            return

        raise ApiError(
            ErrorCode.DISPATCH_NOT_DECLARED,
            f"family {status.family} declares no enqueue trigger",
            family=status.family,
        )

    # ------------------------------------------------------------ for tests

    @property
    def faults(self) -> FaultReporter:
        return self._faults


def _check_view(principal: Principal) -> None:
    """The view routes and `/busy` take `noticeboard` only.

    The read grant alone is not enough: each door that writes holds it too.
    """
    check_access(principal, Access.READ)

    if principal is not Principal.NOTICEBOARD:
        raise ApiError(
            ErrorCode.FORBIDDEN,
            f"{principal.value} is not the view principal",
            detail={"principal": principal.value},
        )


def _check_door(principal: Principal, door: Principal) -> None:
    """Contract 02 §13.4: each door that starts a job serves its own paths.

    The write grant alone is not enough, and neither is being the PEP:
    `chaperone-delegate` holds both and may not create an autonomous session.
    """
    if principal is door:
        return

    raise ApiError(
        ErrorCode.FORBIDDEN,
        f"{principal.value} may not use the routes of {door.value}",
        detail={"principal": principal.value},
    )


def _auto_origin(entry: dispatch.DispatchEntry) -> SessionOrigin:
    """The origin of the row of one autonomous job (decision CH21-4a).

    A dispatch carries its chain: 1 to 8 families, caller first (contract 02
    §13.2 rule 6). `read_dispatch` and the ledger reader refuse one with none,
    so no entry reaches this function without it (§13.4.1 rule 5).
    """
    job = AutoJobId(entry.session)

    if entry.kind is TriggerKind.TIMER:
        trigger = Trigger(TimerTrigger(timer=Timer()))
    elif entry.kind is TriggerKind.WEBHOOK:
        # `read_trigger` refuses a webhook firing with no name.
        name = WebhookName(str(entry.trigger_name))
        trigger = Trigger(WebhookTrigger(webhook=Webhook(name=name)))
    else:
        chain = DispatchChain([FamilyName(name) for name in entry.chain])
        caller = FamilyName(entry.caller_family)
        trigger = Trigger(DispatchTrigger(dispatch=Dispatch(chain=chain, name=caller)))

    return SessionOrigin(AutoOrigin(auto=Auto(job=job, trigger=trigger)))
