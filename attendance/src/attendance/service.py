"""The service behind the routes (contract 02 §12, §13.4, contract 05 §5).

Four operations run work in a family: a delegate call, a dispatched job, a
trigger's job and an ACP door connection. Each one runs over ACP to the
sandbox's facade (`acp_link.py`). The fifth operation is the sandbox switch
that `caregiver` asks for. The view reads a family's sessions over a relay
of its own, and `busy` counts the work in flight.

`attendance` never calls `caregiver`. Everything it knows about a family comes
from the status document `caregiver` publishes (contract 05 §2).
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Callable, Mapping
from typing import Any

from . import acp_view, dispatch, jobs, outcomes
from .acp_door import DoorPrompts
from .acp_jobs import (
    CANCEL_WAIT_S,
    META_DELEGATION,
    META_TRIGGER,
    EndKind,
    JobEnd,
    JobPlan,
    run_job,
)
from .acp_link import (
    MAX_ACP_LINE_BYTES,
    AcpHandshakeError,
    AcpLink,
    ChannelFactory,
    LinkState,
    OrphanFacade,
)
from .atomic import read_json
from .auth import Access, Principal, check_access, check_family_kind
from .channel import Channel, ChannelClosed, SandboxDial
from .clock import now
from .config import Config
from .dispatch import DispatchJob
from .errors import ApiError, ErrorCode
from .exec_channel import ExecChannel
from .family_status import (
    FamilyStatus,
    SandboxInfo,
    StatusReader,
    check_may_serve,
    served_kind,
)
from .faults import FaultCode, FaultReporter
from .ids import SessionPrefix, is_sandbox, is_session, new_ulid
from .models import DEFAULT_DEADLINE_S, TriggerKind
from .paths import outcome_file, sandbox_sessions_mount
from .queueing import MAX_QUEUED_TURNS, JobQueue
from .requests import (
    DelegateRequest,
    DispatchRequest,
    JobQuery,
    SwitchMode,
    SwitchRequest,
    TriggerRequest,
)
from .switching import SwitchBook, SwitchKey, SwitchOutcome, SwitchRun, SwitchTally, switch_key
from .tasks import report_failure
from .workspace import make_owner_dir, owner_dir, workspace_of

_LOG = logging.getLogger("attendance")

# Contract 03 §10 rule 5: a cold start costs about 10 to 15 seconds.
# `sandbox_unavailable` is for a failed start, never for one in progress
# (contract 02 §5.1), so the service waits this long for one.
COLD_START_WAIT_S = 60.0
COLD_START_POLL_S = 0.25

# How often the upkeep loop tries the head of each dispatch queue again.
PUMP_INTERVAL_S = 1.0


class SessionService:
    """The jobs, the ACP door and the switch of each family."""

    def __init__(
        self,
        config: Config,
        cold_start_wait_s: float = COLD_START_WAIT_S,
        acp_factory: ChannelFactory | None = None,
        cancel_wait_s: float = CANCEL_WAIT_S,
        max_queued_turns: int = MAX_QUEUED_TURNS,
        relay_factory: ChannelFactory | None = None,
    ) -> None:
        self._config = config
        self._faults = FaultReporter(config.state_root)
        self._status = StatusReader(config.state_root)
        self._switches = SwitchBook()
        self._cold_start_wait_s = cold_start_wait_s
        self._dispatches = dispatch.DispatchLedger(config.state_root)
        self._triggers = dispatch.DispatchLedger(config.state_root, dispatch.LedgerDoor.TRIGGER)
        # Phase 2 plan, row 18. One supervisor link to each sandbox's facade,
        # and one lock per sandbox so two jobs never start two facades.
        self._acp_factory = acp_factory if acp_factory is not None else self._facade_factory
        # One `acp-relay` per door connection and per view request.
        self._relay_factory = relay_factory if relay_factory is not None else self._relay_channel
        self.door_prompts = DoorPrompts()
        self._acp_links: dict[str, AcpLink] = {}
        self._acp_dials: dict[str, asyncio.Lock] = {}
        self._cancel_wait_s = cancel_wait_s
        # Phase 2 plan, row 19. The dispatch door's jobs, their limit and FIFO.
        self._jobs: JobQueue[DispatchJob] = JobQueue(max_queued_turns)
        self._job_tasks: set[asyncio.Task[None]] = set()
        self._upkeep: asyncio.Task[None] | None = None

    # ---------------------------------------------------------------- life

    def start(self) -> None:
        """Clear this writer's faults (contract 05 §3.3.1).

        The fault file holds this writer's current open faults, and a fresh
        process has observed none. Without this a blocking fault would
        survive for ever: it stops new jobs, so nothing dials the sandbox
        again and nothing can ever clear it.
        """
        for family in self._status.families():
            self._faults.clear_all(family)

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
        """End every job, then stop every facade."""
        if self._upkeep is not None:
            self._upkeep.cancel()
            self._upkeep = None

        # A drain waits for jobs, and a shutdown may not wait for a drain.
        # Nothing about a switch is on disk, so the restart converges on the
        # status document (contract 05 §5.3 rule 1).
        self._switches.cancel_all()

        for job in list(self._job_tasks):
            job.cancel()

        await asyncio.gather(*self._job_tasks, return_exceptions=True)
        self._job_tasks.clear()

        for acp in list(self._acp_links.values()):
            await acp.stop()

        self._acp_links.clear()

    # ------------------------------------------------------------ ACP door
    async def open_acp_relay(self, principal: Principal, family: str) -> Channel:
        """One started `sbx exec acp-relay` for one ACP door connection.

        The same checks as a job: the family must serve, the door must
        reach its kind, and the sandbox is the one its jobs go to.

        `acp-relay` only pipes to the facade's in-VM socket, so the facade
        must run before the relay starts. An idle family has none: its
        supervisor link starts one here. A facade that cannot start is
        `sandbox_unavailable`, and no relay starts.
        """
        check_access(principal, Access.ACP)
        status = self._status.require(family)
        check_may_serve(status)
        check_family_kind(principal, family, served_kind(status))
        info = self._sandbox_for(family, status)

        if info is not None:
            await self._acp_link_for(family, status, info.id)

        return await self._start_relay(family, status)

    async def _start_relay(self, family: str, status: FamilyStatus) -> Channel:
        """One started relay to the sandbox the family's jobs go to."""
        info = self._sandbox_for(family, status)

        if info is None:
            raise ApiError(
                ErrorCode.SANDBOX_UNAVAILABLE,
                f"family {family} has no sandbox to relay to",
                family=family,
            )

        relay = self._relay_factory(self._dial_for(family, status, info.id))

        try:
            await relay.start()
        except ChannelClosed as error:
            raise ApiError(
                ErrorCode.SANDBOX_UNAVAILABLE,
                f"family {family} relay did not start",
                family=family,
            ) from error

        return relay

    # ----------------------------------------------------------------- view

    async def view_sessions(
        self, principal: Principal, family: str, query: Mapping[str, str]
    ) -> dict[str, Any]:
        """One page of the family's sessions: `initialize`, `session/list`."""
        _check_view(principal)
        cursor = acp_view.read_cursor(query)
        relay = await self._start_relay(family, self._status.require(family))

        try:
            return await acp_view.list_sessions(relay, family, cursor)
        finally:
            await relay.close()

    async def view_session(
        self, principal: Principal, family: str, session: str, query: Mapping[str, str]
    ) -> dict[str, Any]:
        """One page of a transcript: `initialize`, `session/load`."""
        _check_view(principal)
        page = acp_view.read_page(query)

        if not is_session(session):
            raise ApiError(ErrorCode.BAD_REQUEST, "not a session id", family=family)

        relay = await self._start_relay(family, self._status.require(family))
        cwd = sandbox_sessions_mount(self._config.sessions_root, family)

        try:
            return await acp_view.load_transcript(relay, family, session, cwd, page)
        finally:
            await relay.close()

    def busy(self, principal: Principal) -> dict[str, Any]:
        """The work in flight, for the release tool's quiet window. No family.

        A prompt counts on each supervisor link (delegate and dispatch
        jobs) and on each ACP door connection. A dispatched job counts from
        its slot, so a job that dials counts before its prompt.
        """
        _check_view(principal)
        links = sum(link.prompts_in_flight for link in self._acp_links.values())
        prompts = links + self.door_prompts.in_flight
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
        """Contract 04 §7.3 and contract 02 §12, over ACP (phase 2 plan, row 18).

        One `session/new` and one `session/prompt` on the family's facade.
        The PEP is the one caller. The answer travels in this response. The
        conversation stays in the facade's store (phase 2 plan, Q5).
        """
        check_access(principal, Access.WRITE)
        self._check_delegate_door(principal)
        family = request.target_family
        status = self._status.require(family)
        # The delegate door's grant is `thin`, so a family of any other kind
        # is refused here before anything is dialled (contract 02 §3.1).
        check_family_kind(principal, family, served_kind(status))
        check_may_serve(status)

        owner = request.claimed_session_id
        self._prepare_work_dir(family, owner)
        link = await self._acp_link_for(family, status)
        job = jobs.new_job_session()
        delegation = {"id": request.delegation_id, "caller_session": owner}
        plan = JobPlan(
            job=job,
            cwd=self._job_cwd(family, owner),
            message=request.message,
            deadline_s=jobs.job_timeout_s(status.job_timeout_s),
            prompt_meta={META_DELEGATION: delegation},
            cancel_wait_s=self._cancel_wait_s,
        )
        return jobs.answer(job, await run_job(link, plan))

    def _job_cwd(self, family: str, owner: str | None) -> str:
        """The cwd of one job inside the VM (contract 02 §12.1 rule 3).

        `code-sandbox` works in the calling chat's directory. Every other job
        works in the family's sessions mount. Each is a host path, and sbx
        mounts it at the same path inside the VM.
        """
        if owner is not None and workspace_of(family, owner) is not None:
            return str(owner_dir(self._config.work_root, owner))

        return sandbox_sessions_mount(self._config.sessions_root, family)

    def _prepare_work_dir(self, family: str, owner: str | None) -> None:
        """Contract 02 §12.1 rule 2. `code-sandbox` works in the chat's own
        directory, made at that chat's first delegate call and at no other
        moment. The id is validated before it becomes a path component."""
        if workspace_of(family, owner) is None or owner is None:
            return

        make_owner_dir(self._config.work_root, owner)

    def _check_delegate_door(self, principal: Principal) -> None:
        """Contract 02 §1: the delegate door serves this one path.

        The write grant alone is not enough. Three other doors hold it, and
        none of them may mint a job session in a thin family.
        """
        if principal is Principal.DOOR_DELEGATE:
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
        _check_door(principal, Principal.DOOR_DISPATCH)
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

        job = DispatchJob(entry=self._new_dispatch(request), message=request.message)
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
        return await self._enqueue(DispatchJob(entry=entry, message=request.message), status)

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

        try:
            link = await self._acp_link_for(target, status)
        except ApiError:
            # Rule 5: a refusal leaves no id for `job_status` to report.
            self._jobs.release(target, entry.session)
            raise

        ledger.write(entry)
        self._start_job(link, job)
        return self._dispatch_answer(entry, created=True)

    def read_jobs(self, principal: Principal, query: JobQuery) -> dict[str, Any]:
        """Contract 02 §13.4.2. The jobs ONE family dispatched, newest first.

        The scope is the ledger directory of `caller_family`, so an unknown
        session id and another family's session id are both simply absent
        (rule 2). No id ever leaks.
        """
        check_access(principal, Access.WRITE)
        _check_door(principal, Principal.DOOR_DISPATCH)
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

    def _start_job(self, link: AcpLink | None, job: DispatchJob) -> None:
        """Run one job apart from the request. With no link, the job dials."""
        task = asyncio.create_task(
            self._run_dispatch_job(link, job), name=f"dispatch {job.entry.session}"
        )
        task.add_done_callback(report_failure)
        task.add_done_callback(self._job_tasks.discard)
        self._job_tasks.add(task)

    async def _run_dispatch_job(self, link: AcpLink | None, job: DispatchJob) -> None:
        """One prompt, then the outcome record, then the next queued job.

        `message` becomes the prompt as DATA. The calling model wrote it, so
        nothing here reads it as an instruction (invariant 14). The trigger
        rides in the prompt's `_meta`, never in its text.
        """
        entry = job.entry
        started = now()

        try:
            end = await self._dispatch_prompt(link, job)
            sandbox = link.status.sandbox if link is not None else None
            record = outcomes.from_job(
                entry.family, entry.session, entry.trigger(), started, end, sandbox or ""
            )
            outcomes.write(self._config.state_root, record)
            self._ledger_of(entry).note_outcome(entry.caller_family, entry.session, record.id)
        finally:
            self._jobs.release(entry.family, entry.session)
            self._upkeep_step("the dispatch pump", lambda: self._pump_dispatch(entry.family))

    async def _dispatch_prompt(self, link: AcpLink | None, job: DispatchJob) -> JobEnd:
        entry = job.entry

        if link is None:
            try:
                link = await self._acp_link_for(entry.family, self._status.require(entry.family))
            except ApiError as error:
                return JobEnd(EndKind.FAILED, error=f"{error.code.value}: {error}")

        plan = JobPlan(
            job=entry.session,
            cwd=sandbox_sessions_mount(self._config.sessions_root, entry.family),
            message=job.message,
            deadline_s=DEFAULT_DEADLINE_S,
            prompt_meta={META_TRIGGER: entry.trigger().to_api()},
            cancel_wait_s=self._cancel_wait_s,
        )
        return await run_job(
            link,
            plan,
            on_session=lambda acp: self._ledger_of(entry).note_acp_session(
                entry.caller_family, entry.session, acp
            ),
        )

    def _pump_dispatch(self, family: str) -> None:
        """Start the head of the family's queue while a slot is free (§13 rule 3).

        The link is dialled inside the job, so the pump never waits.
        """
        while (head := self._jobs.head(family)) is not None:
            status = self._status.read(family)

            if status is None:
                return

            job_id, job = head

            if not self._jobs.take_slot(family, job_id, status.max_running_turns):
                return

            self._jobs.pop(family)
            self._start_job(None, job)

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

    # ------------------------------------------------------- sandbox switch

    async def switch_sandbox(self, principal: Principal, request: SwitchRequest) -> dict[str, Any]:
        """Contract 05 §5. `caregiver`'s one call into this service.

        The work runs in its own task, so a `caregiver` that gives up and
        retries joins the run it already started instead of tearing one
        sandbox down twice (§5.3 rule 7).
        """
        check_access(principal, Access.INTERNAL)
        self._check_switch(request)
        key = switch_key(request.family, request.outgoing, request.to)
        found = self._switches.find(key)

        if found is not None:
            return await asyncio.shield(found)

        run = asyncio.create_task(self._run_switch(request), name=f"switch {request.family}")
        run.add_done_callback(functools.partial(self._switch_ended, key))
        self._switches.remember(key, run)

        # A caller that disconnects changes nothing (invariant 4): the
        # switch is the platform's, not the HTTP request's.
        return await asyncio.shield(run)

    def _switch_ended(self, key: SwitchKey, run: SwitchRun) -> None:
        """Forget a run that did not switch. A refusal is not an outcome to
        repeat.

        This is a done-callback of the run, not a handler in the caller. The
        caller can leave before the run ends, and a failed run that no caller
        forgot would answer each repeat with the first refusal.
        """
        if run.cancelled():
            return

        error = run.exception()

        if error is None:
            return

        if self._switches.find(key) is run:
            self._switches.forget(key)

        # A refusal reaches the caller as its answer. Any other error has no
        # reader when the caller left, so it reaches the log.
        if not isinstance(error, ApiError):
            report_failure(run)

    def _check_switch(self, request: SwitchRequest) -> None:
        """Contract 05 §5.3 rule 8, plus the family the document must know.

        Rule 8 refuses a `to` this service would not dial, which §4.2 fixes
        at `ready` and `creating`. `ready` alone would refuse every first
        switch: a replacement stays `creating` until this service dials it.
        """
        family = request.family
        status = self._status.require(family)

        if request.outgoing == request.to:
            raise _switch_refused(family, "from and to name the same sandbox")

        for name, sandbox in (("to", request.to), ("from", request.outgoing)):
            if sandbox is not None and not _in_family(sandbox, family):
                raise _switch_refused(family, f"{name} is not a sandbox of this family")

        if status.startable_by_id(request.to) is None:
            raise _switch_refused(family, "to is not a sandbox this service would dial")

        # CONTRACT-QUESTION: contract 05 §5.3 rule 8 names two refusals of a
        # switch, and neither one is for a status document that states no
        # known kind. A channel needs the kind (`served_kind`), so this reads
        # `to` as a sandbox that this service would not dial: `bad_request`.
        # Without this check the dial answers `forbidden`, the refusal of a
        # door. Another reading changes the code of this one refusal.
        # `caregiver` reads each refusal of a switch in the same way.
        if status.kind is None:
            raise _switch_refused(family, "the status document states no kind")

    async def _run_switch(self, request: SwitchRequest) -> dict[str, Any]:
        """The switch itself (contract 05 §5.3)."""
        family = request.family
        outgoing = request.outgoing
        await self._open_incoming(family, request.to)

        # Rule 1: from this instant every NEW job goes to the incoming
        # sandbox, whatever the outgoing one is still finishing.
        self._switches.pin(family, request.to)
        jobs = self._acp_links.get(outgoing) if outgoing is not None else None
        held = jobs.prompts_in_flight if jobs is not None else 0
        # No session lives here any more (phase 2 plan, row 21), so the
        # answer's `sessions` is 0.
        tally = SwitchTally(running_at_start=held)

        if request.mode is SwitchMode.INTERRUPT:
            # Rule 3. A removal applies at once (invariant 9), so nothing
            # here waits: the outgoing facade stops in `_retire_sandbox`.
            outcome = SwitchOutcome.INTERRUPTED
            tally.aborted = held
        else:
            outcome = await self._drain_jobs(jobs, tally, request.deadline_s)

        await self._retire_sandbox(outgoing)
        return tally.to_api(outcome)

    async def _drain_jobs(
        self, jobs: AcpLink | None, tally: SwitchTally, deadline_s: int
    ) -> SwitchOutcome:
        """§5.3 rule 2 for the prompts in flight on the outgoing facade.

        A job still in flight at the deadline ends when the outgoing link
        stops: its prompt answers `failed` with `channel_lost`.
        """
        held = jobs.prompts_in_flight if jobs is not None else 0

        if jobs is None or held == 0:
            return SwitchOutcome.DRAINED

        try:
            async with asyncio.timeout(deadline_s):
                while jobs.prompts_in_flight > 0:
                    await asyncio.sleep(COLD_START_POLL_S)
        except TimeoutError:
            tally.finished = held - jobs.prompts_in_flight
            tally.aborted = jobs.prompts_in_flight
            return SwitchOutcome.DEADLINE_HIT

        tally.finished = held
        return SwitchOutcome.DRAINED

    async def _open_incoming(self, family: str, sandbox: str) -> None:
        """Contract 05 §5.3 rule 8: the switch is what asks for the handshake.

        The handshake is ACP `initialize` on the incoming sandbox's facade
        (phase 2 plan, row 20). `ready` means it passed and this service
        holds the only supervisor link, so the call that moves a family onto
        a new sandbox is the one place that can prove the sandbox answers.
        It runs before rule 1's pin, so a handshake that cannot pass moves
        no job, drains nothing and destroys nothing: the family keeps
        serving on the outgoing sandbox and a later `caregiver` pass retries.
        """
        status = self._status.require(family)

        try:
            await self._acp_link_for(family, status, sandbox)
        except ApiError as error:
            cause = error.__cause__ if error.__cause__ is not None else error
            raise ApiError(
                ErrorCode.SANDBOX_UNAVAILABLE,
                f"sandbox {sandbox} did not complete its handshake",
                family=family,
                detail={"sandbox": sandbox, "message": str(cause)},
            ) from error

    async def _retire_sandbox(self, outgoing: str | None) -> None:
        """Stop the outgoing facade before the answer (contract 05 §4.4).

        The answer means the outgoing sandbox is free, so nothing of this
        service may still be attached to it when the answer leaves.
        """
        if outgoing is None:
            return

        acp = self._acp_links.pop(outgoing, None)

        if acp is not None:
            await acp.stop()

    # ------------------------------------------------------------ plumbing

    async def _pick_sandbox(self, family: str, status: FamilyStatus) -> str:
        """The sandbox to dial, waiting out a cold start (§5.1).

        A ready one, else one `sbx create` has already made: opening the
        channel and running the handshake is what promotes it, and this
        service is the only process that runs that handshake. The wait
        below still covers a `planned` sandbox, which does not exist yet.
        """
        waited = 0.0
        current = status

        while True:
            ready = self._sandbox_for(family, current)

            if ready is not None:
                return ready.id

            if not current.has_cold_start() or waited >= self._cold_start_wait_s:
                raise ApiError(
                    ErrorCode.SANDBOX_UNAVAILABLE,
                    f"family {family} has no ready sandbox",
                    family=family,
                    detail={"family_state": current.state_text},
                )

            await asyncio.sleep(COLD_START_POLL_S)
            waited += COLD_START_POLL_S
            current = self._status.require(family)

    def _sandbox_for(self, family: str, status: FamilyStatus) -> SandboxInfo | None:
        """The sandbox this family serves on (contract 05 §4.2 rule 1a).

        The pin is the choice. A switch sets it (§5.3 rule 1), and the first
        job of a family with no pin sets it from the document. It is
        dropped only when the document stops offering that sandbox: after a
        crash or a teardown the document is the truth about which sandbox
        serves.

        Re-reading the document per job instead would hand the family to a
        replacement with no switch at all. Contract 05 §4.3 step 5b has
        `caregiver` publish the incoming sandbox BEFORE it calls, so between
        the publish and the answer both sandboxes are `creating` and rule
        1's "the newest `creating` one" names the wrong one.
        """
        pinned = self._switches.pinned(family)
        found = status.startable_by_id(pinned) if pinned is not None else None

        if found is not None:
            return found

        if pinned is not None:
            self._switches.unpin(family)

        chosen = status.startable_sandbox()

        if chosen is not None:
            self._switches.pin(family, chosen.id)

        return chosen

    def _dial_for(self, family: str, status: FamilyStatus, sandbox: str) -> SandboxDial:
        """The sandbox and its env file (contract 05 §4.1).

        A document that publishes no env file path is a fault, not a guess.
        `sbx exec` forwards no host environment, so a command built without
        `--env-file` starts a playpen that can find none of its three
        mounts and answers `fatal` (contract 03 §7.1). Saying so here names
        the cause; dialling anyway would bury it in a log file.
        """
        info = status.sandbox_by_id(sandbox)
        env_file = info.playpen_env if info is not None else ""

        if not env_file:
            self._faults.raise_fault(
                family,
                FaultCode.SANDBOX_START_FAILED,
                f"the status document publishes no supervisor_env for {sandbox}",
                sandbox,
            )

            raise ApiError(
                ErrorCode.SANDBOX_UNAVAILABLE,
                f"family {family} has no playpen env file for {sandbox}",
                family=family,
                detail={"sandbox": sandbox},
            )

        return SandboxDial(sandbox=sandbox, env_file=env_file)

    def _facade_factory(self, dial: SandboxDial) -> Channel:
        family, _, _ = dial.sandbox.rpartition("-s")

        return ExecChannel(
            dial=dial,
            command=self._config.facade_command,
            log_path=self._config.playpen_log(family),
            max_line_bytes=MAX_ACP_LINE_BYTES,
        )

    def _relay_channel(self, dial: SandboxDial) -> Channel:
        family, _, _ = dial.sandbox.rpartition("-s")

        return ExecChannel(
            dial=dial,
            command=self._config.acp_relay_command,
            log_path=self._config.playpen_log(family),
            max_line_bytes=MAX_ACP_LINE_BYTES,
        )

    async def _acp_link_for(
        self, family: str, status: FamilyStatus, sandbox: str | None = None
    ) -> AcpLink:
        """The ready supervisor link to a facade, dialled once.

        The sandbox is the one the family's jobs go to, or the one a switch
        names. `initialize` is the handshake (contract 05 §4.2's `ready`). A
        link that was lost is dialled again. A dial that fails is
        `sandbox_unavailable`.
        """
        if sandbox is None:
            sandbox = await self._pick_sandbox(family, status)

        lock = self._acp_dials.setdefault(sandbox, asyncio.Lock())

        async with lock:
            link = self._acp_links.get(sandbox)

            if link is not None and link.status.state is LinkState.READY:
                return link

            link = AcpLink(
                family,
                self._acp_factory,
                self._faults,
                self._config.state_root,
                self._acp_lost,
                lock_stale_s=self._config.lock_stale_s,
                lock_poll_s=self._config.lock_poll_s,
            )

            try:
                await link.start(self._dial_for(family, status, sandbox))
            except (OrphanFacade, AcpHandshakeError, ChannelClosed) as error:
                raise ApiError(
                    ErrorCode.SANDBOX_UNAVAILABLE,
                    f"family {family} facade did not start",
                    family=family,
                    detail={"sandbox": sandbox},
                ) from error

            self._acp_links[sandbox] = link
            return link

    async def _acp_lost(self, sandbox: str) -> None:
        """Forget a lost link. The next job dials the sandbox again."""
        link = self._acp_links.get(sandbox)

        if link is not None and link.status.state is not LinkState.READY:
            del self._acp_links[sandbox]

    # ------------------------------------------------------------ for tests

    @property
    def faults(self) -> FaultReporter:
        return self._faults

    def acp_link(self, sandbox: str) -> AcpLink | None:
        return self._acp_links.get(sandbox)


