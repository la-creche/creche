"""The HTTP surface (contract 02 §3, §12, §13.4, contract 05 §5).

Three operations for the PEP's two doors, the single internal path
`caregiver` calls, and the ACP door on the Unix socket. The shapes come from
the contract, not from a framework: every body is parsed by `requests.py` so
one refusal table serves every route.

`attendance` never serves the OpenAI shape. A door does that (contract 02 §1).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse

from . import acp_door
from . import requests as parse
from .atomic import as_object
from .auth import Principal, TokenBook
from .errors import ApiError, ErrorCode
from .service import SessionService

# Contract 02 §14's `message` for `internal`. It holds no text of the
# exception: that text can name a path or a value of the host.
_INTERNAL_MESSAGE = "an error in attendance stopped this request"


class Listener(StrEnum):
    """Where an app is served. Only the Unix socket carries the ACP door."""

    SOCKET = "socket"
    LAN = "lan"


def build_app(
    service: SessionService, tokens: TokenBook, listener: Listener = Listener.LAN
) -> FastAPI:
    """One app over one service. The caller owns both lifetimes."""
    app = FastAPI(title="attendance", docs_url=None, redoc_url=None, openapi_url=None)

    if listener is Listener.SOCKET:
        _add_acp_door(app, service, tokens)

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

    @app.post("/delegate")
    async def _delegate(request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        """Contract 04 §7.3. The PEP's one session path."""
        principal = _caller(tokens, request)
        wanted = parse.read_delegate(await _json(request))
        return JSONResponse(content=await service.run_delegate(principal, wanted))

    @app.post("/dispatch")
    async def _dispatch(request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        """Contract 02 §13.4.1. One family enqueues a job in another."""
        principal = _caller(tokens, request)
        wanted = parse.read_dispatch(await _json(request))
        return JSONResponse(content=await service.run_dispatch(principal, wanted))

    @app.post("/dispatch/jobs")
    async def _jobs(request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        """Contract 02 §13.4.2. A POST for a read: the caller family is a body
        field the token is checked against, exactly as on `/dispatch`, so one
        reader shape serves both and no scope rides in a URL."""
        principal = _caller(tokens, request)
        return JSONResponse(
            content=service.read_jobs(principal, parse.read_jobs(await _json(request)))
        )

    @app.post("/internal/switch-sandbox")
    async def _switch(request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        principal = _caller(tokens, request)
        return JSONResponse(
            content=await service.switch_sandbox(principal, parse.read_switch(await _json(request)))
        )

    return app


def _add_acp_door(app: FastAPI, service: SessionService, tokens: TokenBook) -> None:
    @app.websocket("/acp/{family}")
    async def _acp(websocket: WebSocket, family: str) -> None:  # pyright: ignore[reportUnusedFunction]
        """Phase 2 plan, row 17. The bearer and the family before any `sbx exec`.

        The upgrade is accepted first, so that a refusal can carry its code.
        No frame is read before the checks pass.
        """
        await websocket.accept()

        try:
            principal = tokens.identify(websocket.headers.get("authorization"))
            relay = await service.open_acp_relay(principal, family)
        except ApiError as error:
            await websocket.close(code=acp_door.refusal_code(error), reason=error.code.value)
            return

        await acp_door.pipe(websocket, relay)


def _caller(tokens: TokenBook, request: Request) -> Principal:
    """Who is calling. The token names the door (contract 02 §3.1)."""
    return tokens.identify(request.headers.get("authorization"))


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
