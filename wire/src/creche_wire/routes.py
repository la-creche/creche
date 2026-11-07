# Generated from the route table of rust/crates/wire (decision CH25). Do not edit.
# To write it again, run `CRECHE_WIRE_SCHEMA=write cargo test -p creche-wire` in rust/.
"""The routes of the vmm host API, each with its request, reply and error
types (decision CH25). A client sends `HOST_API_PROTOCOL` in the header
`HOST_API_HEADER`."""

from typing import Final

from .models import (
    AcpError,
    CheckError,
    CheckReply,
    CheckRequest,
    CreateError,
    CreateSession,
    DropError,
    Dropped,
    DropTarget,
    ListError,
    ListSessions,
    OpenAcp,
    SessionPage,
    SessionRow,
)
from .route import JsonRoute, UpgradeRoute

HOST_API_PROTOCOL: Final = 2
HOST_API_HEADER: Final = "creche-host-api-protocol"
HOST_API_BODY_CAP: Final = 1048576
MISMATCH_STATUS: Final = 412

ACP: Final = UpgradeRoute(
    name="acp",
    token="creche-acp",
    request=OpenAcp,
    error=AcpError,
)

CHECK: Final = JsonRoute(
    name="check",
    request=CheckRequest,
    reply=CheckReply,
    error=CheckError,
)

CREATE: Final = JsonRoute(
    name="create",
    request=CreateSession,
    reply=SessionRow,
    error=CreateError,
)

DROP: Final = JsonRoute(
    name="drop",
    request=DropTarget,
    reply=Dropped,
    error=DropError,
)

DROP_CLIENT_TIMEOUT_S: Final = 60

LIST: Final = JsonRoute(
    name="list",
    request=ListSessions,
    reply=SessionPage,
    error=ListError,
)
