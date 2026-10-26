"""The HTTP surface of `attendance` (contract 02): the bodies it reads and
the files it writes.

The surfaces, by what each one pins:

- `session.request.<operation>`: the bytes of a request body to the parsed
  request, or to the refusal with its HTTP status and its error body.
- `session.error_body`: the arguments of one refusal to its HTTP status and
  to the exact bytes of the answer.
- `session.outcome.write`: one outcome record to the exact bytes of its file.

The session API left `attendance` in the phase 2 plan, row 21. The files
of its surfaces are frozen.

A body goes through the real routes of `attendance.api`. The service behind
the routes is a stand-in: it keeps the arguments of the one call that a
route makes, so no vector needs a token, a sandbox or a family.
`session_cases.py` holds the inputs.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final, cast

from attendance.api import build_app
from attendance.auth import Principal, TokenBook
from attendance.errors import ApiError, ErrorCode
from attendance.models import Trigger, TriggerKind
from attendance.paths import outcome_file
from attendance.service import SessionService
from starlette.testclient import TestClient

from attendance import outcomes
from vectors.core import (
    ENTRIES_MARKER,
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
from vectors.surfaces.session_cases import FAMILY, SESSION, TURN, Body, Refusal

CONTRACT: Final = "contract 02"

#: The message of the refusal that the stand-in service answers each call with.
TAKEN: Final = "the stand-in service took the call"

#: The route that each refusal of `session.error_body` goes through. Its body
#: is valid, so the route calls the stand-in service, which raises.
JOBS: Final = "/dispatch/jobs"
JOBS_BODY: Final = b'{"caller_family":"chat"}'

JSON_BODY: Final = {"content-type": "application/json"}


# --- the stand-in service and the real routes --------------------------------------------


@dataclass(frozen=True)
class Call:
    """One call of a route to the service."""

    name: str
    args: tuple[object, ...]
    kwargs: dict[str, object]


class StandIn:
    """Stands for `SessionService` behind the routes of `attendance.api`.

    It keeps the arguments of each call. Then it refuses with `refusal`. A
    route therefore parses its request with the real parser, and nothing
    after the parser runs.
    """

    def __init__(self) -> None:
        self.calls: list[Call] = []
        self.refusal = ApiError(ErrorCode.NOT_IMPLEMENTED, TAKEN)

    def reset(self) -> None:
        self.calls.clear()
        self.refusal = ApiError(ErrorCode.NOT_IMPLEMENTED, TAKEN)

    def __getattr__(self, name: str) -> Callable[..., object]:
        def take(*args: object, **kwargs: object) -> object:
            self.calls.append(Call(name, args, kwargs))
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


# --- the error body and the three objects of an answer -------------------------------------

_NOTE_SORTED: Final = (
    "Every object of free form inside args has its keys in sorted order. A vector file "
    "sorts keys, and the Python code keeps the order that it is given."
)
_NOTE_OUTPUT: Final = "output is the exact bytes that the Python code writes."
_NOTE_REPLY_TIME: Final = (
    "A time in args is in the ISO 8601 form of Python. The generator reads it with "
    "datetime.fromisoformat."
)


def _sorted_at_each_level(value: object) -> bool:
    if isinstance(value, dict):
        typed = cast("dict[str, object]", value)
        keys = list(typed)

        return keys == sorted(keys) and all(_sorted_at_each_level(item) for item in typed.values())

    if isinstance(value, list):
        return all(_sorted_at_each_level(item) for item in cast("list[object]", value))

    return True


def _detail_arg(refusal: Refusal) -> Json:
    """The detail of a refusal, in a form that keeps the order of its keys.

    A vector file sorts keys. A detail whose keys are not in sorted order is
    an `$entries` marker: its members as pairs, in the order of the writer.
    """
    detail = refusal.detail
    if detail is None or refusal.holder_block or _sorted_at_each_level(detail):
        return normalize(detail)

    if not all(_sorted_at_each_level(value) for value in detail.values()):
        raise ValueError(f"{refusal.id}: an object inside a detail has its keys in sorted order")

    return {ENTRIES_MARKER: [[key, normalize(value)] for key, value in detail.items()]}


def _refusal_vector(door: Door, refusal: Refusal) -> Vector:
    args: dict[str, object] = {
        "code": refusal.code,
        "message": refusal.message,
        "family": refusal.family,
        "session": refusal.session,
        "turn": refusal.turn,
    }
    plain = cast("dict[str, Json]", normalize(args))
    given: dict[str, Json] = {"args": {**plain, "detail": _detail_arg(refusal)}}
    door.service.reset()
    door.service.refusal = ApiError(
        refusal.code,
        refusal.message,
        family=refusal.family,
        session=refusal.session,
        turn=refusal.turn,
        detail=refusal.detail,
    )
    answer = attempt(lambda: door.send("POST", JOBS, JOBS_BODY))
    if isinstance(answer, Raised):
        return raised(refusal.id, given, answer.exc)

    output = text_input(answer.content.decode("utf-8"))

    return accepted(refusal.id, given, http_status=answer.status, output=output)


def _time(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _maybe_time(text: str | None) -> datetime | None:
    return _time(text) if text is not None else None


# --- the outcome record (§13.1) ----------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    """The fields of one outcome record."""

    id: str
    args: dict[str, Any] = field(default_factory=dict[str, Any])


#: A text of `spend_reason` that the service wrote before the phase 2 plan,
#: row 21. The writer takes any text.
_SPEND_UNKNOWN: Final = (
    "tokens were used but no cost was reported; the advisory number is unreliable "
    "and this layer does not read LiteLLM, spend's one authority (contract 05 §7)"
)

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
        {**_OUTCOME_ARGS, "spend_usd": None, "spend_reason": _SPEND_UNKNOWN},
    ),
    Outcome("spend-zero", {**_OUTCOME_ARGS, "spend_usd": 0.0}),
    Outcome("spend-small", {**_OUTCOME_ARGS, "spend_usd": 0.000015}),
    Outcome("spend-long-float", {**_OUTCOME_ARGS, "spend_usd": cases.LONG_FLOAT}),
    Outcome("no-sandbox", {**_OUTCOME_ARGS, "sandbox": ""}),
    Outcome(
        "times-before-the-year-1000",
        {
            **_OUTCOME_ARGS,
            "trigger": {"kind": "timer", "name": None, "fired_at": "0001-01-01T00:00:00+00:00"},
            "started_at": "0099-01-02T03:04:05+00:00",
            "ended_at": "0999-12-31T23:59:59+00:00",
        },
    ),
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


def _door_surfaces(door: Door) -> tuple[Surface, ...]:
    return (
        *(_request_surface(door, route) for route in ROUTES),
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
                "Each other detail has free form, and the Python code keeps the order of "
                "its keys. A detail whose keys are not in sorted order is an $entries marker: "
                "its members as pairs, in that order. An object inside a detail has its keys "
                "in sorted order.",
                "http_status is the HTTP status of the answer.",
                _NOTE_OUTPUT,
            ),
            vectors=_vectors(cases.REFUSALS, lambda refusal: _refusal_vector(door, refusal)),
        ),
    )


def _file_surfaces(scratch: Path) -> tuple[Surface, ...]:
    return (
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


def surfaces() -> tuple[Surface, ...]:
    door = Door()
    try:
        through_the_door = _door_surfaces(door)
    finally:
        door.close()

    with tempfile.TemporaryDirectory(prefix="vectors-session-") as scratch:
        on_disk = _file_surfaces(Path(scratch))

    return (*through_the_door, *on_disk)
