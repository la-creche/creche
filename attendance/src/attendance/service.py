"""The session service (contract 02 §5, contract 03 host side).

Every kind of agent is one call: create a session in a family, run a turn.
This module owns sessions, turns, the journal and the event streams, and
nothing else owns any of them.

Two orderings here are not style. A journal line is written before any reader
sees it (§8.2), and the prompt reaches the journal before `start_turn`
reaches the channel, because a new pi session that dies before its first
assistant message leaves no file on disk at all (§8.2).

`attendance` never calls `managerd`. Everything it knows about a family comes
from the status document `managerd` publishes (contract 05 §2).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
from collections.abc import AsyncGenerator, Coroutine
from typing import Any

from . import dispatch, jobs, outcomes, owui_copy, wire
from .approvals import AuditTail, Gate, GateState, outcome_of
from .atomic import read_json
from .auth import (
    Access,
    Principal,
    check_access,
    check_family_kind,
    check_session_prefix,
    grant_of,
    holder_of,
)
from .branching import FORK_REFUSED, Branch, Decision, Fork, decide
from .channel import Channel, ChannelClosed, SandboxDial
from .clock import now
from .config import Config
from .errors import ApiError, ErrorCode, TurnReason
from .exec_channel import ExecChannel
from .family_status import (
    PEP_UNREACHABLE,
    FamilyStatus,
    SandboxInfo,
    StatusReader,
    check_may_serve,
)
from .faults import FaultCode, FaultReporter
from .ids import SessionPrefix, is_family, is_sandbox, is_session, new_ulid, sha256_hex
from .journal import Journal
from .leases import Grant, Intent, LeaseBook, LeaseReason, Takeover, TurnActivity, busy_error
from .models import (
    Holder,
    JournalLine,
    LineKind,
    Session,
    TriggerKind,
    Turn,
    WriterLease,
)
from .paths import dispatch_file, outcome_file, sandbox_cwd, sandbox_session_dir
from .persona import Persona, prepare
from .playpen_link import (
    ChannelFactory,
    HandshakeError,
    OrphanPlaypen,
    PlaypenFatal,
    PlaypenLink,
    Violation,
)
from .queueing import Slot, TurnQueue, Waiting, slot_for
from .requests import (
    CreateRequest,
    DelegateRequest,
    DispatchRequest,
    JobQuery,
    ListQuery,
    RunTurnRequest,
    SwitchMode,
    SwitchRequest,
    Wait,
    WriterRequest,
)
from .states import SessionKind, TurnState, can_move, is_terminal
from .store import SessionStore
from .streams import Follow, StreamEnd, StreamHub
from .switching import SwitchBook, SwitchOutcome, SwitchTally, switch_key
from .terminal import Exchange, pair
from .turns import LiveTurn, TurnBook, text_delta
from .wire import (
    CHANNEL_IDLE_TTL_OTHER_S,
    EntriesLine,
    FailedLine,
    LogLine,
    OpenedLine,
    ProcessExitLine,
    SettledLine,
)
from .workspace import make_owner_dir, remove_owner_dir, workspace_of

_LOG = logging.getLogger("attendance")

# Contract 03 §10 rule 5: a cold start costs about 10 to 15 seconds plus pi's
# own start. `sandbox_unavailable` is for a failed start, never for one in
# progress (contract 02 §5.1), so the service waits this long for one.
COLD_START_WAIT_S = 60.0
COLD_START_POLL_S = 0.25

# How often the periodic fsync runs. Contract 02 §8.3 asks for at least every
# 2 seconds, and `Journal` applies that interval per handle.
FLUSH_INTERVAL_S = 1.0

# Contract 05 §5.3 rule 6's line, named once so a reader can switch on it.
SWITCH_NOTE = "sandbox_switched"

# Contract 02 §10.5. Entry reads waiting for an answer, across every family.
# One read per lease release, and a dropped channel never answers, so the
# oldest is discarded rather than kept for ever.
MAX_ENTRY_READS = 256

_CURSOR_SEPARATOR = "\x1f"

# A cursor that cannot be read starts the listing over rather than refusing:
# it came from a caller, and no row is lost by beginning again.
_CURSOR_START = (float("-inf"), "", "")

_WARN_LEVELS = frozenset({"warn", "error"})

# Contract 02 §13.1's `error`, for a job whose last turn this service never
# saw settle. `_recover_turns` has already failed it with `channel_lost`.
_RESTART_ERROR = "attendance restarted before this job ended"

# Contract 02 §13.1's `error`, for a firing this service stopped before it
# cost anything. It is what the noticeboard shows in place of a silence, so it
# says the effect, the cause and what happens next, in a person's words.
_PEP_AWAY_MESSAGE = (
    "This job did not run: the policy service was not answering, "
    "so no tool call would have worked. The next firing runs as usual."
)

# Contract 02 §10.2 rules 1 and 2: run the prompt, branch nothing. Every
# caller that has no Open WebUI parent id wants exactly this.
PLAIN_PROMPT = Decision(Branch.PROMPT)

# Contract 04 §6.1's `reason` values that end a turn (contract 02 §4.3's
# table). Every refusal that reaches a LIVE sandbox is NOT here: a deny, a
# timeout, an undeliverable push and a grant revoked inside the gate all hand
# the model a tool error it can report, so the turn goes back to `running`
# and finishes the rest of its job. An abandoned gate has no caller left, so
# there is nothing to report to.
_GATE_FAILURES: dict[str, TurnReason] = {
    "approval_abandoned": TurnReason.APPROVAL_DENIED,
}


class SessionService:
    """Sessions, turns, the journal and the channel to each family."""

    def __init__(
        self,
        config: Config,
        factory: ChannelFactory | None = None,
        cold_start_wait_s: float = COLD_START_WAIT_S,
        owui: owui_copy.ChatApi | None = None,
        channel_idle_ttl_s: float = CHANNEL_IDLE_TTL_OTHER_S,
    ) -> None:
        self._config = config
        self._journal = Journal(config.sessions_root)
        self._store = SessionStore(config.sessions_root, self._journal)
        self._hub = StreamHub(self._journal)
        self._leases = LeaseBook(config.state_root)
        self._faults = FaultReporter(config.state_root)
        self._status = StatusReader(config.state_root)
        self._gates = AuditTail(config.state_root)
        self._turns = TurnBook()
        self._switches = SwitchBook()
        self._factory = factory if factory is not None else self._exec_factory
        self._cold_start_wait_s = cold_start_wait_s
        # Contract 03 §10 rule 3, for thin and autonomous families. A link
        # of an attended family ignores it (rule 2).
        self._channel_idle_ttl_s = channel_idle_ttl_s
        self._queue = TurnQueue()
        self._dispatches = dispatch.DispatchLedger(config.state_root)
        # Contract 02 §10.4 rule 4. `owui` is the seam a test fills; the
        # service builds its own client from the configuration otherwise, and
        # an unset base URL or key file leaves the feature off.
        made = (
            owui if owui is not None else owui_copy.read_api(config.owui_url, config.owui_key_file)
        )
        self._owui = owui_copy.ChatCopy(made, config.owui_folder_id, self._keep_owui_chat)
        self._forks: dict[tuple[str, str, str], Fork] = {}
        self._sessions: dict[tuple[str, str], Session] = {}
        self._links: dict[str, PlaypenLink] = {}
        # What `_report_audit` last published, so a once-a-second poll
        # rewrites no file while nothing changes.
        self._audit_fault: str | None = None
        self._upkeep: asyncio.Task[None] | None = None
        self._pre_starts: set[asyncio.Task[None]] = set()
        self._followups: set[asyncio.Task[None]] = set()
        # Contract 02 §10.5. One entry read in flight per request id, so the
        # answer knows which session it belongs to. An answer for a request
        # this service never sent is dropped (contract 03 §5.8 rule 4).
        self._entry_reads: dict[str, tuple[str, str]] = {}
        self._entry_tasks: set[asyncio.Task[None]] = set()

    # ---------------------------------------------------------------- life

    def start(self) -> None:
        """Rebuild every index from disk (invariant 1).

        A session always exists once created. It survives a restart of this
        service, a sandbox replacement and a deploy, so the only truth at
        startup is what is on the platter.
        """
        for record in self._store.scan():
            key = (record.family, record.session)
            self._sessions[key] = record
            last_seq = max(record.journal_seq, self._journal.tail_seq(*key))
            record.journal_seq = last_seq
            self._journal.register(record.family, record.session, last_seq)
            self._recover_turns(record)

        # The fault file holds this writer's current open faults, and a fresh
        # process has observed none (contract 05 §3.3.1). Without this a
        # turn-blocking fault would survive forever: it stops new turns, so
        # nothing dials the sandbox again and nothing can ever clear it.
        for family in self._status.families():
            self._faults.clear_all(family)

        self._sweep_jobs()

    def _sweep_jobs(self) -> None:
        """End the autonomous jobs that finished while this service was down.

        Contract 02 §13 rule 7 ends a job when the last turn settles, and
        that moment can fall on either side of a restart: `_recover_turns`
        has just failed every turn that was running, and a crash between
        writing the outcome record and deleting the session leaves one too.
        Without this sweep those sessions would never end, because nothing
        else ever settles a turn of theirs again.
        """
        finished = [
            record
            for record in list(self._sessions.values())
            if record.kind is SessionKind.AUTONOMOUS and self._job_over(record)
        ]

        for record in finished:
            turns = [live.record for live in self._turns.of_session(record.family, record.session)]
            # The turn's own reason says more than "we restarted": a queued
            # turn reads `queue_lost` and a running one `channel_lost`.
            ending = turns[-1].reason
            error = ending.value if ending is not None else _RESTART_ERROR
            outcomes.write(self._config.state_root, outcomes.build(record, turns, error))
            self._forget_session(record.family, record.session)
            self._store.delete(record.family, record.session)
            remove_owner_dir(self._config.work_root, record.session)

    def _job_over(self, record: Session) -> bool:
        """Contract 02 §13 rule 7, read over a session's live turns."""
        turns = self._turns.of_session(record.family, record.session)

        if not turns:
            return False

        return all(is_terminal(live.record.state) for live in turns)

    def start_upkeep(self) -> None:
        """Begin the periodic fsync and the Open WebUI writer.

        Call it inside the event loop. Both own a task, and a task needs a
        running loop, which `start` has no right to assume.
        """
        self._owui.start()

        if self._upkeep is None:
            self._upkeep = asyncio.create_task(self._flush_loop())

    async def _flush_loop(self) -> None:
        """Contract 02 §8.3: fsync at least every 2 seconds.

        A terminal line syncs on its own, so this covers the long middle of a
        turn, where a crash would otherwise lose the tail of an answer.
        """
        while True:
            await asyncio.sleep(FLUSH_INTERVAL_S)
            self._journal.flush_due()
            self.read_gates()

    def read_gates(self) -> None:
        """Contract 04 §8.6. Show a turn that waits for a phone tap.

        The upkeep loop calls this. It is public so a test can drive it
        without a clock, and so an operator's noticeboard sees the state within
        `FLUSH_INTERVAL_S` of the PEP writing its `pending` record.
        """
        for gate in self._gates.poll():
            self._apply_gate(gate)

        self._report_audit(self._gates.unreadable())

    def _report_audit(self, message: str | None) -> None:
        """Contract 05 §3.3's `audit_unreadable`.

        One audit file serves every family, so an unreadable one faults them
        all. Only a CHANGE is published: this runs once a second, and the
        fault file holds current faults, not an event log (§3.3.1 rule 1).
        """
        if message == self._audit_fault:
            return

        self._audit_fault = message

        for family in self._status.families():
            if message is None:
                self._faults.clear(family, FaultCode.AUDIT_UNREADABLE)
                continue

            self._faults.raise_fault(family, FaultCode.AUDIT_UNREADABLE, message)

    async def owui_drained(self) -> None:
        """Wait until the Open WebUI copy is level with every settled turn.

        No turn calls this (contract 02 §10.4 rule 5: a turn never waits for a
        copy). It is for a reader that must see the record AFTER the write,
        which is what every test of the copy is."""
        await self._owui.drained()

    async def close(self) -> None:
        """Shut every channel, then release the journal handles."""
        if self._upkeep is not None:
            self._upkeep.cancel()
            self._upkeep = None

        await self._owui.close()

        # A pre-start is an optimisation with nothing waiting on it, so it is
        # dropped rather than drained (contract 03 §4.7 rule 10).
        for pre_start in list(self._pre_starts):
            pre_start.cancel()

        self._pre_starts.clear()

        # A terminal read is the same kind of thing: the journal already
        # holds every turn, and the next lease release reads again from the
        # cursor this one did not move (contract 02 §10.5 rule 3).
        for read in list(self._entry_tasks):
            read.cancel()

        self._entry_tasks.clear()

        # What a settled turn asks for next: start the queue's head, delete a
        # finished job. Contract 02 §13.3 ends the queue at a restart anyway,
        # and a delete that never ran is swept by `_sweep_jobs` at the next
        # start, which writes a second outcome record rather than losing one.
        for followup in list(self._followups):
            followup.cancel()

        self._followups.clear()
        # A drain waits for turns, and a shutdown may not wait for a drain.
        # Nothing about a switch is on disk, so the restart converges on the
        # status document (contract 05 §5.3 rule 1).
        self._switches.cancel_all()

        for live in self._turns.all_turns():
            task = live.deadline_task
            live.deadline_task = None

            if task is not None:
                task.cancel()

        for link in list(self._links.values()):
            with contextlib.suppress(ChannelClosed, ValueError):
                await link.send(wire.shutdown())

            await link.close()

        self._links.clear()
        self._journal.close_all()

    # ------------------------------------------------------------ sessions

    def create_or_find(
        self, principal: Principal, request: CreateRequest
    ) -> tuple[dict[str, Any], bool]:
        """Contract 02 §5.1. One call serves both (invariant 3)."""
        check_access(principal, Access.WRITE)
        status = self._status.require(request.family)
        check_family_kind(principal, request.family, status.kind)
        check_session_prefix(principal, request.family, request.session)
        check_may_serve(status)

        found = self._sessions.get((request.family, request.session))

        if found is not None:
            found.labels.update(request.labels)
            found.updated_at = now()
            self._store.save(found)
            self._pre_start(request.family, request.session, status)
            return self._session_body(found), False

        body = self._create(request, status)
        self._pre_start(request.family, request.session, status)
        return body, True

    def get_session(
        self, principal: Principal, family: str, session: str, turns_wanted: int
    ) -> dict[str, Any]:
        """Contract 02 §5.3."""
        record = self._read_access(principal, family, session)
        body = self._session_body(record)
        recent = self._turns.of_session(family, session)[-turns_wanted:] if turns_wanted else []
        body["turns"] = [live.record.to_api() for live in recent]
        return body

    def list_sessions(self, principal: Principal, query: ListQuery) -> dict[str, Any]:
        """Contract 02 §5.2. Newest activity first, keyset paged."""
        check_access(principal, Access.READ)
        allowed = grant_of(principal).kind
        rows = [row for row in self._sessions.values() if self._matches(row, query, allowed)]
        rows.sort(key=_list_order)
        page, next_cursor = _page(rows, query.cursor, query.limit)

        return {
            "sessions": [self._session_body(record) for record in page],
            "next_cursor": next_cursor,
        }

    async def delete_session(self, principal: Principal, family: str, session: str) -> None:
        """Contract 02 §5.8. Every running turn aborts, then the directory goes."""
        self._write_access(principal, family, session)

        # A queued turn counts here as well as a running one: a delete must
        # leave nothing that could reach a slot afterwards.
        for live in self._turns.blocking_in_session(family, session):
            await self._abort_on_channel(live)
            self._settle(live, TurnState.ABORTED, TurnReason.USER_STOPPED, "session deleted")

        await self._remove_session(family, session)

    async def _remove_session(self, family: str, session: str) -> None:
        """Contract 02 §5.8's removal, with no turn left to stop."""
        await self._stop_process_of(session)
        self._forget_session(family, session)
        self._store.delete(family, session)

        # Contract 02 §5.8's last row and §12.1 rule 4. The work directory is
        # keyed by the session that OWNS it, so deleting a job never touches
        # the chat's, and deleting the chat takes it.
        remove_owner_dir(self._config.work_root, session)

    def _forget_session(self, family: str, session: str) -> None:
        """Drop every in-memory trace of one session. The files are separate."""
        self._hub.drop_all(family, session)
        self._turns.drop_session(family, session)
        self._queue.drop_session(family, session)
        self._owui.forget(family, session)
        self._leases.forget(family, session)
        self._sessions.pop((family, session), None)

    def take_writer(
        self,
        principal: Principal,
        family: str,
        session: str,
        request: WriterRequest,
        door_instance: str,
    ) -> dict[str, Any]:
        """Contract 02 §5.9 and §7.3."""
        self._write_access(principal, family, session)
        holder = holder_of(principal, family, session)

        # The token names the door. A body that claims another door is a bug
        # in that door, not a way to write as one (contract 02 §3.1).
        if request.holder is not None and request.holder is not holder:
            raise ApiError(
                ErrorCode.FORBIDDEN,
                f"{principal.value} cannot take the lease as {request.holder.value}",
                family=family,
                session=session,
            )

        takeover = Takeover.FORCE if request.force else Takeover.POLITE
        grant = self._take_lease(family, session, holder, door_instance, takeover, request.intent)
        return grant.lease.to_api()

    def release_writer(
        self, principal: Principal, family: str, session: str, door_instance: str
    ) -> dict[str, Any]:
        """Contract 02 §5.10. A door that is done gives the lease back now.

        Letting the 60-second TTL run out instead is what made invariant 3's
        "continue it in the terminal" cost a minute after every chat turn.
        """
        self._write_access(principal, family, session)
        self._refuse_while_in_flight(family, session, "the writer lease")
        holder = holder_of(principal, family, session)

        if self._leases.release(family, session, holder, door_instance) is None:
            return {"released": False}

        self._journal_writer(family, session, holder, LeaseReason.RELEASED)

        if holder is Holder.TUI:
            self._read_terminal(family, session)

        return {"released": True}

    async def release_process(
        self, principal: Principal, family: str, session: str, door_instance: str
    ) -> dict[str, Any]:
        """Contract 02 §5.11. Close the held-open pi process, keep the session.

        This closes a process and never a session (invariant 1). Without it a
        terminal waits out `pi_idle_ttl_s` behind pi's own fence.
        """
        self._write_access(principal, family, session)
        self._require_lease(principal, family, session, door_instance)
        self._refuse_while_in_flight(family, session, "the pi process")
        return {"released": await self._stop_process_of(session)}

    # --------------------------------------------------------------- turns

    async def run_turn(
        self,
        principal: Principal,
        family: str,
        session: str,
        request: RunTurnRequest,
        door_instance: str,
    ) -> LiveTurn:
        """Contract 02 §5.4. Returns the turn; the caller picks the wait mode."""
        check_access(principal, Access.WRITE)
        status = self._status.require(family)
        check_family_kind(principal, family, status.kind)
        check_may_serve(status)
        record = self._require_session(family, session)
        self._set_trigger(record, status.kind, request)

        if request.idempotency_key is not None:
            repeat = self._repeat_of(family, session, request)

            if repeat is not None:
                return repeat

        holder = holder_of(principal, family, session)
        self._take_lease(family, session, holder, door_instance)
        self._refuse_second_turn(family, session)

        persona = prepare(request.persona_text)
        # Contract 02 §10.2, decided before the turn is minted: rule 1 asks
        # whether the session has turns yet, and minting one changes that.
        branch = self._branch_for(record, request)

        # Contract 05 §3.3 rule 6's stated cost, paid here instead.
        if self._pep_is_away(status):
            return self._refuse_toolless_job(record, request, persona)

        # Contract 02 §13 rules 2 and 3. Only an autonomous family can queue.
        if self._slot_for(status) is Slot.QUEUE:
            return self._queue_turn(record, request, persona, branch)

        sandbox = await self._pick_sandbox(family, status)
        # Refuse a document with no env file before a turn exists. Found
        # later, it would leave the turn running with nothing to run it.
        self._dial_for(family, status, sandbox)
        live = self._open_turn(record, request, sandbox, persona, status)
        start = self._send_start(live, request, persona, status, sandbox, branch)

        # Contract 02 §5.4: `accepted` answers at once. The dial can be a cold
        # start of 10 to 15 seconds (contract 03 §10 rule 5), and after an
        # idle close (rule 3) it usually is. `_send_start` settles the turn
        # itself on every failure, so nothing is lost by not waiting.
        if request.wait is Wait.ACCEPTED:
            self._run_later(start)
            return live

        await start
        return live

    async def steer(
        self,
        principal: Principal,
        family: str,
        session: str,
        turn: str,
        message: str,
        door_instance: str,
    ) -> None:
        """Contract 02 §5.6. Only the lease holder steers."""
        self._write_access(principal, family, session)
        self._require_lease(principal, family, session, door_instance)
        live = self._require_turn(family, session, turn)

        if live.record.state is not TurnState.RUNNING:
            raise ApiError(
                ErrorCode.BAD_REQUEST,
                "a turn that is not running cannot be steered",
                family=family,
                session=session,
                turn=turn,
            )

        self._leases.renew(family, session, turn)
        await self._send_to(live.record.sandbox, family, wire.steer(session, turn, message))

    async def stop_turn(
        self,
        principal: Principal,
        family: str,
        session: str,
        turn: str,
        reason: str,
        door_instance: str,
    ) -> dict[str, Any]:
        """Contract 02 §5.7. The pi process stays open for the next turn."""
        self._write_access(principal, family, session)
        self._require_lease(principal, family, session, door_instance)
        live = self._require_turn(family, session, turn)

        if not live.is_active and live.record.state is not TurnState.QUEUED:
            return live.record.to_api()

        self._leases.renew(family, session, turn)
        await self._abort_on_channel(live)
        self._settle(live, TurnState.ABORTED, TurnReason.USER_STOPPED, reason)
        return live.record.to_api()

    def stream(
        self,
        principal: Principal,
        family: str,
        session: str,
        from_seq: int = 0,
        turn: str | None = None,
        follow: Follow = Follow.KEEP_OPEN,
        end: StreamEnd = StreamEnd.NEVER,
    ) -> AsyncGenerator[JournalLine, None]:
        """Contract 02 §5.5. Attaching is a read and never takes the lease."""
        self._read_access(principal, family, session)
        return self._hub.stream(family, session, from_seq, turn, follow, end)

    async def wait_settled(self, live: LiveTurn) -> dict[str, Any]:
        """Contract 02 §5.4's `wait=settled` body."""
        await live.done.wait()

        return {
            "turn": live.record.turn,
            "state": live.record.state.value,
            "text": live.answer(),
            "usage": live.record.usage.to_api(),
            "journal_seq": self._seq_of(live.record.family, live.record.session),
        }

    def accepted_body(self, live: LiveTurn) -> dict[str, Any]:
        """Contract 02 §5.4's `wait=accepted` body."""
        return {
            "turn": live.record.turn,
            "state": live.record.state.value,
            "journal_seq": self._seq_of(live.record.family, live.record.session),
        }

    # ------------------------------------------------------------ thin jobs

    async def run_delegate(
        self, principal: Principal, request: DelegateRequest, door_instance: str
    ) -> dict[str, Any]:
        """Contract 04 §7.3 and contract 02 §12. One session, one turn, gone.

        The PEP is the one caller. A job leaves the audit record and nothing
        else (invariant 15), so the answer has to travel in this response:
        there is no journal left to fetch it from afterwards.
        """
        check_access(principal, Access.WRITE)
        self._check_delegate_door(principal)
        family = request.target_family
        status = self._status.require(family)
        # The delegate door's grant is `thin`, so a family of any other kind
        # is refused here before a session exists (contract 02 §3.1).
        check_family_kind(principal, family, status.kind)
        check_may_serve(status)

        owner = request.claimed_session_id
        self._prepare_work_dir(family, owner)
        session = jobs.new_job_session()
        self.create_or_find(
            principal, CreateRequest(family=family, session=session, owner_session=owner)
        )

        try:
            return await self._run_job(principal, request, session, status, door_instance)
        finally:
            # §12 rules 5 and 6. The session goes whatever happened, including
            # a refusal on the way to the turn: nothing may outlive the job.
            await self.delete_session(principal, family, session)

    async def _run_job(
        self,
        principal: Principal,
        request: DelegateRequest,
        session: str,
        status: FamilyStatus,
        door_instance: str,
    ) -> dict[str, Any]:
        """One turn at the family's job timeout, then the answer."""
        live = await self.run_turn(
            principal,
            request.target_family,
            session,
            RunTurnRequest(
                prompt=request.message,
                wait=Wait.SETTLED,
                deadline_s=jobs.job_timeout_s(status.job_timeout_s),
                delegation=jobs.Delegation(
                    delegation_id=request.delegation_id,
                    caller_family=request.caller_family,
                    caller_session=request.claimed_session_id,
                ),
            ),
            door_instance,
        )
        await live.done.wait()
        return jobs.answer(session, live.record, live.answer())

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

    async def run_dispatch(
        self, principal: Principal, request: DispatchRequest, door_instance: str
    ) -> dict[str, Any]:
        """Contract 02 §13.4.1. One family enqueues a job in another.

        It never waits for the job. The answer is the session id, so a lead
        can start four workers and carry on, and `read_jobs` is what says
        later what became of each one.
        """
        check_access(principal, Access.WRITE)
        self._check_dispatch_door(principal)
        target = request.target_family
        status = self._status.require(target)
        # The dispatch door's grant is `autonomous`, so a thin or attended
        # target is refused before a session exists (contract 02 §3.1).
        check_family_kind(principal, target, status.kind)
        check_may_serve(status)
        self._check_dispatchable(status)

        repeat = self._repeat_dispatch(request)

        if repeat is not None:
            return repeat

        # Rule 4: the queue is the TARGET's, and a full one refuses before
        # any session exists. `run_turn` checks it again under the same lock.
        self._queue.check_room(target)
        entry = self._open_dispatch(principal, request)

        return await self._start_dispatch(principal, request, entry, door_instance)

    def read_jobs(self, principal: Principal, query: JobQuery) -> dict[str, Any]:
        """Contract 02 §13.4.2. The jobs ONE family dispatched, newest first.

        The scope is the ledger directory of `caller_family`, so an unknown
        session id and another family's session id are both simply absent
        (rule 2). No id ever leaks.
        """
        check_access(principal, Access.WRITE)
        self._check_dispatch_door(principal)

        if query.session is not None:
            one = self._dispatches.read(query.caller_family, query.session)
            found = [one] if one is not None else []
        else:
            found = self._dispatches.list(query.caller_family, query.since, query.limit)

        return {"jobs": [self._job_view(entry) for entry in found]}

    def _job_view(self, entry: dispatch.DispatchEntry) -> dict[str, Any]:
        """One job, live or finished. The live state comes from this
        service's own books, and the outcome from the record on disk."""
        record = self._sessions.get((entry.family, entry.session))
        state = None if record is None else self._turns.session_state(entry.family, entry.session)

        return dispatch.job_view(entry, state, self._outcome_of(entry))

    def _outcome_of(self, entry: dispatch.DispatchEntry) -> dict[str, Any] | None:
        """§13.1's record for a finished job, or None while it runs.

        Rule 4: a noted outcome whose file cannot be read still means the job
        ended. `status_of` answers `ended` from the id alone.
        """
        if entry.outcome_id is None:
            return None

        return read_json(outcome_file(self._config.state_root, entry.family, entry.outcome_id))

    def _repeat_dispatch(self, request: DispatchRequest) -> dict[str, Any] | None:
        """Contract 02 §13.4.4. An hour of deduplication, per caller family.

        A repeat with a different message is refused rather than answered
        with a job that ran a different prompt.
        """
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

        state = self._turns.session_state(found.family, found.session)
        return {"session_id": found.session, "status": state.value, "created": False}

    def _open_dispatch(
        self, principal: Principal, request: DispatchRequest
    ) -> dispatch.DispatchEntry:
        """Create the session and its ledger entry, in that order (rule 5).

        The entry is written before the turn starts, so a job that ends at
        once still has somewhere to note its outcome id.
        """
        session = f"{SessionPrefix.AUTO.value}{new_ulid()}"
        self.create_or_find(
            principal,
            CreateRequest(
                family=request.target_family,
                session=session,
                owner_session=request.claimed_session_id,
            ),
        )
        entry = dispatch.DispatchEntry(
            session=session,
            caller_family=request.caller_family,
            family=request.target_family,
            enqueued_at=now(),
            delegation_id=request.delegation_id,
            chain=request.chain,
            caller_session=request.claimed_session_id,
            idempotency_key=request.idempotency_key,
        )
        self._dispatches.write(entry)
        return entry

    async def _start_dispatch(
        self,
        principal: Principal,
        request: DispatchRequest,
        entry: dispatch.DispatchEntry,
        door_instance: str,
    ) -> dict[str, Any]:
        """Run the one turn and answer at once (rule 1).

        `message` becomes the prompt as DATA. The calling model wrote it, so
        nothing here reads it as an instruction (invariant 14).
        """
        try:
            live = await self.run_turn(
                principal,
                request.target_family,
                entry.session,
                RunTurnRequest(
                    prompt=request.message,
                    wait=Wait.ACCEPTED,
                    trigger=entry.trigger(),
                ),
                door_instance,
            )
        except ApiError:
            # Rule 5: a refusal leaves no id for `job_status` to report.
            await self._remove_session(request.target_family, entry.session)
            self._forget_dispatch(entry)
            raise

        return {
            "session_id": entry.session,
            "status": live.record.state.value,
            "created": True,
        }

    def _forget_dispatch(self, entry: dispatch.DispatchEntry) -> None:
        """Remove a ledger entry whose turn never started (rule 5)."""
        dispatch_file(self._config.state_root, entry.caller_family, entry.session).unlink(
            missing_ok=True
        )

    def _check_dispatch_door(self, principal: Principal) -> None:
        """Contract 02 §13.4: the dispatch door serves these two paths.

        The write grant alone is not enough, and neither is being the PEP:
        `door-delegate` holds both and may not create an autonomous session.
        """
        if principal is Principal.DOOR_DISPATCH:
            return

        raise ApiError(
            ErrorCode.FORBIDDEN,
            f"{principal.value} is not the dispatch door",
            detail={"principal": principal.value},
        )

    def _note_dispatch_outcome(self, record: Session, outcome_id: str) -> None:
        """Contract 02 §13.4.3. Point the caller's ledger entry at the record.

        It runs while the outcome is written and before the session is
        deleted, because the session's trigger is the only thing that says
        which family enqueued this job.
        """
        trigger = record.trigger

        if trigger is None or trigger.kind is not TriggerKind.DISPATCH or trigger.name is None:
            return

        self._dispatches.note_outcome(trigger.name, record.session, outcome_id)

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
        """Contract 05 §5. `managerd`'s one call into this service.

        The work runs in its own task, so a `managerd` that gives up and
        retries joins the run it already started instead of tearing one
        sandbox down twice (§5.3 rule 7).
        """
        check_access(principal, Access.INTERNAL)
        self._check_switch(request)
        key = switch_key(request.family, request.outgoing, request.to)
        found = self._switches.find(key)

        if found is not None:
            return await asyncio.shield(found)

        run = asyncio.create_task(self._run_switch(request))
        self._switches.remember(key, run)

        try:
            # A caller that disconnects changes nothing (invariant 4): the
            # switch is the platform's, not the HTTP request's.
            return await asyncio.shield(run)
        except Exception:
            # A refusal is not an outcome to repeat. `CancelledError` is not
            # caught here: it means this caller went away, not that the run
            # inside the shield failed.
            self._switches.forget(key)
            raise

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

    async def _run_switch(self, request: SwitchRequest) -> dict[str, Any]:
        """The switch itself. Every session survives it (§5.3 rule 4)."""
        family = request.family
        outgoing = request.outgoing
        await self._open_incoming(family, request.to)

        # Rule 1: from this instant every NEW turn goes to the incoming
        # sandbox, whatever the outgoing one is still finishing.
        self._switches.pin(family, request.to)
        moving = self._turns_on(family, outgoing)
        tally = SwitchTally(
            running_at_start=len(moving),
            sessions=len(self._sessions_of(family)),
        )
        self._note_switch(request)

        if request.mode is SwitchMode.INTERRUPT:
            outcome = await self._interrupt_turns(moving, tally, request.reason)
        else:
            outcome = await self._drain_turns(moving, tally, request.deadline_s)

        await self._retire_sandbox(outgoing)
        return tally.to_api(outcome)

    async def _open_incoming(self, family: str, sandbox: str) -> None:
        """Contract 05 §5.3 rule 8: the switch is what asks for the handshake.

        `ready` means the handshake passed and this service holds the only
        channel, so the call that moves a family onto a new sandbox is the
        one place that can prove the sandbox answers. It runs before rule
        1's pin, so a handshake that cannot pass moves no turn, drains
        nothing and destroys nothing: the family keeps serving on the
        outgoing sandbox and a later `managerd` pass retries.

        No pi process starts here. §5.3 rule 5 still holds — the first turn
        on the new sandbox pays its own cold start.
        """
        status = self._status.require(family)

        try:
            await self._link_for(family, status, sandbox)
        except (OrphanPlaypen, PlaypenFatal, HandshakeError, ChannelClosed) as error:
            raise ApiError(
                ErrorCode.SANDBOX_UNAVAILABLE,
                f"sandbox {sandbox} did not complete its handshake",
                family=family,
                detail={"sandbox": sandbox, "message": str(error)},
            ) from error

    async def _drain_turns(
        self, moving: list[LiveTurn], tally: SwitchTally, deadline_s: int
    ) -> SwitchOutcome:
        """§5.3 rule 2. Running turns finish, or the deadline ends them."""
        try:
            await asyncio.wait_for(
                asyncio.gather(*(live.done.wait() for live in moving)), deadline_s
            )
        except TimeoutError:
            await self._abort_moving(moving, tally, TurnReason.SANDBOX_LOST, "drain deadline")
            return SwitchOutcome.DEADLINE_HIT

        tally.finished = len(moving)
        return SwitchOutcome.DRAINED

    async def _interrupt_turns(
        self, moving: list[LiveTurn], tally: SwitchTally, reason: str
    ) -> SwitchOutcome:
        """§5.3 rule 3. A removal applies at once (invariant 9).

        A turn that still holds the removed reach must not be allowed to
        finish, so nothing here waits for anything.
        """
        await self._abort_moving(moving, tally, TurnReason.PERMISSION_REMOVED, reason)
        return SwitchOutcome.INTERRUPTED

    async def _abort_moving(
        self,
        moving: list[LiveTurn],
        tally: SwitchTally,
        why: TurnReason,
        message: str,
    ) -> None:
        """End what is left on the outgoing sandbox and count both groups."""
        for live in moving:
            if not live.is_active:
                tally.finished += 1
                continue

            await self._abort_on_channel(live)
            self._settle(live, TurnState.ABORTED, why, message)
            tally.aborted += 1

    async def _retire_sandbox(self, outgoing: str | None) -> None:
        """Close every held-open process, then the channel (contract 05 §4.4).

        The answer means the outgoing sandbox is free, so nothing of this
        service may still be attached to it when the answer leaves. No pi
        process moves (§5.3 rule 5): each one dies here and the first turn on
        the new sandbox pays a cold start.
        """
        link = self._link_of(outgoing)

        if link is None or outgoing is None:
            return

        for session in link.opened_sessions():
            with contextlib.suppress(ChannelClosed, ValueError):
                await link.send(wire.stop_process(session))

        with contextlib.suppress(ChannelClosed, ValueError):
            await link.send(wire.shutdown())

        await link.close()
        self._links.pop(outgoing, None)

    def _turns_on(self, family: str, sandbox: str | None) -> list[LiveTurn]:
        """The family's running turns that the outgoing sandbox serves."""
        if sandbox is None:
            return []

        running = self._turns.active_in_family(family)
        return [live for live in running if live.record.sandbox == sandbox]

    def _sessions_of(self, family: str) -> list[str]:
        return [session for held, session in self._sessions if held == family]

    def _note_switch(self, request: SwitchRequest) -> None:
        """§5.3 rule 6. One `note` line per session, so a UI can say why."""
        body = {
            "note": SWITCH_NOTE,
            "from": request.outgoing,
            "to": request.to,
            "mode": request.mode.value,
            "reason": request.reason,
        }

        for session in self._sessions_of(request.family):
            self._append(request.family, session, LineKind.NOTE, None, dict(body))

    # ----------------------------------------------------------- internals

    def _create(self, request: CreateRequest, status: FamilyStatus) -> dict[str, Any]:
        moment = now()
        record = Session(
            family=request.family,
            session=request.session,
            kind=status.kind,
            created_at=moment,
            updated_at=moment,
            title=request.title,
            labels=dict(request.labels),
            owner_session=request.owner_session,
        )
        self._sessions[(record.family, record.session)] = record
        self._store.create(record)
        body = self._session_body(record)
        self._append(record.family, record.session, LineKind.SESSION_CREATED, None, dict(body))
        self._store.save(record)
        return self._session_body(record)

    def _open_turn(
        self,
        record: Session,
        request: RunTurnRequest,
        sandbox: str,
        persona: Persona,
        status: FamilyStatus,
    ) -> LiveTurn:
        """Mint the turn and journal it BEFORE the channel hears about it."""
        live = self._mint_turn(record, request, persona, TurnState.RUNNING)
        self._begin_turn(live, record, request, persona, status, sandbox)
        return live

    def _mint_turn(
        self,
        record: Session,
        request: RunTurnRequest,
        persona: Persona,
        state: TurnState,
    ) -> LiveTurn:
        """One turn record, indexed, counted, and not yet journalled."""
        turn = Turn(
            turn=new_ulid(),
            session=record.session,
            family=record.family,
            state=state,
            started_at=now(),
            # A queued turn has no sandbox yet: nothing has dialled one, and
            # §4.4 reads `sandbox` as "the sandbox that served it".
            sandbox="",
            deadline_s=request.deadline_s,
            idempotency_key=request.idempotency_key,
            owui=request.owui,
            persona_truncated=persona.truncated,
            prompt_sha256=sha256_hex(request.prompt),
        )
        live = LiveTurn(record=turn, prompt=request.prompt)
        self._turns.add(live)
        record.turns_total += 1
        record.labels.update(request.labels)
        return live

    def _begin_turn(
        self,
        live: LiveTurn,
        record: Session,
        request: RunTurnRequest,
        persona: Persona,
        status: FamilyStatus,
        sandbox: str,
    ) -> None:
        """Move a turn to `running` and journal it. A queued turn ends here too."""
        turn = live.record
        turn.state = TurnState.RUNNING
        turn.sandbox = sandbox
        turn.started_at = now()

        record.sandbox = sandbox
        record.persona_hash = persona.digest or None

        line = self._append(
            record.family,
            record.session,
            LineKind.TURN_STARTED,
            turn.turn,
            {
                "prompt": request.prompt,
                "sandbox": sandbox,
                "deadline_s": request.deadline_s,
                "persona_hash": persona.digest or None,
                # Contract 02 §5.1: a turn is served from a status document
                # over 90 seconds old and says so here. Refusing would make a
                # `managerd` restart a chat outage, which invariant 1 forbids.
                "status_stale": status.is_stale(),
            },
        )
        live.first_seq = line.journal_seq if line.journal_seq is not None else 0

        if persona.truncated:
            self._append(
                record.family,
                record.session,
                LineKind.NOTE,
                turn.turn,
                {"note": "persona_truncated", "cap_bytes": wire.MAX_PERSONA_BYTES},
            )

        self._leases.renew(record.family, record.session, turn.turn)
        self._store.save_turn(turn)
        self._store.save(record)

    # ----------------------------------------------- the PEP is not answering

    def _pep_is_away(self, status: FamilyStatus) -> bool:
        """Contract 05 §3.3's `pep_unreachable`, for an autonomous family only.

        An attended turn still runs. A person asked, and an answer without
        tools is still an answer, which is exactly why the fault carries
        `blocks_turns: false`. A thin job is a call already blocking at the
        PEP, so it cannot arrive while the PEP is away.

        An autonomous firing is the one case that is all cost. Its whole
        purpose is tool calls, nobody is waiting for its text, and contract 05
        §3.3 rule 6 names what happens without this check: the turn runs to
        the end and LiteLLM bills it.
        """
        if status.kind is not SessionKind.AUTONOMOUS:
            return False

        return status.has_fault(PEP_UNREACHABLE)

    def _refuse_toolless_job(
        self,
        record: Session,
        request: RunTurnRequest,
        persona: Persona,
    ) -> LiveTurn:
        """End the firing at once, with a record that says why.

        The turn is minted and failed rather than refused with an error,
        because a refusal before the mint leaves the noticeboard a silence:
        contract 02 §13.1's outcome record is built from a session's turns,
        and a session with no turn produces none.

        The reason is `pep_unreachable` (contract 02 §14), the same word
        contract 05 §3.3's fault and contract 04's `/call` failure use.
        `permission_removed` would send a reader looking for a grant nobody
        took away.
        """
        live = self._mint_turn(record, request, persona, TurnState.RUNNING)
        self._settle(live, TurnState.FAILED, TurnReason.PEP_UNREACHABLE, _PEP_AWAY_MESSAGE)
        return live

    # ------------------------------------------------------------- the queue

    def _branch_for(self, record: Session, request: RunTurnRequest) -> Decision:
        """Contract 02 §10.2. A turn with no Open WebUI parent branches nothing."""
        refs = request.owui

        if refs is None:
            return PLAIN_PROMPT

        return decide(record.owui_map, refs.parent_id, record.turns_total)

    async def _retry_without_fork(self, live: LiveTurn, message: FailedLine) -> bool:
        """Contract 03 §4.1 and contract 02 §10.2's last paragraph.

        pi refused the fork target, so the turn runs as a plain prompt and
        the divergence reaches the journal instead of the person.
        """
        fork = self._forks.pop(live.key, None)

        if fork is None:
            return False

        self._append(
            live.record.family,
            live.record.session,
            LineKind.BRANCH_FALLBACK,
            live.record.turn,
            {"wanted_entry": _wanted_entry(fork), "reason": message.message or FORK_REFUSED},
        )
        await self._send_start(live, fork.request, fork.persona, fork.status, fork.sandbox)
        return True

    def _slot_for(self, status: FamilyStatus) -> Slot:
        """Contract 02 §13 rule 2. The limit counts the whole family."""
        running = len(self._turns.active_in_family(status.family))
        return slot_for(status.kind, status.max_running_turns, running)

    def _queue_turn(
        self,
        record: Session,
        request: RunTurnRequest,
        persona: Persona,
        branch: Decision,
    ) -> LiveTurn:
        """Contract 02 §13 rule 3 and §8.2. The prompt reaches disk first.

        `turn_queued` is written before the FIFO holds the turn, so a crash
        between the two loses a slot and never a prompt (§13.3).
        """
        family = record.family
        self._queue.check_room(family)

        live = self._mint_turn(record, request, persona, TurnState.QUEUED)
        line = self._append(
            family,
            record.session,
            LineKind.TURN_QUEUED,
            live.record.turn,
            {
                "prompt": request.prompt,
                "idempotency_key": request.idempotency_key,
                "queue_depth": self._queue.depth(family) + 1,
            },
        )
        live.first_seq = line.journal_seq if line.journal_seq is not None else 0
        self._store.save_turn(live.record)
        self._store.save(record)
        self._queue.add(family, Waiting(live=live, request=request, persona=persona, branch=branch))
        return live

    def _pump_queue(self, family: str) -> None:
        """Start the head of a family's queue when a slot frees (§13 rule 3)."""
        if self._queue.depth(family) == 0:
            return

        status = self._status.read(family)

        if status is None or self._slot_for(status) is Slot.QUEUE:
            return

        waiting = self._queue.take(family)

        if waiting is None:
            return

        if not self._run_later(self._start_waiting(waiting, status)):
            # No event loop, so nothing can dial a sandbox. The turn stays
            # `queued` on disk and the next start aborts it (§13.3).
            self._queue.add(family, waiting)

    def _run_later(self, work: Coroutine[Any, Any, None]) -> bool:
        """Do this after the turn that asked for it has settled, or behind an
        `accepted` answer.

        A settle runs inside a channel read or a request, and both of these
        dial a sandbox or remove a directory. False means no event loop, so
        the caller decides what an undone follow-up costs it.
        """
        try:
            task = asyncio.create_task(work)
        except RuntimeError:
            work.close()
            return False

        self._followups.add(task)
        task.add_done_callback(self._followups.discard)
        return True

    async def _start_waiting(self, waiting: Waiting, status: FamilyStatus) -> None:
        """Move one queued turn to `running` (contract 02 §4.3)."""
        live = waiting.live
        family = live.record.family
        session = live.record.session

        # It may have been stopped, or its session deleted, while it waited.
        if live.record.state is not TurnState.QUEUED:
            return

        record = self._sessions.get((family, session))

        if record is None:
            return

        # A family with nothing to dial, or a sandbox with no env file
        # (contract 05 §4.1). Contract 02 §4.3 has no `queued -> failed`, so
        # the turn starts, on `sandbox` "" when none was found, then fails at
        # once. Left `queued`, it would sit outside the FIFO and its job would
        # never end. `_dial_for` is checked here because `_send_start` lets its
        # refusal escape this task.
        sandbox = ""

        try:
            sandbox = await self._pick_sandbox(family, status)
            self._dial_for(family, status, sandbox)
        except ApiError as error:
            self._begin_turn(live, record, waiting.request, waiting.persona, status, sandbox)
            self._settle(live, TurnState.FAILED, TurnReason.SANDBOX_LOST, error.message)
            return

        self._begin_turn(live, record, waiting.request, waiting.persona, status, sandbox)
        await self._send_start(
            live, waiting.request, waiting.persona, status, sandbox, waiting.branch
        )

    def _pre_start(self, family: str, session: str, status: FamilyStatus) -> None:
        """Contract 03 §4.7 rules 8 and 9. Warm the pi process, do not wait.

        This is the first moment the host knows a session will take a turn,
        and pi's cold start is 1195 ms against 4 ms for a held-open process
        (probe 0a, A2 and A3). The door's answer must not pay for it, so the
        work runs behind this call.
        """
        try:
            task = asyncio.create_task(self._open_session(family, session, status))
        except RuntimeError:
            # No event loop, so no channel either. §4.7 rule 11: a pre-start
            # that cannot happen costs the next turn pi's cold start and
            # nothing else. It may never cost the caller its answer.
            _LOG.info("no pre-start for %s/%s: no running loop", family, session)
            return

        # A task with no reference can be collected mid-flight. This set is
        # the reference, and `close()` is where it ends.
        self._pre_starts.add(task)
        task.add_done_callback(self._pre_starts.discard)

    async def _open_session(self, family: str, session: str, status: FamilyStatus) -> None:
        """Contract 03 §4.7 rules 10 and 11. Every failure here is survivable.

        `start_turn` opens the process itself when this did not happen, so a
        family with no sandbox yet, a channel that would not open and an
        orphaned playpen all end the same way: one log line, no turn.
        """
        try:
            sandbox = await self._pick_sandbox(family, status)
            link = await self._link_for(family, status, sandbox)
            link.note_opened(session)
            await link.send(
                wire.open_session(
                    session=session,
                    cwd=self._sandbox_cwd(family, session),
                    session_dir=self._sandbox_pi_dir(family, session),
                    epoch=status.epoch,
                    config_rev=status.config_rev,
                    # §4.7 rule 6: a process opened without the workspace its
                    # first turn carries has the wrong cwd, so that turn
                    # replaces it. That is worse than never pre-starting.
                    workspace=self._workspace(family, session),
                )
            )
        except (ApiError, OrphanPlaypen, HandshakeError, ChannelClosed, ValueError) as error:
            _LOG.info("no pre-start for %s/%s: %s", family, session, error)

    async def _send_start(
        self,
        live: LiveTurn,
        request: RunTurnRequest,
        persona: Persona,
        status: FamilyStatus,
        sandbox: str,
        branch: Decision = PLAIN_PROMPT,
    ) -> None:
        family = live.record.family
        session = live.record.session

        if branch.branch is Branch.FALLBACK:
            # Contract 02 §10.2 rule 4. The turn runs; the divergence is
            # visible rather than silent.
            self._append(
                family,
                session,
                LineKind.BRANCH_FALLBACK,
                live.record.turn,
                {"wanted_entry": None, "reason": branch.reason},
            )

        if branch.branch is Branch.FORK:
            # Contract 03 §4.1: pi may refuse a target that is not on the
            # active branch, and the host owns the fallback.
            self._forks[live.key] = Fork(request, persona, status, sandbox)

        try:
            link = await self._link_for(family, status, sandbox)

            # The dial may outlast the turn: an `accepted` one can be stopped,
            # or its session deleted, meanwhile. Registered, a settled turn
            # would hold the channel open for ever (contract 03 §10 rule 3).
            if not live.is_active:
                return

            link.register(session, live.record.turn)
            link.note_opened(session)
            await link.send(
                wire.start_turn(
                    turn=live.record.turn,
                    session=session,
                    cwd=self._sandbox_cwd(family, session),
                    session_dir=self._sandbox_pi_dir(family, session),
                    prompt=request.prompt,
                    deadline_s=request.deadline_s,
                    epoch=status.epoch,
                    config_rev=status.config_rev,
                    persona=persona.framed(),
                    attachments=request.attachments,
                    workspace=self._workspace(family, session),
                    branch=branch.wire_branch,
                    delegation=_delegation_of(request),
                )
            )
        except (OrphanPlaypen, PlaypenFatal) as error:
            # Two ways the sandbox cannot serve. An old playpen may still
            # be alive in the VM, so no second one may start there, and one
            # that answered `fatal` will answer the same way on every dial.
            # `managerd` replaces the sandbox, not this service (§11.4 rule 6).
            self._settle(live, TurnState.FAILED, TurnReason.SANDBOX_LOST, str(error))
            return
        except (ChannelClosed, HandshakeError, ValueError) as error:
            self._settle(live, TurnState.FAILED, TurnReason.CHANNEL_LOST, str(error))
            return

        live.deadline_task = asyncio.create_task(self._watch_deadline(live))

    async def _watch_deadline(self, live: LiveTurn) -> None:
        """Contract 02 §12 rule 4. The playpen runs its own deadline too."""
        try:
            await asyncio.sleep(live.record.deadline_s)
        except asyncio.CancelledError:
            return

        if not live.is_active:
            return

        await self._abort_on_channel(live)
        self._settle(live, TurnState.FAILED, TurnReason.TURN_TIMEOUT, "past deadline_s")

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
                    detail={"family_state": current.state.value},
                )

            await asyncio.sleep(COLD_START_POLL_S)
            waited += COLD_START_POLL_S
            current = self._status.require(family)

    def _sandbox_for(self, family: str, status: FamilyStatus) -> SandboxInfo | None:
        """The sandbox this family serves on (contract 05 §4.2 rule 1a).

        The pin is the choice. A switch sets it (§5.3 rule 1), and the first
        turn of a family with no pin sets it from the document. It is
        dropped only when the document stops offering that sandbox: after a
        crash or a teardown the document is the truth about which sandbox
        serves.

        Re-reading the document per turn instead would hand the family to a
        replacement with no switch at all. Contract 05 §4.3 step 5b has
        `managerd` publish the incoming sandbox BEFORE it calls, so between
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

    def _sandbox_cwd(self, family: str, session: str) -> str:
        """The turn's cwd inside the sandbox (contract 03 §4.1, §7.1).

        It is the HOST path of the session directory: sbx mounts the
        family's session store at that same path inside the VM.
        """
        return sandbox_cwd(self._config.sessions_root, family, session)

    def _sandbox_pi_dir(self, family: str, session: str) -> str:
        """PI_CODING_AGENT_DIR inside the sandbox (contract 03 §4.1)."""
        return sandbox_session_dir(self._config.sessions_root, family, session)

    def _workspace(self, family: str, session: str) -> dict[str, Any] | None:
        """Contract 03 §7.2's `workspace`, or None for every other family.

        It names the owner and no host path: the playpen derives the link
        target from its own mount, so a host message cannot aim it elsewhere.
        """
        record = self._sessions.get((family, session))

        if record is None:
            return None

        return workspace_of(family, record.owner_session)

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

    async def _link_for(self, family: str, status: FamilyStatus, sandbox: str) -> PlaypenLink:
        """The channel to one sandbox. Contract 03 §1: one channel per sandbox.

        Keyed by sandbox rather than by family, because a drain runs both at
        once: turns finish on the outgoing sandbox while new turns start on
        the incoming one (contract 05 §5.3 rules 1 and 2).
        """
        dial = self._dial_for(family, status, sandbox)
        link = self._links.get(sandbox)

        if link is None:
            link = PlaypenLink(
                family=family,
                kind=status.kind,
                events=_SandboxEvents(self, family, sandbox),
                factory=self._factory,
                faults=self._faults,
                state_root=self._config.state_root,
                lock_stale_s=self._config.lock_stale_s,
                lock_poll_s=self._config.lock_poll_s,
                idle_ttl_s=self._channel_idle_ttl_s,
                queued=lambda: self._queue.depth(family),
            )
            self._links[sandbox] = link

        await link.ensure_open(dial, status.epoch)
        return link

    def _link_of(self, sandbox: str | None) -> PlaypenLink | None:
        """The channel serving one sandbox, or None."""
        if sandbox is None:
            return None

        return self._links.get(sandbox)

    def _exec_factory(self, dial: SandboxDial) -> Channel:
        family, _, _ = dial.sandbox.rpartition("-s")

        return ExecChannel(
            dial=dial,
            command=self._config.channel_command,
            log_path=self._config.playpen_log(family),
        )

    async def _send_to(self, sandbox: str, family: str, message: dict[str, Any]) -> None:
        """Write one host message to the sandbox that serves a turn."""
        link = self._link_of(sandbox)

        if link is None or not link.is_open:
            raise ApiError(
                ErrorCode.SANDBOX_UNAVAILABLE,
                f"sandbox {sandbox} has no open channel",
                family=family,
                detail={"sandbox": sandbox},
            )

        try:
            await link.send(message)
        except (ChannelClosed, ValueError) as error:
            raise ApiError(
                ErrorCode.SANDBOX_UNAVAILABLE,
                f"sandbox {sandbox} lost its channel",
                family=family,
                detail={"sandbox": sandbox},
            ) from error

    async def _abort_on_channel(self, live: LiveTurn) -> None:
        """Ask the turn's own sandbox to stop. A closed channel is fine here."""
        link = self._link_of(live.record.sandbox)

        if link is None or not link.is_open:
            return

        with contextlib.suppress(ChannelClosed, ValueError):
            await link.send(wire.abort(live.record.session, live.record.turn))

    async def _stop_process_of(self, session: str) -> bool:
        """Close this session's pi process wherever one may be held open.

        Every open channel is asked, not the one the last turn named. A
        session with no turn yet was pre-started on a sandbox no turn record
        names (contract 03 §4.7 rule 8), and a session that lived through a
        drain was opened on two. A closed channel is not an error here.

        True means at least one channel was asked, which is contract 02
        §5.11's `released`.
        """
        asked = False

        for link in list(self._links.values()):
            if not link.is_open or not link.has_opened(session):
                continue

            with contextlib.suppress(ChannelClosed, ValueError):
                await link.send(wire.stop_process(session))
                asked = True

        return asked

    # ------------------------------------------------------ channel events
    #
    # The link calls these, so they belong to this class's design rather
    # than to its internals. Each takes the family, because one link
    # serves exactly one family.

    async def handle_event(
        self, family: str, session: str, turn: str, event: dict[str, Any]
    ) -> None:
        live = self._turns.get(family, session, turn)

        if live is None or not live.is_active:
            return

        delta = text_delta(event)

        if delta is not None:
            live.text.append(delta)

        self._append(family, session, LineKind.PI_EVENT, turn, event)

    async def handle_settled(self, family: str, message: SettledLine) -> None:
        """Contract 03 §13 rule 8: only a turn the host started may settle."""
        live = self._turns.get(family, message.session, message.turn)

        if live is None or not live.is_active:
            return

        live.record.usage.add(message.usage)
        self._map_owui(family, live, message)
        self._settle(
            live,
            TurnState.SETTLED,
            None,
            "",
            {
                "usage": live.record.usage.to_api(),
                "leaf_id": message.leaf_id,
                "user_entry_id": message.user_entry_id,
            },
        )

    async def handle_failed(self, family: str, message: FailedLine) -> None:
        live = self._turns.get(family, message.session, message.turn)

        if live is None or not live.is_active:
            return

        if message.reason is wire.PlaypenReason.FORK_REFUSED and (
            await self._retry_without_fork(live, message)
        ):
            return

        mapped = wire.host_reason(message.reason)

        # A retried reason answers a message this stage never sends, so it is
        # the playpen that is confused, not the turn (contract 03 §5.3).
        if mapped is None:
            self._append(
                family,
                message.session,
                LineKind.NOTE,
                message.turn,
                {"note": "unexpected_playpen_reason", "reason": message.reason.value},
            )
            mapped = TurnReason.INTERNAL

        self._settle(live, TurnState.FAILED, mapped, message.message)

    async def handle_violation(self, family: str, session: str, turn: str, why: Violation) -> None:
        live = self._turns.get(family, session, turn)

        if live is None or not live.is_active:
            return

        self._settle(live, TurnState.FAILED, TurnReason.PROTOCOL_VIOLATION, why.value)

    async def handle_process_exit(self, family: str, message: ProcessExitLine) -> None:
        if (family, message.session) not in self._sessions:
            return

        self._append(
            family,
            message.session,
            LineKind.NOTE,
            None,
            {"note": "process_exit", "reason": message.reason, "code": message.code},
        )

    async def handle_log(self, family: str, message: LogLine) -> None:
        _LOG.info("family=%s session=%s %s", family, message.session, message.message)

        if message.level not in _WARN_LEVELS or message.session is None:
            return

        if (family, message.session) not in self._sessions:
            return

        self._append(
            family,
            message.session,
            LineKind.NOTE,
            None,
            {"note": "playpen_log", "level": message.level, "message": message.message},
        )

    async def handle_session_opened(self, family: str, message: OpenedLine) -> None:
        """Contract 03 §5.6. Read, never acted on.

        A pre-start that did not happen costs the next turn pi's cold start
        and nothing else (§4.7 rule 10), so this is a log line and no journal
        line. `not_held` is a family that holds nothing between turns, which
        is the design rather than a failure (§6 rule 4).
        """
        if message.resident or message.reason == wire.OPEN_REASON_NOT_HELD:
            return

        _LOG.info(
            "family=%s session=%s pre-start did not hold: %s",
            family,
            message.session,
            message.reason,
        )

    # ------------------------------------------------------ terminal turns

    def _read_terminal(self, family: str, session: str) -> None:
        """Contract 02 §10.5. Ask what the terminal wrote, off the door's path.

        A lease call answers in milliseconds and this read opens a channel
        and may start a pi process, so it runs behind the answer. §10.5's
        last paragraph is why nothing here may fail the release: a door that
        could not get out of the way would hold the session.
        """
        record = self._sessions.get((family, session))

        if record is None or record.kind is not SessionKind.ATTENDED:
            return

        try:
            task = asyncio.create_task(self._send_entry_read(family, session))
        except RuntimeError:
            _LOG.info("no terminal read for %s/%s: no running loop", family, session)
            return

        self._entry_tasks.add(task)
        task.add_done_callback(self._entry_tasks.discard)

    async def _send_entry_read(self, family: str, session: str) -> None:
        """Contract 03 §4.8. One read, and every failure is survivable."""
        record = self._sessions.get((family, session))

        if record is None:
            return

        request = new_ulid()

        try:
            status = self._status.require(family)
            sandbox = await self._pick_sandbox(family, status)
            link = await self._link_for(family, status, sandbox)

            if not _serves_entries(link):
                # An image built before contract 03 §4.8 logs an unknown type
                # and answers nothing, and the read would wait for ever.
                # Saying so is the point: silence is the failure §10.5 exists
                # to remove.
                _LOG.info("sandbox %s serves no %s", sandbox, wire.HostType.GET_ENTRIES.value)
                return

            self._remember_read(request, family, session)
            await link.send(
                wire.get_entries(
                    request=request,
                    session=session,
                    cwd=self._sandbox_cwd(family, session),
                    session_dir=self._sandbox_pi_dir(family, session),
                    epoch=status.epoch,
                    config_rev=status.config_rev,
                    since=record.pi_cursor,
                    workspace=self._workspace(family, session),
                )
            )
        except (ApiError, OrphanPlaypen, HandshakeError, ChannelClosed, ValueError) as error:
            # The next lease release reads again, and the cursor did not
            # move, so nothing was lost.
            self._entry_reads.pop(request, None)
            _LOG.info("no terminal read for %s/%s: %s", family, session, error)

    def _remember_read(self, request: str, family: str, session: str) -> None:
        """Note one read in flight, and never let the note outlive the fleet.

        A channel that drops between the question and the answer leaves an
        entry nothing will ever pop. Dropping the oldest bounds it: the
        answer to a read this old is not coming, and the cursor it would
        have moved is still where the next lease release will find it.
        """
        self._entry_reads[request] = (family, session)

        while len(self._entry_reads) > MAX_ENTRY_READS:
            self._entry_reads.pop(next(iter(self._entry_reads)))

    async def handle_entries(self, family: str, message: EntriesLine) -> None:
        """Contract 03 §5.8 and contract 02 §10.5 steps 2 to 5."""
        owner = self._entry_reads.pop(message.request, None)

        if owner is None or owner != (family, message.session):
            # §5.8 rule 4. A request this service never sent names nothing.
            _LOG.info("family=%s dropped an entries answer nobody asked for", family)
            return

        record = self._sessions.get(owner)

        if record is None:
            return

        if not message.ok:
            # The cursor did not move, so the next lease release reads the
            # same entries again (§10.5's last paragraph).
            _LOG.info("no entries for %s/%s: %s", family, message.session, message.reason)
            return

        read = pair(message.entries, known=record.owui_map.values())

        for exchange in read.exchanges:
            self._journal_exchange(record, exchange)

        if read.cursor is None:
            # Nothing complete came back, so nothing moved. Reading again
            # would ask the same question and never stop.
            return

        record.pi_cursor = read.cursor
        self._save_session(*owner)

        if message.truncated:
            # Contract 03 §8's cap. The cursor moved, so the next read asks
            # for what the cap left behind and this terminates.
            self._read_terminal(*owner)

    def _journal_exchange(self, record: Session, exchange: Exchange) -> None:
        """One `terminal_exchange` line, one count, one copy (§10.5)."""
        self._append(
            record.family,
            record.session,
            LineKind.TERMINAL_EXCHANGE,
            None,
            {
                "entry_id": exchange.entry_id,
                "parent_entry_id": exchange.user_entry_id,
                "prompt": exchange.prompt,
                "answer": exchange.answer,
            },
        )
        record.turns_total += 1
        record.terminal_total += 1
        self._copy_exchange(record, exchange)

    def _copy_exchange(self, record: Session, exchange: Exchange) -> None:
        """§10.5 steps 5 and rule 5. The chat gains it, and so does the map.

        The two Open WebUI message ids are minted HERE and not in the writer.
        `owui_map`'s order is what §10.2 reads, so a row written when a slow
        Open WebUI finally answered would land after the next turn's rows and
        turn a continuation into a fork.
        """
        if not self._owui.enabled:
            return

        copy = owui_copy.TurnCopy(
            prompt=exchange.prompt,
            answer=exchange.answer,
            source=owui_copy.Source.TERMINAL,
        )
        self._map_copy(record, copy.pair, exchange.user_entry_id, exchange.entry_id)
        self._owui.submit(record, copy)

    async def handle_channel_lost(self, family: str, sandbox: str) -> None:
        """Contract 03 §11.4. Sessions survive. Turns in flight do not.

        Only the turns of THIS sandbox die. During a drain the family has a
        second channel open, and the turns already moved onto it are not
        affected by the outgoing sandbox dropping (contract 05 §5.3 rule 2).
        """
        self._links.pop(sandbox, None)

        for live in self._turns_on(family, sandbox):
            self._settle(live, TurnState.FAILED, TurnReason.CHANNEL_LOST, "channel dropped")

    # ------------------------------------------------------------ plumbing

    def _settle(
        self,
        live: LiveTurn,
        state: TurnState,
        reason: TurnReason | None,
        message: str,
        body: dict[str, Any] | None = None,
    ) -> None:
        """Move a turn to a terminal state, journal it, and wake its waiters."""
        if not can_move(live.record.state, state):
            return

        live.record.state = state
        live.record.reason = reason
        live.record.ended_at = now()

        family = live.record.family
        session = live.record.session
        payload = dict(body) if body is not None else {}

        if reason is not None:
            payload["reason"] = reason.value

        if message:
            payload["message"] = message

        self._append(family, session, _TERMINAL_KIND[state], live.record.turn, payload)
        self._store.save_turn(live.record)
        self._save_session(family, session)
        self._release(live)
        self._end_job(live, message)

    def _apply_gate(self, gate: Gate) -> None:
        """One audit record, applied to the turn it names (contract 04 §8.6).

        CONTRACT-QUESTION: contract 04 sends this service no message when a
        gate opens, and `claimed.session_id` and `claimed.turn_id` are the
        sandbox's own claims (§3.1). The lookup below is what makes them
        safe: only a turn the host itself started, in the family the PEP's
        token proved, is moved.
        """
        live = self._turns.get(gate.family, gate.session, gate.turn)

        if live is None:
            return

        if gate.state is GateState.OPENED:
            self._open_gate(live, gate)
            return

        self._close_gate(live, gate)

    def _open_gate(self, live: LiveTurn, gate: Gate) -> None:
        """Contract 02 §4.3 and §8.1's `approval_requested` line."""
        if not can_move(live.record.state, TurnState.WAITING_APPROVAL):
            return

        live.record.state = TurnState.WAITING_APPROVAL
        live.record.approvals += 1
        self._store.save_turn(live.record)
        self._append(
            live.record.family,
            live.record.session,
            LineKind.APPROVAL_REQUESTED,
            live.record.turn,
            # The summary is the PEP's, built by a pure function from the
            # call (contract 04 §8.3), and it never reaches this file.
            {"tool": gate.tool, "summary": "", "gate_id": gate.gate},
        )

    def _close_gate(self, live: LiveTurn, gate: Gate) -> None:
        """Contract 02 §4.3. Back to `running`, or failed with the reason."""
        if live.record.state is not TurnState.WAITING_APPROVAL:
            return

        self._append(
            live.record.family,
            live.record.session,
            LineKind.APPROVAL_RESOLVED,
            live.record.turn,
            {"decision": gate.reason, "waited_s": gate.waited_s},
        )
        # Contract 02 §13.1.1. Counted here, from the PEP's own record, and
        # never from the turn's failure reason: a deny leaves the turn
        # running, so one turn can meet an approved gate and a denied one.
        live.record.gates.count(outcome_of(gate.reason))
        ending = _GATE_FAILURES.get(gate.reason)

        if ending is None:
            live.record.state = TurnState.RUNNING
            self._store.save_turn(live.record)
            return

        self._settle(live, TurnState.FAILED, ending, gate.reason)

    def _end_job(self, live: LiveTurn, message: str) -> None:
        """Contract 02 §13 rule 7. A finished job leaves a record, not a session.

        The job ends when every turn of the session is terminal. A queued
        turn means a slot is still coming, so the job is not over yet.
        """
        family = live.record.family
        session = live.record.session
        record = self._sessions.get((family, session))

        if record is None or record.kind is not SessionKind.AUTONOMOUS:
            return

        turns = self._turns.of_session(family, session)

        if any(not is_terminal(one.record.state) for one in turns):
            return

        # The record is written first. A crash between the two leaves a
        # session that ends again and writes a second record, which is
        # visible and harmless. The other order loses the job entirely.
        error = None if live.record.state is TurnState.SETTLED else message
        built = outcomes.build(record, [one.record for one in turns], error)
        outcomes.write(self._config.state_root, built)
        self._note_dispatch_outcome(record, built.id)
        self._run_later(self._remove_session(family, session))

    def _release(self, live: LiveTurn) -> None:
        family = live.record.family
        session = live.record.session
        link = self._link_of(live.record.sandbox)

        if link is not None:
            link.unregister(session, live.record.turn)

        task = live.deadline_task
        live.deadline_task = None

        if task is not None and task is not asyncio.current_task():
            task.cancel()

        self._queue.drop(family, live.record.turn)
        self._forks.pop(live.key, None)
        self._leases.clear_turn(family, session)
        live.done.set()

        # A slot may have just freed. Contract 02 §13 rule 3: a queued turn
        # starts when a running one ends.
        self._pump_queue(family)

    def _append(
        self,
        family: str,
        session: str,
        kind: LineKind,
        turn: str | None,
        body: dict[str, Any],
    ) -> JournalLine:
        """Write first, then fan out. A reader never sees an unwritten line."""
        line = self._journal.append(family, session, kind, turn, body)
        record = self._sessions.get((family, session))

        if record is not None and line.journal_seq is not None:
            record.journal_seq = line.journal_seq
            record.updated_at = line.ts

        self._hub.publish(family, session, line)
        return line

    def _save_session(self, family: str, session: str) -> None:
        record = self._sessions.get((family, session))

        if record is not None:
            self._store.save(record)

    def _map_owui(self, family: str, live: LiveTurn, message: SettledLine) -> None:
        """Contract 02 §10.1. Two rows per settled turn, from the turn itself."""
        record = self._sessions.get((family, live.record.session))

        if record is None:
            return

        self._copy_to_owui(record, live, message)

        # §10.5 rule 3. The cursor covers every entry this service has
        # accounted for, turns included. Without it the next terminal read
        # would ask for everything after the last TERMINAL exchange and copy
        # the turns between them into Open WebUI a second time.
        if message.leaf_id is not None:
            record.pi_cursor = message.leaf_id

        refs = live.record.owui

        if refs is None:
            return

        if refs.user_message_id is not None and message.user_entry_id is not None:
            record.owui_map[refs.user_message_id] = message.user_entry_id

        if message.leaf_id is not None:
            record.owui_map[refs.message_id] = message.leaf_id

        # §10.5 step 5 needs a parent for the terminal's first message, and
        # Open WebUI's own newest message is it. `owui_chat` stays None for
        # a session born there: `chat_of` reads the chat id from the session
        # id, which contract 02 §2 fixed at create.
        record.owui_leaf = refs.message_id

    def _copy_to_owui(self, record: Session, live: LiveTurn, message: SettledLine) -> None:
        """Contract 02 §10.4. A session not born in Open WebUI gains a chat.

        Attended sessions only. A thin job leaves the audit record and
        nothing else (invariant 15), and an autonomous job leaves §13.1's
        outcome record and is deleted, so neither has a transcript to copy.
        """
        if not self._owui.enabled or record.kind is not SessionKind.ATTENDED:
            return

        if _born_in_owui(record.session) or live.record.owui is not None:
            # Open WebUI already holds this session's chat. The SESSION's own
            # id settles that, not one turn's refs: invariant 3 lets another
            # door run a turn on an `owui-` session, and such a turn carries
            # no refs, so a check on the refs alone would give a session that
            # already has a chat a second one.
            return

        copy = owui_copy.TurnCopy(prompt=live.prompt, answer=live.answer())
        self._map_copy(record, copy.pair, message.user_entry_id, message.leaf_id)

        # Rule 5: the cost sits off the turn's path. `submit` queues the turn
        # and returns, so an Open WebUI that does not answer delays this
        # settle by nothing. Rule 3's retry is the writer's own job.
        self._owui.submit(record, copy)

    def _map_copy(
        self,
        record: Session,
        pair: owui_copy.MessagePair,
        user_entry_id: str | None,
        entry_id: str | None,
    ) -> None:
        """§10.5 rule 5, for an exchange THIS service wrote into a chat.

        A copied turn and a terminal exchange are the same case: the two
        Open WebUI message ids were minted here, so the phone's next message
        names one of them as its parent, and §10.2 rule 4 writes
        `branch_fallback` unless the map has it.
        """
        if user_entry_id is not None:
            record.owui_map[pair.user] = user_entry_id

        if entry_id is not None:
            record.owui_map[pair.assistant] = entry_id

    def _keep_owui_chat(self, record: Session) -> None:
        """Save a session the Open WebUI writer just changed (§10.4).

        The copy left the settle path, so the session reached disk before
        the write happened and without the chat id the write learned. A
        restart that lost it would make a second chat for a session that
        already has one.
        """
        self._store.save(record)

    def _recover_turns(self, record: Session) -> None:
        """A restart is a channel drop (contract 03 §11), so nothing survives it."""
        for turn in self._store.load_turns(record.family, record.session):
            live = LiveTurn(record=turn, prompt="")

            if turn.state in (TurnState.RUNNING, TurnState.WAITING_APPROVAL):
                turn.state = TurnState.FAILED
                turn.reason = TurnReason.CHANNEL_LOST
                turn.ended_at = now()
                self._store.save_turn(turn)
            elif turn.state is TurnState.QUEUED:
                # Contract 02 §13.3. The queue lives in memory, so a restart
                # ends every turn waiting in it, and `queue_lost` says that
                # rather than claiming a bug in this service.
                turn.state = TurnState.ABORTED
                turn.reason = TurnReason.QUEUE_LOST
                turn.ended_at = now()
                self._store.save_turn(turn)

            live.done.set()
            self._turns.add(live)

    def _repeat_of(self, family: str, session: str, request: RunTurnRequest) -> LiveTurn | None:
        """Contract 02 §6. A repeat never starts a second turn."""
        key = request.idempotency_key

        if key is None:
            return None

        found = self._turns.by_key(family, session, key)

        if found is None:
            return None

        if found.record.prompt_sha256 != sha256_hex(request.prompt):
            raise ApiError(
                ErrorCode.IDEMPOTENCY_MISMATCH,
                "this idempotency key is known and the prompt differs",
                family=family,
                session=session,
                turn=found.record.turn,
            )

        return found

    def _set_trigger(self, record: Session, kind: SessionKind, request: RunTurnRequest) -> None:
        """Contract 02 §13.2 rules 1 and 3. One session is one job is one firing."""
        if request.trigger is None:
            return

        if kind is not SessionKind.AUTONOMOUS:
            raise ApiError(
                ErrorCode.BAD_REQUEST,
                f"a {kind.value} family's turn carries no trigger",
                family=record.family,
                session=record.session,
            )

        # Rule 1: a later turn's trigger is read and ignored. The outcome
        # record names the firing that started the job, not the newest one.
        if record.trigger is not None:
            return

        record.trigger = request.trigger
        self._store.save(record)

    def _refuse_second_turn(self, family: str, session: str) -> None:
        """Turns of one session are serial (contract 03 §6 rule 2).

        Contract 02 §5.6: a second turn on one session is `session_busy`,
        and the detail names the turn so a door can wait for the right thing.
        """
        active = self._turns.active_in_session(family, session)

        if not active:
            return

        raise ApiError(
            ErrorCode.SESSION_BUSY,
            "this session already runs a turn",
            family=family,
            session=session,
            detail={"turn": active[0].record.turn},
        )

    def _require_lease(
        self, principal: Principal, family: str, session: str, door_instance: str
    ) -> None:
        """Steer and stop belong to the lease holder (contract 02 §5.6, §7)."""
        lease = self._leases.get(family, session)

        if lease is None:
            return

        if lease.holder is holder_of(principal, family, session) and (
            lease.door_instance == door_instance
        ):
            return

        raise busy_error(family, session, lease)

    def _take_lease(
        self,
        family: str,
        session: str,
        holder: Holder,
        door_instance: str,
        takeover: Takeover = Takeover.POLITE,
        intent: Intent = Intent.ACQUIRE,
    ) -> Grant:
        """Contract 02 §7.3, plus the journal line both UIs read (§8.1)."""
        before = self._leases.last_holder(family, session)
        grant = self._leases.take(
            family,
            session,
            holder,
            door_instance,
            takeover,
            self._activity(family, session),
            intent,
        )

        # A renew changes no writer, and a turn renews the lease. Journalling
        # it would put a `writer_changed` line beside every turn saying the
        # writer did not change. §7.3's sentence covers a grant and a release.
        if grant.reason is not LeaseReason.RENEWED:
            self._journal_writer(family, session, holder, grant.reason)

        # §10.5. A `tui` lease that ended is when a terminal's exchanges
        # exist. A takeover is one way it ends (the operator opens Open WebUI on a
        # terminal they never closed), and rule 2's grant over an EXPIRED
        # lease is the other (they killed the window, and 60 seconds passed).
        moved = grant.reason in (LeaseReason.TAKEN_OVER, LeaseReason.GRANTED)

        if moved and before is Holder.TUI:
            self._read_terminal(family, session)

        return grant

    def _journal_writer(
        self, family: str, session: str, holder: Holder, reason: LeaseReason
    ) -> None:
        """Contract 02 §8.1's `writer_changed` line, so both UIs see it."""
        self._append(
            family,
            session,
            LineKind.WRITER_CHANGED,
            None,
            {"holder": holder.value, "reason": reason.value},
        )

    def _refuse_while_in_flight(self, family: str, session: str, what: str) -> None:
        """Contract 02 §5.10 rule 2 and §5.11 rule 2.

        An unowned turn in flight is a turn any door may then stop, which §7
        exists to prevent. A caller that wants a busy session stops the turn
        first (§5.7).
        """
        blocking = self._turns.blocking_in_session(family, session)

        if not blocking:
            return

        raise ApiError(
            ErrorCode.SESSION_BUSY,
            f"{what} stays held while a turn is in flight",
            family=family,
            session=session,
            detail={"turn": blocking[0].record.turn},
        )

    def _activity(self, family: str, session: str) -> TurnActivity:
        if self._turns.blocking_in_session(family, session):
            return TurnActivity.ACTIVE

        return TurnActivity.IDLE

    def _require_turn(self, family: str, session: str, turn: str) -> LiveTurn:
        live = self._turns.get(family, session, turn)

        if live is None:
            raise ApiError(
                ErrorCode.TURN_NOT_FOUND,
                "no such turn in this session",
                family=family,
                session=session,
                turn=turn,
            )

        return live

    def _require_session(self, family: str, session: str) -> Session:
        if not is_family(family) or not is_session(session):
            raise ApiError(ErrorCode.BAD_REQUEST, "family or session is malformed")

        record = self._sessions.get((family, session))

        if record is None:
            raise ApiError(ErrorCode.NOT_FOUND, "no such session", family=family, session=session)

        return record

    def _read_access(self, principal: Principal, family: str, session: str) -> Session:
        """The prefix check belongs to create only (see `check_session_prefix`)."""
        check_access(principal, Access.READ)
        record = self._require_session(family, session)
        check_family_kind(principal, family, record.kind)
        return record

    def _write_access(self, principal: Principal, family: str, session: str) -> Session:
        check_access(principal, Access.WRITE)
        record = self._require_session(family, session)
        check_family_kind(principal, family, record.kind)
        return record

    def _session_body(self, record: Session) -> dict[str, Any]:
        return record.to_api(
            state=self._turns.session_state(record.family, record.session),
            turns_running=self._turns.turns_running(record.family, record.session),
            writer=self._leases.get(record.family, record.session),
        )

    def _seq_of(self, family: str, session: str) -> int:
        record = self._sessions.get((family, session))
        return record.journal_seq if record is not None else 0

    def _matches(self, record: Session, query: ListQuery, allowed: SessionKind | None) -> bool:
        if allowed is not None and record.kind is not allowed:
            return False

        if query.family is not None and record.family != query.family:
            return False

        if query.kind is not None and record.kind is not query.kind:
            return False

        if query.state is None:
            return True

        return self._turns.session_state(record.family, record.session) is query.state

    # ------------------------------------------------------------ for tests

    @property
    def faults(self) -> FaultReporter:
        return self._faults

    @property
    def store(self) -> SessionStore:
        return self._store

    def lease(self, family: str, session: str) -> WriterLease | None:
        return self._leases.get(family, session)

    def queue_depth(self, family: str) -> int:
        """How many turns wait for a slot (contract 02 §13 rule 3)."""
        return self._queue.depth(family)

    def link(self, sandbox: str) -> PlaypenLink | None:
        return self._links.get(sandbox)

    def live_turn(self, family: str, session: str, turn: str) -> LiveTurn | None:
        return self._turns.get(family, session, turn)