def _check_view(principal: Principal) -> None:
    """The view routes and `/busy` take `view-ro` only.

    The read grant alone is not enough: each door that writes holds it too.
    """
    check_access(principal, Access.READ)

    if principal is not Principal.NOTICEBOARD_RO:
        raise ApiError(
            ErrorCode.FORBIDDEN,
            f"{principal.value} is not the view principal",
            detail={"principal": principal.value},
        )


def _check_door(principal: Principal, door: Principal) -> None:
    """Contract 02 §13.4: each door that starts a job serves its own paths.

    The write grant alone is not enough, and neither is being the PEP:
    `door-delegate` holds both and may not create an autonomous session.
    """
    if principal is door:
        return

    raise ApiError(
        ErrorCode.FORBIDDEN,
        f"{principal.value} may not use the routes of {door.value}",
        detail={"principal": principal.value},
    )


def _in_family(sandbox: str, family: str) -> bool:
    """True when this sandbox id belongs to this family (contract 05 §4.1)."""
    return is_sandbox(sandbox) and sandbox.startswith(f"{family}-s")


def _switch_refused(family: str, why: str) -> ApiError:
    """Contract 05 §5.3 rule 8's refusal, with one body shape."""
    return ApiError(ErrorCode.BAD_REQUEST, why, family=family)
