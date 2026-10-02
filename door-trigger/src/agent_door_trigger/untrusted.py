"""Narrowing helpers for JSON that crossed a process boundary.

Every byte from `sessiond` and from a status document is untrusted input:
its shape is checked before a field is read (invariants 12 and 14).
`isinstance(value, dict)` alone narrows to `dict[Unknown, Unknown]` under a
strict type checker, which hides exactly the mistake these checks exist to
catch. A `TypeGuard` states the shape instead.
"""

from __future__ import annotations

from typing import TypeGuard


def is_object(value: object) -> TypeGuard[dict[str, object]]:
    """True for a JSON object. Its keys are strings by construction."""
    return isinstance(value, dict)


def as_object(value: object) -> dict[str, object]:
    """A JSON object, or an empty one. Never raises."""
    return value if is_object(value) else {}


def as_text(value: object) -> str:
    """A JSON string, or the empty string. Never raises."""
    return value if isinstance(value, str) else ""


def field_text(source: dict[str, object], field: str) -> str:
    """One string field of an object, or the empty string."""
    return as_text(source.get(field))


def is_list(value: object) -> TypeGuard[list[object]]:
    """True for a JSON array. Its members are still untrusted."""
    return isinstance(value, list)