class _SandboxEvents:
    """Binds one sandbox's channel to the service's handlers (contract 03 §5).

    `LinkEvents` carries neither family nor sandbox, because a link serves
    exactly one of each. This adapter supplies both, so the service keeps one
    handler per event kind rather than one per channel.
    """

    def __init__(self, service: SessionService, family: str, sandbox: str) -> None:
        self._service = service
        self._family = family
        self._sandbox = sandbox

    async def on_event(self, session: str, turn: str, event: dict[str, Any]) -> None:
        await self._service.handle_event(self._family, session, turn, event)

    async def on_settled(self, message: SettledLine) -> None:
        await self._service.handle_settled(self._family, message)

    async def on_failed(self, message: FailedLine) -> None:
        await self._service.handle_failed(self._family, message)

    async def on_violation(self, session: str, turn: str, why: Violation) -> None:
        await self._service.handle_violation(self._family, session, turn, why)

    async def on_process_exit(self, message: ProcessExitLine) -> None:
        await self._service.handle_process_exit(self._family, message)

    async def on_log(self, message: LogLine) -> None:
        await self._service.handle_log(self._family, message)

    async def on_session_opened(self, message: OpenedLine) -> None:
        await self._service.handle_session_opened(self._family, message)

    async def on_entries(self, message: EntriesLine) -> None:
        await self._service.handle_entries(self._family, message)

    async def on_channel_lost(self, sandbox: str) -> None:
        await self._service.handle_channel_lost(self._family, sandbox)


