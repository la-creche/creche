"""Narrowing helpers for JSON that crossed a process boundary.

Every byte from `attendance` and from `caregiver`'s status document is untrusted
input: its shape is checked before a field is read (invariants 12 and 14).
`isinstance(value, dict)` alone narrows to `dict[Unknown, Unknown]` under a
strict type checker, which hides exactly the mistake these checks exist to
catch. A `TypeGuard` states the shape instead.
"""

from __future__ import annotations

from typing import TypeGuard


def is_object(value: object) -> TypeGuard[dict[str, object]]:
    """True for a JSON object. Its keys are strings by construction."""
    return isinstance(value, dict)


def is_list(value: object) -> TypeGuard[list[object]]:
    """True for a JSON array."""
    return isinstance(value, list)


def as_object(value: object) -> dict[str, object]:
    """A JSON object, or an empty one. Never raises."""
    return value if is_object(value) else {}


def as_list(value: object) -> list[object]:
    """A JSON array, or an empty one. Never raises."""
    return value if is_list(value) else []


def as_text(value: object) -> str:
    """A JSON string, or the empty string. Never raises."""
    return value if isinstance(value, str) else ""


def field_text(source: dict[str, object], field: str) -> str:
    """One string field of an object, or the empty string."""
    return as_text(source.get(field))


def field_int(source: dict[str, object], field: str) -> int:
    """One integer field of an object, or zero. `bool` is not an integer here."""
    value = source.get(field)

    if isinstance(value, bool) or not isinstance(value, int):
        return 0

    return value
