"""The pydantic models of the wire types of the VM owner (decision CH9-1).

`models.py` is generated from `rust/crates/wire/schema/wire.json`. Never edit
it by hand. `wire/AGENTS.md` has the command that writes it.

`read` is the one way to read a wire text (`reader.py`)."""

from .models import (
    Auto,
    AutoJobId,
    AutoOrigin,
    Delegate,
    DelegateOrigin,
    DelegationId,
    Dispatch,
    DispatchChain,
    DispatchTrigger,
    Door,
    DoorOrigin,
    FamilyName,
    JobId,
    SessionId,
    SessionOrigin,
    Timer,
    TimerTrigger,
    Trigger,
    UnixMillis,
    Webhook,
    WebhookName,
    WebhookTrigger,
)
from .reader import read
from .strictjson import NotStrictJson

__all__ = [
    "Auto",
    "AutoJobId",
    "AutoOrigin",
    "Delegate",
    "DelegateOrigin",
    "DelegationId",
    "Dispatch",
    "DispatchChain",
    "DispatchTrigger",
    "Door",
    "DoorOrigin",
    "FamilyName",
    "JobId",
    "NotStrictJson",
    "SessionId",
    "SessionOrigin",
    "Timer",
    "TimerTrigger",
    "Trigger",
    "UnixMillis",
    "Webhook",
    "WebhookName",
    "WebhookTrigger",
    "read",
]
