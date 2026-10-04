"""The session API (contract 02): what `attendance` reads and writes.

The surfaces, by what each one pins:

- `session.request.<operation>`: the bytes of a request body to the parsed
  request, or to the refusal with its HTTP status and its error body.
- `session.query.<operation>`: the parameters of a query to the parsed
  query, or to the refusal.
- `session.error_body`: the arguments of one refusal to its HTTP status and
  to the exact bytes of the answer.
- `session.answer.<object>`: the fields of one session, turn or lease to the
  exact bytes of the answer.
- `session.journal.read`: one line of a journal file to the record that a
  replay gives.
- `session.journal.write`: the arguments of one append to the exact bytes
  of the line.
- `session.stream.encode`: one record of the event stream to the exact bytes
  that a reader gets.
- `session.stream.live`: the two records that the stream makes itself.
- `session.turn.move`: each pair of turn states to whether the move is legal.
- `session.state.derive`: the states of the turns of a session to the state
  of the session.
- `session.outcome.status`: the last turn of a job to the status of the job.
- `session.outcome.write`: one outcome record to the exact bytes of its file.

A body and a query go through the real routes of `attendance.api`. The
service behind the routes is a stand-in: it keeps the arguments of the one
call that a route makes, so no vector needs a token, a sandbox or a family.
`session_cases.py` holds the inputs.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final, cast
from urllib.parse import urlencode

from attendance.api import build_app
from attendance.auth import Principal, TokenBook
from attendance.clock import now
from attendance.errors import ApiError, ErrorCode, TurnReason
from attendance.journal import Journal
from attendance.models import (
    Holder,
    JournalLine,
    LineKind,
    OwuiRefs,
    Session,
    Trigger,
    TriggerKind,
    Turn,
    Usage,
    WriterLease,
)
from attendance.paths import journal_file, outcome_file
from attendance.service import SessionService
from attendance.states import (
    SessionKind,
    SessionState,
    TurnState,
    can_move,
    derive_session_state,
)
from attendance.streams import StreamHub
from starlette.testclient import TestClient

from attendance import outcomes
from vectors.core import (
    Json,
    Raised,
    Surface,
    Vector,
    accepted,
    attempt,
    normalize,
    raised,
    refused,
    text_input,
)
from vectors.surfaces import session_cases as cases
from vectors.surfaces.session_cases import (
    FAMILY,
    SESSION,
    TURN,
    Append,
    Body,
    Derive,
    Live,
    Query,
    Record,
    Refusal,
)

CONTRACT: Final = "contract 02"

#: The message of the refusal that the stand-in service answers each call with.
TAKEN: Final = "the stand-in service took the call"

SESSIONS: Final = "/v1/sessions"
ONE_SESSION: Final = f"{SESSIONS}/{FAMILY}/{SESSION}"
ONE_TURN: Final = f"{ONE_SESSION}/turns/{TURN}"

JSON_BODY: Final = {"content-type": "application/json"}

#: The time of each line that the generator appends itself.
MOMENT: Final = datetime.fromisoformat("2026-10-05T19:22:05.118000+00:00")


# --- the stand-in service and the real routes --------------------------------------------


@dataclass(frozen=True)
class Call:
    """One call of a route to the service."""

    name: str
    args: tuple[object, ...]
    kwargs: dict[str, object]


async def _given(lines: tuple[JournalLine, ...]) -> AsyncIterator[JournalLine]:
    for line in lines:
        yield line


async def _later(answer: dict[str, Any]) -> dict[str, Any]:
    return answer


class StandIn:
    """Stands for `SessionService` behind the routes of `attendance.api`.

    It keeps the arguments of each call. Then it answers with `answer`, or
    with `lines` for a stream, or it refuses with `refusal`. A route
    therefore parses its request with the real parser, and nothing after
    the parser runs. `awaited` is for a route that awaits its answer.
    """

    def __init__(self) -> None:
        self.calls: list[Call] = []
        self.refusal = ApiError(ErrorCode.NOT_IMPLEMENTED, TAKEN)
        self.answer: dict[str, Any] | None = None
        self.awaited = False
        self.lines: tuple[JournalLine, ...] | None = None

    def reset(self) -> None:
        self.calls.clear()
        self.refusal = ApiError(ErrorCode.NOT_IMPLEMENTED, TAKEN)
        self.answer = None
        self.awaited = False
        self.lines = None

    def __getattr__(self, name: str) -> Callable[..., object]:
        def take(*args: object, **kwargs: object) -> object:
            self.calls.append(Call(name, args, kwargs))
            if self.lines is not None:
                return _given(self.lines)

            if self.answer is not None:
                return _later(self.answer) if self.awaited else self.answer

            raise self.refusal

        return take


class _Tokens:
    """Stands for `TokenBook`: each caller is the Open WebUI door."""

    def identify(self, header: str | None) -> Principal:
        return Principal.DOOR_OWUI


@dataclass(frozen=True)
class Answer:
    """What a route answered, and the call that it made to the service."""

    status: int
    content: bytes
    call: Call | None

    def refusal(self) -> dict[str, object]:
        return {"http_status": self.status, "body": json.loads(self.content)}


class Door:
    """The routes of `attendance.api` over the stand-in service."""

    def __init__(self) -> None:
        self.service = StandIn()
        app = build_app(cast("SessionService", self.service), cast("TokenBook", _Tokens()))
        self._client = TestClient(app)

    def send(self, method: str, path: str, content: bytes | None = None) -> Answer:
        headers = JSON_BODY if content is not None else None
        response = self._client.request(method, path, content=content, headers=headers)
        calls = self.service.calls
        if len(calls) > 1:
            raise ValueError(f"{method} {path} made {len(calls)} calls to the service")

        return Answer(response.status_code, response.content, calls[0] if calls else None)

    def close(self) -> None:
        self._client.close()


# --- the request bodies ----------------------------------------------------------------


@dataclass(frozen=True)
class Route:
    """One route with a body, and where its call holds the parsed request."""

    name: str
    path: str
    entry: str
    section: str
    bodies: tuple[Body, ...]
    #: The parsed request, from the call that the route makes.
    take: Callable[[Call], object]
    notes: tuple[str, ...] = ()


def _argument(position: int) -> Callable[[Call], object]:
    """The parsed request is one positional argument of the call."""

    def take(call: Call) -> object:
        return call.args[position]

    return take


def _named(name: str, position: int) -> Callable[[Call], object]:
    """The parsed request is one text. The value is an object with that one field."""

    def take(call: Call) -> object:
        return {name: call.args[position]}

    return take


_NOTE_BODY: Final = (
    "The input is the bytes of a request body. The route reads it as JSON and gives the "
    "object to the entry point."
)
_NOTE_VALUE: Final = "value is the parsed request, with every default filled in."
_NOTE_REFUSAL: Final = (
    "refusal.http_status is the HTTP status of the answer. refusal.body is the error body "
    "of contract 02 §14, as parsed JSON."
)
_NOTE_PATH: Final = (
    "The family, the session and the turn of the path are in context. The entry point does "
    "not check them. It copies the family and the session into an error body."
)
_NOTE_SERVICE: Final = (
    "The service behind the route is a stand-in. A refusal that the real service makes "
    "after the parse has no vector here."
)
_NOTE_TIME: Final = (
    "A time in a value is in UTC, in the ISO 8601 form of Python: +00:00 and not Z, and six "
    "digits of fraction when the fraction is not zero."
)

_PATH_CONTEXT: Final[dict[str, Json]] = {"family": FAMILY, "session": SESSION, "turn": TURN}

ROUTES: Final[tuple[Route, ...]] = (
    Route(
        "create",
        SESSIONS,
        "attendance.requests.read_create",
        "§5.1",
        (*cases.READER_BODIES, *cases.CREATE_BODIES),
        _argument(1),
        (
            "The vectors with an id that starts with body-, json-, bytes- or top- are about "
            "the reader of a body. Every route with a body has the same reader.",
        ),
    ),
    Route(
        "run_turn",
        f"{ONE_SESSION}/turns",
        "attendance.requests.read_run_turn",
        "§5.4, §13.2",
        cases.RUN_TURN_BODIES,
        _argument(3),
        (
            _NOTE_PATH,
            _NOTE_TIME,
            "value.delegation is always null. No body carries one.",
        ),
    ),
    Route(
        "writer",
        f"{ONE_SESSION}/writer",
        "attendance.requests.read_writer",
        "§5.9",
        cases.WRITER_BODIES,
        _argument(3),
        (_NOTE_PATH,),
    ),
    Route(
        "steer",
        f"{ONE_TURN}/steer",
        "attendance.requests.read_steer",
        "§5.6",
        cases.STEER_BODIES,
        _named("message", 4),
        (_NOTE_PATH, "The entry point returns the message. value holds it as value.message."),
    ),
    Route(
        "stop",
        f"{ONE_TURN}/stop",
        "attendance.requests.read_stop_reason",
        "§5.7",
        cases.STOP_BODIES,
        _named("reason", 4),
        (_NOTE_PATH, "The entry point returns the reason. value holds it as value.reason."),
    ),
    Route(
        "dispatch",
        "/dispatch",
        "attendance.requests.read_dispatch",
        "§13.4.1",
        cases.DISPATCH_BODIES,
        _argument(1),
    ),
    Route(
        "jobs",
        "/dispatch/jobs",
        "attendance.requests.read_jobs",
        "§13.4.2",
        cases.JOBS_BODIES,
        _argument(1),
        (_NOTE_TIME,),
    ),
    Route(
        "delegate",
        "/delegate",
        "attendance.requests.read_delegate",
        "§12, contract 04 §7.3",
        cases.DELEGATE_BODIES,
        _argument(1),
    ),
    Route(
        "switch",
        "/internal/switch-sandbox",
        "attendance.requests.read_switch",
        "§5, contract 05 §5.1",
        cases.SWITCH_BODIES,
        _argument(1),
        ("value.outgoing is the field `from` of the body.",),
    ),
)


def _request_vector(door: Door, route: Route, body: Body) -> Vector:
    given = body.given()
    raw = body.data()
    door.service.reset()
    answer = attempt(lambda: door.send("POST", route.path, raw))
    if isinstance(answer, Raised):
        return raised(body.id, given, answer.exc)

    if answer.call is None:
        return refused(body.id, given, answer.refusal())

    return accepted(body.id, given, route.take(answer.call))


def _request_surface(door: Door, route: Route) -> Surface:
    return Surface(
        name=f"session.request.{route.name}",
        path=f"session/request.{route.name}.json",
        entry=route.entry,
        contract=f"{CONTRACT} {route.section}",
        notes=(_NOTE_BODY, _NOTE_VALUE, _NOTE_REFUSAL, *route.notes, _NOTE_SERVICE),
        context=_PATH_CONTEXT,
        vectors=tuple(_request_vector(door, route, body) for body in route.bodies),
    )


# --- the queries ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Lookup:
    """One route with a query, and where its call holds the parsed query."""

    name: str
    path: str
    entry: str
    section: str
    queries: tuple[Query, ...]
    take: Callable[[Call], object]
    notes: tuple[str, ...] = ()


def _events_query(call: Call) -> object:
    """The three parameters of the stream, as the route gives them to the service."""
    return {name: call.kwargs[name] for name in ("from_seq", "turn", "follow")}


_NOTE_QUERY: Final = (
    "The input is the parameters of a query, by name. A parameter that is null is not in "
    "the query. The generator writes each other one into the URL in percent form, and the "
    "route reads it back as the same text."
)

LOOKUPS: Final[tuple[Lookup, ...]] = (
    Lookup(
        "list",
        SESSIONS,
        "attendance.requests.read_list_query",
        "§5.2",
        cases.LIST_QUERIES,
        _argument(1),
        ("The route reads limit as a number with Python's int before the entry point runs.",),
    ),
    Lookup(
        "get",
        ONE_SESSION,
        "attendance.requests.read_turns_wanted",
        "§5.3",
        cases.GET_QUERIES,
        _named("turns", 3),
        (
            _NOTE_PATH,
            "The route reads turns as a number with Python's int before the entry point runs.",
        ),
    ),
    Lookup(
        "events",
        f"{ONE_SESSION}/events",
        "attendance.requests.read_from_seq",
        "§5.5",
        cases.EVENTS_QUERIES,
        _events_query,
        (
            _NOTE_PATH,
            "The route reads from_seq with Python's int, then with the entry point. It reads "
            "follow itself. It gives turn to the service as the text of the query.",
            "value.follow is keep_open or replay_only.",
        ),
    ),
)


def _query_vector(door: Door, lookup: Lookup, query: Query) -> Vector:
    given: dict[str, Json] = {"args": normalize(query.args)}
    sent = urlencode({name: text for name, text in query.args.items() if text is not None})
    door.service.reset()
    answer = attempt(lambda: door.send("GET", f"{lookup.path}?{sent}" if sent else lookup.path))
    if isinstance(answer, Raised):
        return raised(query.id, given, answer.exc)

    if answer.call is None:
        return refused(query.id, given, answer.refusal())

    return accepted(query.id, given, lookup.take(answer.call))


def _query_surface(door: Door, lookup: Lookup) -> Surface:
    return Surface(
        name=f"session.query.{lookup.name}",
        path=f"session/query.{lookup.name}.json",
        entry=lookup.entry,
        contract=f"{CONTRACT} {lookup.section}",
        notes=(_NOTE_QUERY, _NOTE_VALUE, _NOTE_REFUSAL, *lookup.notes, _NOTE_SERVICE),
        context=_PATH_CONTEXT,
        vectors=tuple(_query_vector(door, lookup, query) for query in lookup.queries),
    )


# --- the error body and the three objects of an answer -------------------------------------

_NOTE_SORTED: Final = (
    "Every object of free form inside args has its keys in sorted order. A vector file "
    "sorts keys, and the Python code keeps the order that it is given."
)
_NOTE_OUTPUT: Final = "output is the exact bytes that the Python code writes."


def _sorted_at_each_level(value: object) -> bool:
    if isinstance(value, dict):
        typed = cast("dict[str, object]", value)
        keys = list(typed)

        return keys == sorted(keys) and all(_sorted_at_each_level(item) for item in typed.values())

    if isinstance(value, list):
        return all(_sorted_at_each_level(item) for item in cast("list[object]", value))

    return True


def _refusal_vector(door: Door, refusal: Refusal) -> Vector:
    if not refusal.holder_block and not _sorted_at_each_level(refusal.detail):
        raise ValueError(f"{refusal.id}: a detail of free form has its keys in sorted order")

    args: dict[str, object] = {
        "code": refusal.code,
        "message": refusal.message,
        "family": refusal.family,
        "session": refusal.session,
        "turn": refusal.turn,
        "detail": refusal.detail,
    }
    given: dict[str, Json] = {"args": normalize(args)}
    door.service.reset()
    door.service.refusal = ApiError(
        refusal.code,
        refusal.message,
        family=refusal.family,
        session=refusal.session,
        turn=refusal.turn,
        detail=refusal.detail,
    )
    answer = attempt(lambda: door.send("DELETE", ONE_SESSION))
    if isinstance(answer, Raised):
        return raised(refusal.id, given, answer.exc)

    output = text_input(answer.content.decode("utf-8"))

    return accepted(refusal.id, given, http_status=answer.status, output=output)


@dataclass(frozen=True)
class Reply:
    """The fields of one object of an answer, and the route that answers with it."""

    id: str
    args: dict[str, Any]


def _time(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _maybe_time(text: str | None) -> datetime | None:
    return _time(text) if text is not None else None


def _lease(args: dict[str, Any]) -> WriterLease:
    return WriterLease(
        holder=Holder(args["holder"]),
        door_instance=args["door_instance"],
        since=_time(args["since"]),
        expires_at=_time(args["expires_at"]),
        turn=args["turn"],
    )


def _session(args: dict[str, Any]) -> dict[str, Any]:
    record = Session(
        family=args["family"],
        session=args["session"],
        kind=SessionKind(args["kind"]),
        created_at=_time(args["created_at"]),
        updated_at=_time(args["updated_at"]),
        title=args["title"],
        journal_seq=args["journal_seq"],
        turns_total=args["turns_total"],
        sandbox=args["sandbox"],
        persona_hash=args["persona_hash"],
        labels=args["labels"],
        terminal_total=args["terminal_total"],
    )
    writer = _lease(args["writer"]) if args["writer"] is not None else None

    return record.to_api(SessionState(args["state"]), args["turns_running"], writer)


def _turn(args: dict[str, Any]) -> Turn:
    owui = args["owui"]
    reason = args["reason"]

    return Turn(
        turn=args["turn"],
        session=SESSION,
        family=FAMILY,
        state=TurnState(args["state"]),
        started_at=_time(args["started_at"]),
        sandbox=args["sandbox"],
        deadline_s=args["deadline_s"],
        reason=TurnReason(reason) if reason is not None else None,
        ended_at=_maybe_time(args["ended_at"]),
        idempotency_key=args["idempotency_key"],
        usage=Usage(**args["usage"]),
        approvals=args["approvals"],
        owui=OwuiRefs(**owui) if owui is not None else None,
        persona_truncated=args["persona_truncated"],
    )


_SESSION_ARGS: Final[dict[str, Any]] = {
    "family": FAMILY,
    "session": SESSION,
    "kind": "attended",
    "title": "Kitchen sensor debug",
    "state": "idle",
    "created_at": "2026-10-05T19:21:47+00:00",
    "updated_at": "2026-10-05T19:25:10+00:00",
    "journal_seq": 63,
    "writer": None,
    "turns_total": 2,
    "terminal_total": 0,
    "turns_running": 0,
    "sandbox": cases.SANDBOX,
    "persona_hash": cases.DIGEST,
    "labels": {"door": "owui", "room": "kitchen"},
}

_LEASE_ARGS: Final[dict[str, Any]] = {
    "holder": "tui",
    "door_instance": "tui.4242",
    "since": "2026-10-06T08:15:20+00:00",
    "expires_at": "2026-10-06T08:16:20+00:00",
    "turn": None,
}

_USAGE_ARGS: Final[dict[str, Any]] = {
    "input": 4120,
    "output": 188,
    "cache_read": 0,
    "cache_write": 0,
    "cost_usd": 0.014,
}

_TURN_ARGS: Final[dict[str, Any]] = {
    "turn": TURN,
    "state": "settled",
    "reason": None,
    "started_at": "2026-10-05T19:22:05+00:00",
    "ended_at": "2026-10-05T19:22:31+00:00",
    "deadline_s": 3600,
    "idempotency_key": "b7c1e2d0-1f44-4c61-8a2b-9e0d3c5f7a11",
    "sandbox": cases.SANDBOX,
    "usage": _USAGE_ARGS,
    "approvals": 0,
    "owui": {
        "chat_id": "3f2a9c41-77b0-4a1e-9a4c-1d0e5f8b2c33",
        "message_id": "b7c1e2d0-1f44-4c61-8a2b-9e0d3c5f7a11",
        "user_message_id": "a1b2c3d4-5e6f-4071-8293-a4b5c6d7e8f9",
        "parent_id": None,
    },
    "persona_truncated": False,
}

SESSION_REPLIES: Final[tuple[Reply, ...]] = (
    Reply("idle", _SESSION_ARGS),
    Reply(
        "new",
        {
            **_SESSION_ARGS,
            "title": "",
            "updated_at": _SESSION_ARGS["created_at"],
            "journal_seq": 0,
            "turns_total": 0,
            "sandbox": None,
            "persona_hash": None,
            "labels": {},
        },
    ),
    *(
        Reply(f"state-{state.value}", {**_SESSION_ARGS, "state": state.value})
        for state in SessionState
    ),
    *(Reply(f"kind-{kind.value}", {**_SESSION_ARGS, "kind": kind.value}) for kind in SessionKind),
    Reply(
        "running-with-a-writer",
        {
            **_SESSION_ARGS,
            "state": "running",
            "turns_running": 1,
            "writer": {**_LEASE_ARGS, "holder": "owui", "door_instance": "door-owui", "turn": TURN},
        },
    ),
    Reply("terminal-exchanges", {**_SESSION_ARGS, "turns_total": 5, "terminal_total": 3}),
    Reply(
        "text-forms",
        {
            **_SESSION_ARGS,
            "title": 'caf\u00e9 "quoted" \\ / \u2028 \U0001f600 \x7f\n',
            "labels": {"a": "", "caf\u00e9": "\u00e9"},
        },
    ),
    Reply(
        "times-with-an-offset",
        {
            **_SESSION_ARGS,
            "created_at": "2026-12-31T23:30:00.999999-01:00",
            "updated_at": "2027-01-01T02:30:00+02:00",
        },
    ),
)

LEASE_REPLIES: Final[tuple[Reply, ...]] = (
    Reply("idle", _LEASE_ARGS),
    *(
        Reply(f"holder-{holder.value}", {**_LEASE_ARGS, "holder": holder.value})
        for holder in Holder
    ),
    Reply("with-a-turn", {**_LEASE_ARGS, "turn": TURN}),
    Reply("door-instance-any-text", {**_LEASE_ARGS, "door_instance": "Not An Instance \u00e9"}),
    Reply(
        "times-with-a-fraction",
        {
            **_LEASE_ARGS,
            "since": "2026-10-06T08:15:20.999999+00:00",
            "expires_at": "2026-10-06T10:12:40.5+02:00",
        },
    ),
)

TURN_REPLIES: Final[tuple[Reply, ...]] = (
    Reply("settled", _TURN_ARGS),
    *(
        Reply(
            f"state-{state.value}",
            {**_TURN_ARGS, "state": state.value, "ended_at": None, "owui": None},
        )
        for state in (TurnState.QUEUED, TurnState.RUNNING, TurnState.WAITING_APPROVAL)
    ),
    *(
        Reply(f"failed-{reason.value}", {**_TURN_ARGS, "state": "failed", "reason": reason.value})
        for reason in TurnReason
    ),
    Reply("aborted", {**_TURN_ARGS, "state": "aborted", "reason": "user_stopped"}),
    Reply(
        "queued-no-sandbox",
        {**_TURN_ARGS, "state": "queued", "ended_at": None, "sandbox": "", "owui": None},
    ),
    Reply(
        "aborted-in-the-queue",
        {**_TURN_ARGS, "state": "aborted", "reason": "queue_lost", "sandbox": "", "owui": None},
    ),
    Reply(
        "minimal",
        {
            **_TURN_ARGS,
            "state": "running",
            "ended_at": None,
            "deadline_s": 1,
            "idempotency_key": None,
            "usage": {**_USAGE_ARGS, "input": 0, "output": 0, "cost_usd": 0.0},
            "owui": None,
        },
    ),
    Reply(
        "counts",
        {
            **_TURN_ARGS,
            "approvals": 7,
            "persona_truncated": True,
            "usage": {
                "input": 2**53,
                "output": 2**63,
                "cache_read": 2**64 - 1,
                "cache_write": 1,
                "cost_usd": 1234.5678,
            },
        },
    ),
    Reply("owui-two-ids", {**_TURN_ARGS, "owui": {**_TURN_ARGS["owui"], "user_message_id": None}}),
    Reply(
        "cost-long-float",
        {**_TURN_ARGS, "usage": {**_USAGE_ARGS, "cost_usd": cases.LONG_FLOAT}},
    ),
)


@dataclass(frozen=True)
class ReplyRoute:
    """One object of an answer: the route that writes it, and how the model makes it."""

    name: str
    method: str
    path: str
    entry: str
    section: str
    replies: tuple[Reply, ...]
    build: Callable[[dict[str, Any]], dict[str, Any]]
    notes: tuple[str, ...] = ()
    #: The route awaits the answer of the service.
    awaited: bool = False


def _lease_body(args: dict[str, Any]) -> dict[str, Any]:
    return _lease(args).to_api()


def _turn_body(args: dict[str, Any]) -> dict[str, Any]:
    return _turn(args).to_api()


_NOTE_REPLY_TIME: Final = (
    "A time in args is in the ISO 8601 form of Python. The generator reads it with "
    "datetime.fromisoformat."
)

REPLY_ROUTES: Final[tuple[ReplyRoute, ...]] = (
    ReplyRoute(
        "session",
        "GET",
        ONE_SESSION,
        "attendance.models.Session.to_api",
        "§4.2",
        SESSION_REPLIES,
        _session,
        (
            "args.state, args.turns_running and args.writer are the three arguments of the "
            "entry point. Each other field of args is a field of the Session.",
        ),
    ),
    ReplyRoute(
        "turn",
        "POST",
        f"{ONE_TURN}/stop",
        "attendance.models.Turn.to_api",
        "§4.4",
        TURN_REPLIES,
        _turn_body,
        awaited=True,
    ),
    ReplyRoute(
        "lease",
        "POST",
        f"{ONE_SESSION}/writer",
        "attendance.models.WriterLease.to_api",
        "§7.1",
        LEASE_REPLIES,
        _lease_body,
    ),
)


def _reply_vector(door: Door, route: ReplyRoute, reply: Reply) -> Vector:
    args = reply.args
    if not _sorted_at_each_level(args.get("labels")):
        raise ValueError(f"{reply.id}: the labels have their keys in sorted order")

    given: dict[str, Json] = {"args": normalize(args)}
    body = attempt(lambda: route.build(args))
    if isinstance(body, Raised):
        return raised(reply.id, given, body.exc)

    door.service.reset()
    door.service.answer = body
    door.service.awaited = route.awaited
    content = b"{}" if route.method == "POST" else None
    answer = attempt(lambda: door.send(route.method, route.path, content))
    if isinstance(answer, Raised):
        return raised(reply.id, given, answer.exc)

    output = text_input(answer.content.decode("utf-8"))

    return accepted(reply.id, given, http_status=answer.status, output=output)


def _reply_surface(door: Door, route: ReplyRoute) -> Surface:
    return Surface(
        name=f"session.answer.{route.name}",
        path=f"session/answer.{route.name}.json",
        entry=route.entry,
        contract=f"{CONTRACT} {route.section}",
        notes=(
            "The input is the fields of the object. The generator makes the model from them "
            "and calls the entry point. A route of attendance.api then writes the result.",
            _NOTE_REPLY_TIME,
            _NOTE_OUTPUT,
            *route.notes,
            "The labels of a session have free form. " + _NOTE_SORTED,
        ),
        vectors=tuple(_reply_vector(door, route, reply) for reply in route.replies),
    )


# --- the journal ---------------------------------------------------------------------------

LF: Final = b"\n"


def _read_vector(scratch: Path, line: Body) -> Vector:
    given = line.given()
    raw = line.data()
    if LF in raw:
        raise ValueError(f"{line.id}: the input is more than one line")

    root = scratch / "read" / line.id
    path = journal_file(root, FAMILY, SESSION)
    path.parent.mkdir(parents=True)
    path.write_bytes(raw + LF)
    before = now()
    found = attempt(lambda: list(Journal(root).replay(FAMILY, SESSION)))
    after = now()
    if isinstance(found, Raised):
        return raised(line.id, given, found.exc)

    if not found:
        return refused(line.id, given)

    (record,) = found
    # A time between the two readings of the clock is the time of the read:
    # the reader could not read the time of the line. No file can hold it.
    read_now = before <= record.ts <= after

    return accepted(line.id, given, _record_value(record, None if read_now else record.ts))


def _record_value(record: JournalLine, ts: datetime | None) -> dict[str, object]:
    """The fields of one record, with `ts` in place of its time."""
    return {
        "journal_seq": record.journal_seq,
        "ts": ts,
        "kind": record.kind,
        "turn": record.turn,
        "body": record.body,
    }


def _append_vector(scratch: Path, append: Append) -> Vector:
    if append.free and not _sorted_at_each_level(append.body):
        raise ValueError(f"{append.id}: a body of free form has its keys in sorted order")

    args: dict[str, object] = {
        "kind": append.kind,
        "turn": append.turn,
        "body": append.body,
        "moment": append.moment,
        "last_seq": append.last_seq,
    }
    given: dict[str, Json] = {"args": normalize(args)}
    root = scratch / "write" / append.id
    journal = Journal(root)
    journal.register(FAMILY, SESSION, append.last_seq)
    moment = datetime.fromisoformat(append.moment)
    line = attempt(
        lambda: journal.append(FAMILY, SESSION, append.kind, append.turn, append.body, moment)
    )
    journal.close_all()
    if isinstance(line, Raised):
        return raised(append.id, given, line.exc)

    written = journal_file(root, FAMILY, SESSION).read_bytes()

    return accepted(append.id, given, output=text_input(written.decode("utf-8")))


# --- the event stream ----------------------------------------------------------------------


def _record_vector(door: Door, record: Record) -> Vector:
    args: dict[str, object] = {
        "journal_seq": record.journal_seq,
        "ts": record.ts,
        "kind": record.kind,
        "turn": record.turn,
        "body": record.body,
    }
    given: dict[str, Json] = {"args": normalize(args)}
    line = JournalLine(
        journal_seq=record.journal_seq,
        ts=datetime.fromisoformat(record.ts),
        kind=record.kind,
        turn=record.turn,
        body=record.body,
    )
    door.service.reset()
    door.service.lines = (line,)
    answer = attempt(lambda: door.send("GET", f"{ONE_SESSION}/events"))
    if isinstance(answer, Raised):
        return raised(record.id, given, answer.exc)

    output = text_input(answer.content.decode("utf-8"))

    return accepted(record.id, given, http_status=answer.status, output=output)


#: The room of the reader in an overrun vector: one line.
SMALL_BUFFER: Final = 1

HEARTBEAT: Final = "heartbeat"
OVERRUN: Final = "overrun"


def _note_line(journal: Journal) -> JournalLine:
    """One more line on disk. The stream gets it only when the caller publishes it."""
    return journal.append(FAMILY, SESSION, LineKind.NOTE, None, {"note": "x"}, MOMENT)


async def _first_without_seq(stream: AsyncIterator[JournalLine]) -> JournalLine:
    async for line in stream:
        if line.journal_seq is None:
            return line

    raise ValueError("the stream ended with no record of its own")


async def _heartbeat(hub: StreamHub, live: Live) -> JournalLine:
    """No line arrives, so the heartbeat is due at once."""
    stream = hub.stream(FAMILY, SESSION, from_seq=live.from_seq, heartbeat_s=0.0)
    try:
        return await _first_without_seq(stream)
    finally:
        await stream.aclose()


async def _overrun(hub: StreamHub, journal: Journal, live: Live) -> JournalLine:
    """Two lines arrive while the reader has room for one."""
    stream = hub.stream(FAMILY, SESSION, from_seq=live.from_seq, heartbeat_s=60.0)
    try:
        for _ in range(live.lines_on_disk - live.from_seq):
            await anext(stream)

        waiting = asyncio.ensure_future(anext(stream))
        while hub.reader_count(FAMILY, SESSION) == 0 or not _is_waiting(waiting):
            await asyncio.sleep(0)

        for _ in range(SMALL_BUFFER + 1):
            hub.publish(FAMILY, SESSION, _note_line(journal))

        await waiting

        return await _first_without_seq(stream)
    finally:
        await stream.aclose()


def _is_waiting(task: asyncio.Future[JournalLine]) -> bool:
    """Whether the stream runs and waits for its next line."""
    return not task.done()


async def _live(scratch: Path, live: Live) -> JournalLine:
    journal = Journal(scratch / "live" / live.id)
    try:
        for _ in range(live.lines_on_disk):
            _note_line(journal)

        if live.event == HEARTBEAT:
            return await _heartbeat(StreamHub(journal), live)

        return await _overrun(StreamHub(journal, capacity=SMALL_BUFFER), journal, live)
    finally:
        journal.close_all()


def _live_vector(scratch: Path, live: Live) -> Vector:
    args: dict[str, object] = {
        "event": live.event,
        "lines_on_disk": live.lines_on_disk,
        "from_seq": live.from_seq,
    }
    given: dict[str, Json] = {"args": normalize(args)}
    record = attempt(lambda: asyncio.run(_live(scratch, live)))
    if isinstance(record, Raised):
        return raised(live.id, given, record.exc)

    value = _record_value(record, None)
    del value["ts"]

    return accepted(live.id, given, value)


# --- the states ------------------------------------------------------------------------------


def _move_vector(current: TurnState, wanted: TurnState) -> Vector:
    vector_id = f"{current.value}-to-{wanted.value}"
    given: dict[str, Json] = {"args": {"current": current.value, "wanted": wanted.value}}

    return accepted(vector_id, given) if can_move(current, wanted) else refused(vector_id, given)


def _derive_vector(derive: Derive) -> Vector:
    args: dict[str, object] = {
        "turns": derive.turns,
        "last_settled_failed": derive.last_settled_failed,
    }
    given: dict[str, Json] = {"args": normalize(args)}
    state = derive_session_state(list(derive.turns), derive.last_settled_failed)

    return accepted(derive.id, given, state)


# --- the outcome record (§13.1) ----------------------------------------------------------------

#: The states that end a turn, and each reason that a turn record can hold.
_ENDS: Final = (TurnState.SETTLED, TurnState.FAILED, TurnState.ABORTED)
_REASONS: Final[tuple[TurnReason | None, ...]] = (None, *TurnReason)


def _status_vector(state: TurnState, reason: TurnReason | None) -> Vector:
    word = reason.value if reason is not None else "no-reason"
    args: dict[str, object] = {"state": state, "reason": reason}
    given: dict[str, Json] = {"args": normalize(args)}
    last = Turn(
        turn=TURN,
        session=SESSION,
        family=FAMILY,
        state=state,
        started_at=MOMENT,
        sandbox=cases.SANDBOX,
        reason=reason,
    )

    return accepted(f"{state.value}-{word}", given, outcomes.status_of(last))


@dataclass(frozen=True)
class Outcome:
    """The fields of one outcome record."""

    id: str
    args: dict[str, Any] = field(default_factory=dict[str, Any])


_OUTCOME_ARGS: Final[dict[str, Any]] = {
    "id": "01JBQ80M4F7S2YQ1VZK6W3TDEN",
    "family": "scrum-lead",
    "session": cases.JOB_SESSION,
    "trigger": {"kind": "timer", "name": "morning-triage", "fired_at": "2026-10-06T06:00:00+00:00"},
    "started_at": "2026-10-06T06:00:01+00:00",
    "ended_at": "2026-10-06T06:05:12+00:00",
    "status": "ok",
    "error": None,
    "turns": 3,
    "approvals": {"requested": 1, "approved": 1, "denied": 0, "timed_out": 0},
    "spend_usd": 0.21,
    "spend_reason": None,
    "sandbox": "scrum-lead-s2",
}

OUTCOMES: Final[tuple[Outcome, ...]] = (
    Outcome("ok", _OUTCOME_ARGS),
    *(
        Outcome(f"status-{status.value}", {**_OUTCOME_ARGS, "status": status.value})
        for status in outcomes.Outcome
    ),
    Outcome("no-trigger", {**_OUTCOME_ARGS, "trigger": None}),
    Outcome(
        "trigger-webhook-no-time",
        {**_OUTCOME_ARGS, "trigger": {"kind": "webhook", "name": "boiler-alert", "fired_at": None}},
    ),
    Outcome(
        "trigger-timer-no-name",
        {**_OUTCOME_ARGS, "trigger": {"kind": "timer", "name": None, "fired_at": None}},
    ),
    Outcome(
        "trigger-dispatch",
        {
            **_OUTCOME_ARGS,
            "trigger": {
                "kind": "dispatch",
                "name": "chat",
                "fired_at": "2026-10-06T06:00:00+00:00",
                "chain": ["chat", "scrum-lead"],
            },
        },
    ),
    Outcome(
        "failed-with-an-error",
        {
            **_OUTCOME_ARGS,
            "status": "failed",
            "error": 'the model gave no answer: caf\u00e9 "quoted" \\ \u2028 \U0001f600\n',
            "turns": 1,
        },
    ),
    Outcome(
        "spend-unknown",
        {**_OUTCOME_ARGS, "spend_usd": None, "spend_reason": outcomes.SPEND_UNKNOWN_REASON},
    ),
    Outcome("spend-zero", {**_OUTCOME_ARGS, "spend_usd": 0.0}),
    Outcome("spend-small", {**_OUTCOME_ARGS, "spend_usd": 0.000015}),
    Outcome("spend-long-float", {**_OUTCOME_ARGS, "spend_usd": cases.LONG_FLOAT}),
    Outcome(
        "approvals-denied-and-timed-out",
        {
            **_OUTCOME_ARGS,
            "status": "denied",
            "approvals": {"requested": 5, "approved": 1, "denied": 2, "timed_out": 1},
        },
    ),
    Outcome("no-sandbox", {**_OUTCOME_ARGS, "sandbox": ""}),
)


def _trigger(args: dict[str, Any] | None) -> Trigger | None:
    if args is None:
        return None

    return Trigger(
        kind=TriggerKind(args["kind"]),
        name=args["name"],
        fired_at=_maybe_time(args["fired_at"]),
        chain=tuple(args.get("chain", ())),
    )


def _outcome_record(args: dict[str, Any]) -> outcomes.OutcomeRecord:
    return outcomes.OutcomeRecord(
        id=args["id"],
        family=args["family"],
        session=args["session"],
        trigger=_trigger(args["trigger"]),
        started_at=_time(args["started_at"]),
        ended_at=_time(args["ended_at"]),
        status=outcomes.Outcome(args["status"]),
        error=args["error"],
        turns=args["turns"],
        approvals=outcomes.Approvals(**args["approvals"]),
        spend_usd=args["spend_usd"],
        spend_reason=args["spend_reason"],
        sandbox=args["sandbox"],
    )


def _outcome_vector(scratch: Path, outcome: Outcome) -> Vector:
    args = outcome.args
    given: dict[str, Json] = {"args": normalize(args)}
    root = scratch / "outcome" / outcome.id
    written = attempt(lambda: outcomes.write(root, _outcome_record(args)))
    if isinstance(written, Raised):
        return raised(outcome.id, given, written.exc)

    if written != outcome_file(root, args["family"], args["id"]):
        raise ValueError(f"{outcome.id}: the record is not where the path module puts it")

    return accepted(outcome.id, given, output=text_input(written.read_bytes().decode("utf-8")))


# --- the surfaces ------------------------------------------------------------------------------


def _vectors[T](items: Sequence[T], make: Callable[[T], Vector]) -> tuple[Vector, ...]:
    return tuple(make(item) for item in items)


def _file_surfaces(scratch: Path) -> tuple[Surface, ...]:
    return (
        Surface(
            name="session.journal.read",
            path="session/journal.read.json",
            entry="attendance.journal.Journal.replay",
            contract=f"{CONTRACT} §8, §9",
            notes=(
                "The input is the bytes of one line of a journal file, with no LF. The "
                "generator writes the line and one LF to a journal file. Then it replays "
                "the file from sequence 0.",
                "value is the record that the replay gives. A refused vector is a line that "
                "the replay skips. A line with a journal_seq of 0 or less is such a line.",
                "value.ts is the time of the line in UTC, in the ISO 8601 form of Python. It "
                "is null when the reader cannot read the time of the line. The reader then "
                "gives the line the time of the read.",
                "value.body is the body as the reader keeps it. The reader does not check a "
                "body against its kind.",
            ),
            vectors=_vectors(cases.JOURNAL_LINES, lambda line: _read_vector(scratch, line)),
        ),
        Surface(
            name="session.journal.write",
            path="session/journal.write.json",
            entry="attendance.journal.Journal.append",
            contract=f"{CONTRACT} §8, §8.1",
            notes=(
                "The input is the arguments of one append. args.last_seq is the sequence "
                "of the last line of the journal before the append. args.moment is the time "
                "of the line, in the ISO 8601 form of Python.",
                "The entry point takes each body. It does not check a body against its kind. "
                "The body of each vector has a form that attendance.service writes, or a "
                "form that the contract leaves free.",
                "The body of a pi_event has free form, and so has the body of a note that "
                "the service does not write. " + _NOTE_SORTED,
                "output is the exact line that the Python code writes, with its LF.",
            ),
            vectors=_vectors(cases.APPENDS, lambda append: _append_vector(scratch, append)),
        ),
        Surface(
            name="session.stream.live",
            path="session/stream.live.json",
            entry="attendance.streams.StreamHub.stream",
            contract=f"{CONTRACT} §5.5, §8.1",
            notes=(
                "The input is one stream. args.lines_on_disk is the count of lines of the "
                "journal. The reader starts after args.from_seq.",
                "args.event is heartbeat or overrun. For heartbeat, no line arrives after "
                "the replay. For overrun, two lines arrive, and the reader has room for one.",
                "value is the first record that the stream makes itself: a record with no "
                "journal_seq. value holds no ts. The ts is the time of the write.",
            ),
            vectors=_vectors(cases.LIVES, lambda live: _live_vector(scratch, live)),
        ),
        Surface(
            name="session.outcome.write",
            path="session/outcome.write.json",
            entry="attendance.outcomes.write",
            contract=f"{CONTRACT} §13.1",
            notes=(
                "The input is the fields of one outcome record. " + _NOTE_REPLY_TIME,
                "output is the exact bytes of the file that the Python code writes.",
            ),
            vectors=_vectors(OUTCOMES, lambda outcome: _outcome_vector(scratch, outcome)),
        ),
    )


def _state_surfaces() -> tuple[Surface, ...]:
    return (
        Surface(
            name="session.turn.move",
            path="session/turn.move.json",
            entry="attendance.states.can_move",
            contract=f"{CONTRACT} §4.3",
            notes=(
                "The input is two states of a turn. accepted means that a turn in "
                "args.current can move to args.wanted.",
            ),
            vectors=tuple(_move_vector(current, wanted) for current, wanted in cases.MOVES),
        ),
        Surface(
            name="session.state.derive",
            path="session/state.derive.json",
            entry="attendance.states.derive_session_state",
            contract=f"{CONTRACT} §4.1",
            notes=(
                "args.turns is the state of each turn of one session, the oldest first. "
                "args.last_settled_failed is true when the last turn that ended has the "
                "state failed.",
                "value is the state of the session.",
            ),
            vectors=_vectors(cases.DERIVES, _derive_vector),
        ),
        Surface(
            name="session.outcome.status",
            path="session/outcome.status.json",
            entry="attendance.outcomes.status_of",
            contract=f"{CONTRACT} §13.1, §13.1.2",
            notes=(
                "The input is the state and the reason of the turn that ended a job. "
                "value is the status of the job.",
                "The service gives a settled turn no reason. It gives a failed turn and an "
                "aborted turn a reason. The entry point takes each pair.",
            ),
            vectors=tuple(_status_vector(state, reason) for state in _ENDS for reason in _REASONS),
        ),
    )


def _door_surfaces(door: Door) -> tuple[Surface, ...]:
    return (
        *(_request_surface(door, route) for route in ROUTES),
        *(_query_surface(door, lookup) for lookup in LOOKUPS),
        Surface(
            name="session.error_body",
            path="session/error_body.json",
            entry="attendance.errors.ApiError.body",
            contract=f"{CONTRACT} §14",
            notes=(
                "The input is the arguments of one ApiError. The stand-in service raises "
                "it, and the handler of attendance.api writes the answer.",
                "A detail with the four keys holder, since, expires_at and turn is the holder "
                "block of contract 02 §7.2. The Python code writes those keys in that order.",
                "Each other detail has free form. " + _NOTE_SORTED,
                "http_status is the HTTP status of the answer.",
                _NOTE_OUTPUT,
            ),
            vectors=_vectors(cases.REFUSALS, lambda refusal: _refusal_vector(door, refusal)),
        ),
        *(_reply_surface(door, route) for route in REPLY_ROUTES),
        Surface(
            name="session.stream.encode",
            path="session/stream.encode.json",
            entry="attendance.api.build_app",
            contract=f"{CONTRACT} §5.5, §8",
            notes=(
                "The input is the fields of one record of the event stream. args.ts is in "
                "the ISO 8601 form of Python. The stand-in service gives the record to the "
                "events route.",
                "A record with a journal_seq of null is one that the stream makes itself: a "
                "heartbeat, or the note stream_overrun.",
                "output is the exact line that a reader of the stream gets, with its LF.",
            ),
            vectors=_vectors(cases.RECORDS, lambda record: _record_vector(door, record)),
        ),
    )


def surfaces() -> tuple[Surface, ...]:
    door = Door()
    try:
        through_the_door = _door_surfaces(door)
    finally:
        door.close()

    with tempfile.TemporaryDirectory(prefix="vectors-session-") as scratch:
        on_disk = _file_surfaces(Path(scratch))

    return (*through_the_door, *on_disk, *_state_surfaces())