_TERMINAL_KIND: dict[TurnState, LineKind] = {
    TurnState.SETTLED: LineKind.TURN_SETTLED,
    TurnState.FAILED: LineKind.TURN_FAILED,
    TurnState.ABORTED: LineKind.TURN_ABORTED,
}


def _in_family(sandbox: str, family: str) -> bool:
    """True when this sandbox id belongs to this family (contract 05 §4.1)."""
    return is_sandbox(sandbox) and sandbox.startswith(f"{family}-s")


def _switch_refused(family: str, why: str) -> ApiError:
    """Contract 05 §5.3 rule 8's refusal, with one body shape."""
    return ApiError(ErrorCode.BAD_REQUEST, why, family=family)


def _wanted_entry(fork: Fork) -> str | None:
    """Which pi entry the refused fork named (contract 02 §8.1)."""
    refs = fork.request.owui
    return refs.parent_id if refs is not None else None


def _serves_entries(link: PlaypenLink) -> bool:
    """Contract 03 §3. Does this sandbox's image answer §4.8 at all?"""
    ready = link.ready

    return ready is not None and wire.HostType.GET_ENTRIES.value in ready.caps


def _born_in_owui(session: str) -> bool:
    """Contract 02 §2: an Open WebUI chat becomes `owui-<chat id>`.

    The prefix is fixed at create and outlives every door, which is what
    makes it the right answer to "does this session already have a chat".
    """
    return session.startswith(SessionPrefix.OWUI.value)


