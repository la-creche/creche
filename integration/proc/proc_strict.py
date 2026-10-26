"""The strict reader of one JSON text, for each program of this suite that reads JSON.

`json.loads` alone takes more than JSON: the tokens `NaN`, `Infinity` and
`-Infinity`, and a key that appears twice in one object, where the last
value wins. A reader that takes those hides a writer that sends them. So
each reader here refuses them.
"""

from __future__ import annotations

import json
from typing import Any, cast


def strict_object(text: str) -> dict[str, Any]:
    """One JSON object. Raises `ValueError` for any other text."""
    value: object = json.loads(text, parse_constant=_no_constant, object_pairs_hook=_no_duplicate)

    if not isinstance(value, dict):
        raise ValueError("the text is not one JSON object")

    return cast("dict[str, Any]", value)


def _no_constant(token: str) -> object:
    raise ValueError(f"{token} is no token of JSON")


def _no_duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]

    if len(set(keys)) != len(keys):
        raise ValueError("a key appears twice in one object")

    return dict(pairs)
