"""The FastAPI app: wires config, auth, the picker and `attendance` into the
OpenAI surface contract 02 promises Open WebUI.

One request is one turn. This module owns request/response shape and
sequencing; it holds no session state itself (`attendance` does) and no
presentation logic itself (`translate.py`/`sse.py` do). Its own job is
narrow: authenticate, validate, call `attendance` in the right order, and
turn whatever comes back into the OpenAI shape.
"""

from __future__ import annotations

import hmac
import logging
import time
import uuid
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .attendance import AttendanceClient, AttendanceError, StreamBroken, TurnRequest
from .config import DoorConfig
from .errors import (
    HTTP_BAD_REQUEST,
    HTTP_UNAUTHORIZED,
    DoorError,
    ErrorType,
    from_attendance,
    turn_failure,
)
from .families import FamilyDirectory
from .headers import read_ids
from .journal import JournalLine
from .openai_api import ChatRequest, completion_body, models_body, parse_chat_request
from .sse import SseWriter
from .stream import with_keepalive
from .translate import TurnTranslator

_LOG = logging.getLogger(__name__)

_BEARER_PREFIX = "Bearer "
_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
_SSE_MEDIA_TYPE = "text/event-stream"

# Contract 02 §10.2: a request that carried `parent_id` and got back exactly
# this code (§14, HTTP 501) is retried once, without `parent_id`. Every
# other error propagates unchanged.
_NOT_IMPLEMENTED_CODE = "not_implemented"

# Not a contract 02 §14 reason: those describe why attendance ended a turn.
# This one is local — the connection or the framing between this door and
# attendance broke while a line stream was already open (StreamBroken, or a
# network error from httpx). A failure is never silent, and that includes
# a failure that is the door's own.
_LOCAL_STREAM_BREAK_REASON = "door_lost_the_attendance_stream"

# Contract 02 §10.3: the door answers a background request itself, with a
# 400, `background_task_not_supported`, and a message naming the fix, so a
# reader can act on it without reading a log.
#
# The fix names BOTH task models, because Open WebUI picks one by the
# CONNECTION TYPE of the chat's own model. With a connection typed `local`
# and an empty local task model, every background request falls back to the
# chat's model, which is this door, however the external task model is set.
# A message that says "set the Task Model" sends such a reader to a page
# that already looks right.
_BACKGROUND_TASK_MESSAGE = (
    "this connection also received a background request (a title, tag or "
    "follow-up), and a background request must not start an agent turn. "
    "Open WebUI picks the task model by the connection type of the chat's "
    "model, and an empty one falls back to this connection. In Open WebUI, "
    "under Admin Settings > Interface > Task Model, set BOTH the local and "
    "the external task model to a plain model."
)


def create_app(
    config: DoorConfig, attendance: AttendanceClient, families: FamilyDirectory
) -> FastAPI:
    """Build the door's ASGI app. One call per process; the three
    collaborators are fixed for the app's lifetime (invariant 5: the door
    holds no session state of its own to keep separate per request)."""
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    @app.exception_handler(DoorError)
    async def _on_door_error(  # pyright: ignore[reportUnusedFunction]
        _request: Request, exc: DoorError
    ) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content=exc.body())

    @app.exception_handler(AttendanceError)
    async def _on_attendance_error(  # pyright: ignore[reportUnusedFunction]
        _request: Request, exc: AttendanceError
    ) -> JSONResponse:
        mapped = from_attendance(exc.code, exc.message)
        return JSONResponse(status_code=mapped.status, content=mapped.body())

    @app.get("/v1/models")
    async def list_models(request: Request) -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        _authenticate(request, config.door_key)
        return models_body(families.serving())

    # response_model=None: the two success shapes (a JSON body, an SSE
    # stream) are already-built Response objects, not a Pydantic field for
    # FastAPI to validate against — trying to build one from the Union
    # itself raises at startup, before any request arrives.
    @app.post("/v1/chat/completions", response_model=None)
    async def chat_completions(  # pyright: ignore[reportUnusedFunction]
        request: Request,
    ) -> JSONResponse | StreamingResponse:
        _authenticate(request, config.door_key)
        ids = read_ids(dict(request.headers))
        if ids.is_background_task:
            raise _background_task_refusal()

        chat = parse_chat_request(await _json_body(request))
        turn_request = TurnRequest(
            family=chat.family,
            session=ids.session,
            prompt=chat.prompt,
            idempotency_key=ids.message_id,
            persona=chat.persona,
            chat_id=ids.chat_id,
            message_id=ids.message_id,
            user_message_id=ids.user_message_id,
            parent_id=ids.parent_id,
        )

        await attendance.ensure_session(chat.family, ids.session)
        if chat.stream:
            return await _streamed(attendance, turn_request, chat)

        return await _settled(attendance, turn_request, chat)

    return app