def _delegation_of(request: RunTurnRequest) -> dict[str, Any] | None:
    """What `start_turn` carries for a job inside a chain (contract 03 §7.4)."""
    if request.delegation is None:
        return None

    return request.delegation.to_channel()


def _list_order(record: Session) -> tuple[float, str, str]:
    """Newest activity first, then a stable tie-break the cursor can resume."""
    return (-record.updated_at.timestamp(), record.family, record.session)


def _page(rows: list[Session], cursor: str | None, limit: int) -> tuple[list[Session], str | None]:
    """Keyset paging. An offset would skip or repeat rows as sessions move."""
    start = 0

    if cursor is not None:
        mark = _decode_cursor(cursor)
        start = next(
            (index for index, row in enumerate(rows) if _list_order(row) > mark),
            len(rows),
        )

    page = rows[start : start + limit]
    more = start + limit < len(rows)
    return page, _encode_cursor(page[-1]) if page and more else None


def _encode_cursor(record: Session) -> str:
    """The cursor is opaque, so it carries the sort key itself.

    An RFC 3339 string would round to the second, and a page of sessions
    touched inside one second would then resume at the wrong row.
    """
    raw = _CURSOR_SEPARATOR.join(
        (repr(record.updated_at.timestamp()), record.family, record.session)
    )
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[float, str, str]:
    """A cursor comes from a caller, so a bad one reads as "start over"."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return _CURSOR_START

    parts = raw.split(_CURSOR_SEPARATOR)

    if len(parts) != 3:
        return _CURSOR_START

    try:
        moment = float(parts[0])
    except ValueError:
        return _CURSOR_START

    return (-moment, parts[1], parts[2])
