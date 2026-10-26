"""The door's ASGI app: `GET /healthz` and `WS /acp/<family>`.

The door forwards ACP frames between a WebSocket client and `attendance`'s
`WS /acp/{family}`, and translates nothing. Each check here reads the
request line and the headers only, and each refusal comes before the
upgrade. `forward.py` owns the frames.
"""

from __future__ import annotations

import hmac
import re

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket

from .config import DoorConfig
from .forward import Upstream, forward

#: The family path grammar: `/acp/<family>`. Read with `fullmatch` only.
ACP_PREFIX = "/acp/"
FAMILY_PATTERN = re.compile(r"[a-z][a-z0-9-]{1,30}")

#: The header that the reverse proxy sets after Authentik's forward auth
#: passes. The door reads its presence only, never its value as an identity.
#: The check runs only while `require_authentik` is true: the chat-UI
#: deployment sets it. A terminal deployment leaves it off.
FORWARD_AUTH_HEADER = "x-authentik-username"

_BEARER = b"Bearer "
_LATIN_1 = "latin-1"


def create_app(config: DoorConfig) -> Starlette:
    """The app for one config. The key stays in this closure."""
    expected = _BEARER + config.door_key
    upstream = Upstream(config.attendance_socket, config.attendance_token)

    async def healthz(request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def acp(websocket: WebSocket) -> None:
        if not _authorized(websocket, expected, require_authentik=config.require_authentik):
            await websocket.send_denial_response(Response(status_code=401))
            return

        # The path itself, not `path_params`: Starlette's route regex ends
        # in `$`, so its parameter drops a final newline.
        family = str(websocket.scope["path"]).removeprefix(ACP_PREFIX)
        if FAMILY_PATTERN.fullmatch(family) is None:
            await websocket.send_denial_response(Response(status_code=404))
            return

        await websocket.accept()
        await forward(websocket, family, upstream)

    return Starlette(
        routes=[
            Route("/healthz", healthz, methods=["GET"]),
            WebSocketRoute(ACP_PREFIX + "{family:path}", acp),
        ]
    )


def _authorized(websocket: WebSocket, expected: bytes, *, require_authentik: bool) -> bool:
    """Both checks run every time, so the time taken says nothing about which failed."""
    offered = websocket.headers.get("authorization", "").encode(_LATIN_1)
    bearer = hmac.compare_digest(offered, expected)
    forwarded = websocket.headers.get(FORWARD_AUTH_HEADER, "").strip() != ""

    return bearer and (forwarded or not require_authentik)
