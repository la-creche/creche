"""The HTTP surface (contract 02 §3, §5).

Eleven operations for the doors and the noticeboard, plus the single internal path
`caregiver` calls. The shapes come from the contract, not from a framework:
every body is parsed by `requests.py` so one refusal table serves every route.

`attendance` never serves the OpenAI shape. A door does that (contract 02 §1).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from . import requests as parse
from .atomic import as_object
from .auth import Principal, TokenBook
from .errors import ApiError, ErrorCode
from .models import JournalLine
from .requests import Wait
from .service import SessionService
from .streams import Follow, StreamEnd
from .turns import LiveTurn

NDJSON = "application/x-ndjson"
DOOR_INSTANCE_HEADER = "X-Door-Instance"

HTTP_OK = 200
HTTP_CREATED = 201
HTTP_ACCEPTED = 202

_LF = "\n"

# Contract 02 §14's `message` for `internal`. It holds no text of the
# exception: that text can name a path or a value of the host.
_INTERNAL_MESSAGE = "an error in attendance stopped this request"


def build_app(service: SessionService, tokens: TokenBook) -> FastAPI:
    """One app over one service. The caller owns both lifetimes."""
    app = FastAPI(title="attendance", docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(ApiError)
    async def _refused(_: Request, error: ApiError) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        """Contract 02 §14's one body shape, for every failure."""
        return JSONResponse(status_code=error.status, content=error.body())

    @app.exception_handler(Exception)
    async def _failed(_: Request, __: Exception) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        """Contract 02 §14's `internal`, for an exception that no route expects.

        A door reads the same body for each failure. The framework raises the
        exception again after this answer, so the server still logs it.

        CONTRACT-QUESTION: contract 02 §14 gives the code and the status of
        `internal`, and no rule for the other fields. This reading answers
        null for `family`, `session` and `turn`, an empty `detail` and a
        fixed message. The handler holds no validated id, and the text of
        the exception can name a path or a value of the host. A reading that
        names the family or the session must validate each one first.
        """
        refusal = ApiError(ErrorCode.INTERNAL, _INTERNAL_MESSAGE)
        return JSONResponse(status_code=refusal.status, content=refusal.body())

    @app.post("/v1/sessions")
    async def _create(request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        principal, _ = _caller(tokens, request)
        body, created = service.create_or_find(principal, parse.read_create(await _json(request)))
        return JSONResponse(status_code=HTTP_CREATED if created else HTTP_OK, content=body)

    @app.get("/v1/sessions")
    async def _list(request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        principal, _ = _caller(tokens, request)
        query = parse.read_list_query(
            family=request.query_params.get("family"),
            state=request.query_params.get("state"),
            kind=request.query_params.get("kind"),
            limit=_int_param(request, "limit"),
            cursor=request.query_params.get("cursor"),
        )
        return JSONResponse(content=service.list_sessions(principal, query))

    @app.get("/v1/sessions/{family}/{session}")
    async def _get(family: str, session: str, request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        principal, _ = _caller(tokens, request)
        wanted = parse.read_turns_wanted(_int_param(request, "turns"))
        return JSONResponse(content=service.get_session(principal, family, session, wanted))

    @app.delete("/v1/sessions/{family}/{session}")
    async def _delete(family: str, session: str, request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        principal, _ = _caller(tokens, request)
        await service.delete_session(principal, family, session)
        return JSONResponse(content={"deleted": True})

    @app.post("/v1/sessions/{family}/{session}/writer")
    async def _writer(family: str, session: str, request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        principal, door = _caller(tokens, request)
        wanted = parse.read_writer(await _json(request), family, session)
        return JSONResponse(content=service.take_writer(principal, family, session, wanted, door))

    @app.delete("/v1/sessions/{family}/{session}/writer")
    async def _release_writer(family: str, session: str, request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        """Contract 02 §5.10. No body: the token and the header name the caller."""
        principal, door = _caller(tokens, request)
        return JSONResponse(content=service.release_writer(principal, family, session, door))

    @app.post("/v1/sessions/{family}/{session}/release-process")
    async def _release_process(family: str, session: str, request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        """Contract 02 §5.11. Closes a pi process, never a session."""
        principal, door = _caller(tokens, request)
        return JSONResponse(content=await service.release_process(principal, family, session, door))

    @app.post("/v1/sessions/{family}/{session}/turns")
    async def _run_turn(family: str, session: str, request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        principal, door = _caller(tokens, request)
        wanted = parse.read_run_turn(await _json(request), family, session)
        live = await service.run_turn(principal, family, session, wanted, door)
        return await _turn_response(service, principal, family, session, live, wanted.wait)

    @app.get("/v1/sessions/{family}/{session}/events")
    async def _events(family: str, session: str, request: Request) -> StreamingResponse:  # pyright: ignore[reportUnusedFunction]
        principal, _ = _caller(tokens, request)
        stream = service.stream(
            principal,
            family,
            session,
            from_seq=parse.read_from_seq(_int_param(request, "from_seq")),
            turn=request.query_params.get("turn"),
            follow=_follow(request),
        )
        return StreamingResponse(_ndjson(stream), media_type=NDJSON)

    @app.post("/v1/sessions/{family}/{session}/turns/{turn}/steer")
    async def _steer(family: str, session: str, turn: str, request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        principal, door = _caller(tokens, request)
        message = parse.read_steer(await _json(request), family, session)
        await service.steer(principal, family, session, turn, message, door)
        return JSONResponse(content={"steered": True})

    @app.post("/v1/sessions/{family}/{session}/turns/{turn}/stop")
    async def _stop(family: str, session: str, turn: str, request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        principal, door = _caller(tokens, request)
        reason = parse.read_stop_reason(await _json(request), family, session)
        return JSONResponse(
            content=await service.stop_turn(principal, family, session, turn, reason, door)
        )

    @app.post("/delegate")
    async def _delegate(request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        """Contract 04 §7.3. The PEP's one session path."""
        principal, door = _caller(tokens, request)
        wanted = parse.read_delegate(await _json(request))
        return JSONResponse(content=await service.run_delegate(principal, wanted, door))

    @app.post("/dispatch")
    async def _dispatch(request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        """Contract 02 §13.4.1. One family enqueues a job in another."""
        principal, door = _caller(tokens, request)
        wanted = parse.read_dispatch(await _json(request))
        return JSONResponse(content=await service.run_dispatch(principal, wanted, door))

    @app.post("/dispatch/jobs")
    async def _jobs(request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        """Contract 02 §13.4.2. A POST for a read: the caller family is a body
        field the token is checked against, exactly as on `/dispatch`, so one
        reader shape serves both and no scope rides in a URL."""
        principal, _ = _caller(tokens, request)
        return JSONResponse(
            content=service.read_jobs(principal, parse.read_jobs(await _json(request)))
        )

    @app.post("/internal/switch-sandbox")
    async def _switch(request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        principal, _ = _caller(tokens, request)
        return JSONResponse(
            content=await service.switch_sandbox(principal, parse.read_switch(await _json(request)))
        )

    return app


async def _turn_response(
    service: SessionService,
    principal: Principal,
    family: str,
    session: str,
    live: LiveTurn,
    wait: Wait,
) -> Response:
    """Contract 02 §5.4's three shapes, one per wait mode."""
    if wait is Wait.ACCEPTED:
        return JSONResponse(status_code=HTTP_ACCEPTED, content=service.accepted_body(live))

    if wait is Wait.SETTLED:
        return JSONResponse(content=await service.wait_settled(live))

    # A repeat under the same idempotency key streams the whole turn, not its
    # tail, so the replay starts at the turn's own first line (§6 rule 4).
    stream = service.stream(
        principal,
        family,
        session,
        from_seq=max(live.first_seq - 1, 0),
        turn=live.record.turn,
        end=StreamEnd.AFTER_TERMINAL,
    )
    return StreamingResponse(_ndjson(stream), media_type=NDJSON)


async def _ndjson(lines: AsyncIterator[JournalLine]) -> AsyncIterator[bytes]:
    """One JSON object per line, LF only (contract 02 §8)."""
    async for line in lines:
        yield (json.dumps(line.to_api(), separators=(",", ":")) + _LF).encode("utf-8")


def _caller(tokens: TokenBook, request: Request) -> tuple[Principal, str]:
    """Who is calling, and which of that door's processes.

    Contract 02 §7.1: `door_instance` comes from the optional
    `X-Door-Instance` header and falls back to the door's own name, so two
    processes of one door share a lease rather than fight over it.
    """
    principal = tokens.identify(request.headers.get("authorization"))
    instance = request.headers.get(DOOR_INSTANCE_HEADER, "").strip()
    return principal, instance or principal.value


async def _json(request: Request) -> dict[str, Any]:
    """The body, or `bad_request`. A door's body is untrusted input."""
    try:
        raw: object = await request.json()
    except (ValueError, UnicodeDecodeError, RecursionError) as error:
        raise ApiError(ErrorCode.BAD_REQUEST, parse.NOT_JSON) from error

    record = as_object(raw)

    if record is None:
        raise ApiError(ErrorCode.BAD_REQUEST, "body is not a JSON object")

    return record


def _int_param(request: Request, name: str) -> int | None:
    text = request.query_params.get(name)

    if text is None:
        return None

    try:
        return int(text)
    except ValueError as error:
        raise ApiError(ErrorCode.BAD_REQUEST, f"{name} is not a number") from error


def _follow(request: Request) -> Follow:
    """`follow` defaults to true (contract 02 §5.5)."""
    text = request.query_params.get("follow", "true").strip().lower()

    if text in {"0", "false", "no"}:
        return Follow.REPLAY_ONLY

    return Follow.KEEP_OPEN