def _authenticate(request: Request, door_key: str) -> None:
    """Bearer auth with the door's own key, compared in constant time.

    The comparison itself never branches on how much of the token matched:
    `hmac.compare_digest` is documented to resist timing analysis even
    across inputs of different lengths, which is why the length is not
    checked separately first.
    """
    header = request.headers.get("authorization", "")
    if not header.startswith(_BEARER_PREFIX):
        raise _unauthorized()

    supplied = header[len(_BEARER_PREFIX) :].encode("utf-8")
    if not hmac.compare_digest(supplied, door_key.encode("utf-8")):
        raise _unauthorized()


def _unauthorized() -> DoorError:
    return DoorError(
        HTTP_UNAUTHORIZED,
        "missing or invalid bearer token for this connection.",
        code="unauthorized",
        error_type=ErrorType.AUTHENTICATION,
    )


def _background_task_refusal() -> DoorError:
    return DoorError(
        HTTP_BAD_REQUEST, _BACKGROUND_TASK_MESSAGE, code="background_task_not_supported"
    )


async def _json_body(request: Request) -> object:
    try:
        return await request.json()
    except (ValueError, RecursionError) as exc:
        # RecursionError: a body that nests too deep is not a ValueError.
        raise DoorError(
            HTTP_BAD_REQUEST, "the request body is not valid JSON.", code="bad_body"
        ) from exc


def _is_branch_fallback(request: TurnRequest, exc: AttendanceError) -> bool:
    """True when this is the one case contract 02 §10.2 asks the door to
    recover from itself: a branching attempt attendance cannot yet serve."""
    return request.parent_id is not None and exc.code == _NOT_IMPLEMENTED_CODE


async def _settled(
    attendance: AttendanceClient, request: TurnRequest, chat: ChatRequest
) -> JSONResponse:
    try:
        result = await attendance.settled_turn(request)
    except AttendanceError as exc:
        if not _is_branch_fallback(request, exc):
            raise
        _log_branch_fallback(request)
        result = await attendance.settled_turn(request, with_parent=False)

    if not result.is_settled:
        raise turn_failure(result.reason or result.state)

    body = completion_body(
        f"chatcmpl-{uuid.uuid4()}", int(time.time()), chat.model, result.text, result.usage
    )
    return JSONResponse(content=body)


async def _streamed(
    attendance: AttendanceClient, request: TurnRequest, chat: ChatRequest
) -> StreamingResponse:
    writer = SseWriter(f"chatcmpl-{uuid.uuid4()}", int(time.time()), chat.model)
    relay = with_keepalive(_stream_turn_frames(attendance, request, writer))

    # Priming the relay for its first frame opens the attendance connection
    # right here, in this coroutine, before any bytes go to the client. A
    # StreamingResponse locks in its status the instant it is constructed
    # (Starlette sends "http.response.start" before pulling the first
    # chunk), so this is the only point where a refusal — session_busy and
    # its HTTP 409 above all — can still become a real status code instead
    # of a 200 stream that opens with an error frame.
    first = await anext(relay)

    return StreamingResponse(
        _prepend(first, relay), media_type=_SSE_MEDIA_TYPE, headers=_SSE_HEADERS
    )


async def _prepend(first: str, rest: AsyncIterator[str]) -> AsyncIterator[str]:
    yield first
    async for item in rest:
        yield item


async def _stream_turn_frames(
    attendance: AttendanceClient, request: TurnRequest, writer: SseWriter
) -> AsyncIterator[str]:
    """One turn's frames, end to end.

    This whole generator — opening the attendance stream, translating every
    line, and closing it again — runs inside whichever single task iterates
    it. `with_keepalive` iterates it from one task it owns for that exact
    reason (its own docstring explains why splitting entry and exit across
    tasks breaks anyio's cancel scopes), so the retry below stays inside
    this same function rather than a second helper on the outside.
    """
    translator = TurnTranslator(writer)

    try:
        async with attendance.stream_turn(request) as lines:
            async for frame in _drive(translator, lines):
                yield frame
            return
    except AttendanceError as exc:
        if not _is_branch_fallback(request, exc):
            raise
        _log_branch_fallback(request)

    async with attendance.stream_turn(request, with_parent=False) as lines:
        async for frame in _drive(translator, lines):
            yield frame


async def _drive(
    translator: TurnTranslator, lines: AsyncIterator[JournalLine]
) -> AsyncIterator[str]:
    for frame in translator.start():
        yield frame

    try:
        async for line in lines:
            for frame in translator.feed(line):
                yield frame
            if translator.settled:
                return
    except StreamBroken as exc:
        # The turn itself may still be running on the host (invariant 4) —
        # only the reading stopped. A failure is never silent: the reader
        # must see this, not a stream that quietly cuts off mid-answer.
        _LOG.warning("attendance stream broke mid-turn: %s", exc)
        for frame in translator.fail(_LOCAL_STREAM_BREAK_REASON):
            yield frame
        return

    for frame in translator.finish():
        yield frame


def _log_branch_fallback(request: TurnRequest) -> None:
    _LOG.info(
        "attendance cannot branch yet; retrying message %s without parent_id", request.message_id
    )
