"""The facade's vector file: the creche extensions of ACP (phase 2 plan (d)).

The facade tests read the same file. A key or a shape changes in one place.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

VECTOR_FILE = Path(__file__).resolve().parents[2] / "facade" / "vectors" / "creche-extensions.json"


def session_meta() -> dict[str, Any]:
    """The `session_meta` table: each creche key of `_meta`, by its role."""
    vectors = cast("dict[str, Any]", json.loads(VECTOR_FILE.read_text(encoding="utf-8")))
    return cast("dict[str, Any]", vectors["session_meta"])


def meta_key(role: str) -> str:
    """The `_meta` key of one role: `job`, `delegation` or `trigger`."""
    return str(session_meta()[role]["key"])


def value_fields(role: str) -> set[str]:
    """The fields of the vector's value for one role, an object."""
    return set(cast("dict[str, Any]", session_meta()[role]["value"]))
