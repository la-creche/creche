# pyright: reportUnnecessaryTypeIgnoreComment=true
"""The typing check of the route constants (decision CH25).

Nothing imports this module, and nothing calls its functions. pyright reads
it in strict mode in `bin/quality-gate.sh`. Each function makes one wrong use
of a route, and its ignore names the error that pyright must report. A change
that widens `JsonRoute` takes the error away. The ignore is then unnecessary,
and pyright fails the gate.
"""

from pydantic import BaseModel

from .models import CreateError, CreateSession, SessionRow
from .route import JsonRoute
from .routes import CREATE


def a_wrong_request(row: SessionRow) -> bytes:
    """`encode` refuses a model that is not the request of the route."""
    return CREATE.encode(row)  # pyright: ignore[reportArgumentType]


def a_wrong_reply() -> JsonRoute[CreateSession, CreateSession, CreateError]:
    """A route does not pass for a route of a different reply model."""
    return CREATE  # pyright: ignore[reportReturnType]


def a_wider_request() -> JsonRoute[BaseModel, SessionRow, CreateError]:
    """`Request` is invariant, so a route does not pass for a route of a
    wider request model. `encode` makes it invariant."""
    return CREATE  # pyright: ignore[reportReturnType]
