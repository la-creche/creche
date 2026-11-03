"""The pydantic models of the wire types of the VM owner (decision CH9-1).

`models.py` is generated from `rust/crates/wire/schema/wire.json`. Never edit
it by hand. `wire/AGENTS.md` has the command that writes it.

`read` is the one way to read a wire text (`reader.py`)."""

from .models import (
    AutoJobId,
    Delegate,
    DelegateOrigin,
    DelegationId,
    Door,
    DoorOrigin,
    JobId,
    SessionId,
    SessionOrigin,
)
from .reader import read
from .strictjson import NotStrictJson

__all__ = [
    "AutoJobId",
    "Delegate",
    "DelegateOrigin",
    "DelegationId",
    "Door",
    "DoorOrigin",
    "JobId",
    "NotStrictJson",
    "SessionId",
    "SessionOrigin",
    "read",
]
