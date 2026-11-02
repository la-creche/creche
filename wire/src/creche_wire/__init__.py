"""The pydantic models of the wire types of the VM owner (decision CH9-1).

`models.py` is generated from `rust/crates/wire/schema/wire.json`. Never edit
it by hand. `wire/AGENTS.md` has the command that writes it."""

from .models import SessionId

__all__ = ["SessionId"]
