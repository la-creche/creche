"""The webhook listener: `POST /triggers/<family>/<name>`.

One route pattern serves every declared webhook trigger; `routes.py`'s
table is what makes a specific `(family, name)` answer instead of 404.
Three failure shapes collapse to the SAME 404 on purpose: an unknown
family, an unknown trigger name, and a wrong token. Answering any of them
differently would tell a prober which names exist.

The route table refreshes on a timer and on SIGHUP (contract 02 §3 rule 8
does the same for a door's token files: "SIGHUP reloads... It does not
drop sessions or turns." A route reload drops nothing in flight either).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from .attendance import AttendanceClient
from .config import ServeConfig
from .errors import CODE_UNREACHABLE, AttendanceError, webhook_status
from .fire import Firing, TriggerKind, fire_trigger
from .payload import MAX_PAYLOAD_BYTES, PayloadInvalid, PayloadTooLarge, read_payload
from .routes import Route, RouteLookup
from .tokens import tokens_match

_LOG = logging.getLogger(__name__)

_BEARER_PREFIX = "Bearer "

# A fixed-length placeholder so an unknown route still pays one
# `tokens_match` call. Its value never matters: an offered token can only
# equal it by sending this exact string, which teaches an attacker
# nothing a 404 does not already say.
_DUMMY_TOKEN = "0" * 64


def _error(code: str, message: str) -> dict[str, dict[str, str]]:
    """This listener's one error shape, toward whatever external system
    posted the trigger. Not contract 02 §14's own body: that shape is
    between attendance and this door, and a code that started there
    (`webhook_status`'s input) is carried through, never restated."""
    return {"error": {"code": code, "message": message}}


def create_app(config: ServeConfig, attendance: AttendanceClient, routes: RouteLookup) -> FastAPI:
    """Build the webhook listener's ASGI app. `routes` is refreshed once at
    startup, then on `config.refresh_s` and on SIGHUP, for the app's whole
    lifetime.
    """

    @asynccontextmanager
    async def _lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        await run_in_threadpool(routes.refresh)
        task = asyncio.create_task(_refresh_periodically(routes, config.refresh_s))
        _install_sighup(routes)
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None, lifespan=_lifespan)

    @app.post("/triggers/{family}/{name}")
    async def fire_webhook(  # pyright: ignore[reportUnusedFunction]
        family: str, name: str, request: Request
    ) -> JSONResponse:
        route = routes.get(family, name)
        offered = _bearer_value(request.headers.get("authorization"))
        if not _authorized(route, offered):
            # Same body, same status, whichever of the three reasons
            # applies.
            return JSONResponse(status_code=404, content=_error("not_found", "no such trigger"))

        if _declared_too_large(request.headers.get("content-length")):
            return JSONResponse(
                status_code=413,
                content=_error("payload_too_large", "request body is too large"),
            )

        body = await request.body()
        return await run_in_threadpool(_fire_webhook, attendance, family, name, body)

    return app


def _fire_webhook(
    attendance: AttendanceClient, family: str, name: str, body: bytes
) -> JSONResponse:
    try:
        payload = read_payload(body) if body else None
    except PayloadTooLarge as exc:
        return JSONResponse(status_code=413, content=_error("payload_too_large", str(exc)))
    except PayloadInvalid as exc:
        return JSONResponse(status_code=400, content=_error("bad_request", str(exc)))

    firing = Firing(family=family, kind=TriggerKind.WEBHOOK, name=name, payload=payload)

    try:
        outcome = fire_trigger(attendance, firing)
    except AttendanceError as exc:
        _LOG.warning(
            "trigger %s/%s: attendance refused: %s: %s", family, name, exc.code, exc.message
        )
        return JSONResponse(
            status_code=webhook_status(exc.code), content=_error(exc.code, exc.message)
        )
    except (OSError, httpx.HTTPError) as exc:
        # A dead socket, a refused connection, a timeout: attendance did not
        # refuse the job, it did not answer. The CLI maps the same failure
        # (`cli.py`).
        _LOG.warning(
            "trigger %s/%s: cannot reach attendance: %s: %s", family, name, type(exc).__name__, exc
        )
        return JSONResponse(
            status_code=webhook_status(CODE_UNREACHABLE),
            content=_error(CODE_UNREACHABLE, "attendance did not answer"),
        )

    _LOG.info(
        "trigger %s/%s: fired, session %s, state %s", family, name, outcome.session, outcome.state
    )
    return JSONResponse(
        status_code=202,
        content={"session": outcome.session, "turn": outcome.turn, "state": outcome.state},
    )


def _authorized(route: Route | None, offered: str | None) -> bool:
    """Always one `tokens_match` call, route or no route, so a timing
    difference cannot tell an attacker "no such route" from "wrong
    token": the token comparison's constant time, extended to the routing
    decision around it.
    """
    expected = route.token if route is not None else _DUMMY_TOKEN
    matched = offered is not None and tokens_match(offered, expected)

    return route is not None and matched


def _bearer_value(header: str | None) -> str | None:
    if header is None or not header.startswith(_BEARER_PREFIX):
        return None

    value = header[len(_BEARER_PREFIX) :].strip()
    return value if value else None


def _declared_too_large(content_length: str | None) -> bool:
    """A cheap guard before the body is read into memory. Not a full
    defense against a chunked request with no Content-Length — that needs
    a streaming read with a cap, which this listener does not have yet.
    The real cap is `payload.py`'s, checked again after the read.
    """
    if content_length is None:
        return False

    try:
        return int(content_length) > MAX_PAYLOAD_BYTES
    except ValueError:
        return False  # let the real read fail this instead of guessing


async def _refresh_periodically(routes: RouteLookup, interval_s: float) -> None:
    while True:
        await asyncio.sleep(interval_s)
        try:
            count = await run_in_threadpool(routes.refresh)
            _LOG.info("trigger routes refreshed: %d live", count)
        except Exception as exc:
            # One handler for each failure. A registry file that the loader
            # cannot read must not stop this task: it is the one mechanism
            # that always refreshes the routes.
            _LOG.warning(
                "trigger routes: refresh failed, keeping the last table (%s: %s)",
                type(exc).__name__,
                exc,
            )


def _install_sighup(routes: RouteLookup) -> None:
    """SIGHUP reloads the route table at once. It drops no request in
    flight: a lookup already in progress finished against whichever
    table `RouteTable.get` returned before the swap (`routes.py`).

    `add_signal_handler` raises `NotImplementedError` on a platform with
    no signal support (Windows) and `RuntimeError` off the main thread of
    the main interpreter — the second is not a platform gap, it is how a
    test harness (`starlette.testclient.TestClient`) runs this app's
    lifespan at all, in a worker thread. Either way SIGHUP becomes
    unavailable, never fatal: `_refresh_periodically` is the mechanism
    that always works, and this is a faster path on top of it, not a
    replacement for it.
    """
    loop = asyncio.get_running_loop()

    def reload_routes() -> None:
        try:
            count = routes.refresh()
            _LOG.info("trigger routes reloaded: %d live", count)
        except Exception as exc:
            _LOG.error(
                "trigger routes: reload refused, keeping the last table (%s: %s)",
                type(exc).__name__,
                exc,
            )

    try:
        loop.add_signal_handler(signal.SIGHUP, reload_routes)
    except (NotImplementedError, RuntimeError) as exc:
        _LOG.info("trigger routes: SIGHUP reload unavailable here (%s)", exc)
