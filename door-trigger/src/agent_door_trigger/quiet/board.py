"""The board's fingerprint: every move the family acts on, and nothing else.

    #12:TODO:3:r:finance #14:TRIAGE:-:-:-

One `#<id>:<column>:<points|->:<r|->:<epic|->` per ticket, sorted by id,
then hashed so the gate's file stays small as DONE grows. Titles, assignees,
comments and the order inside a column are left out: none is a move the
lead acts on.

The input is `<server>__survey_board`'s answer. agent-mcp's
`lead/board.py` renders it as

    {"columns": {"TODO": [{"id": 12, "points": 3, "refined": true,
                           "epic": "finance", ...}], ...}}

Any other shape answers None, and the gate wakes on it."""

from __future__ import annotations

import hashlib
from typing import Final

from ..untrusted import as_object, is_list, is_object

NO_VALUE: Final = "-"
REFINED_MARK: Final = "r"


def fingerprint(answer: object) -> str | None:
    """The fingerprint's sha256, or None when `answer` is not a survey."""
    columns = as_object(answer).get("columns")
    if not is_object(columns):
        return None

    entries: list[tuple[int, str]] = []
    for column, tickets in columns.items():
        if not is_list(tickets):
            return None

        for ticket in tickets:
            entry = _entry(column, ticket)
            if entry is None:
                return None

            entries.append(entry)

    joined = " ".join(text for _, text in sorted(entries))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _entry(column: str, ticket: object) -> tuple[int, str] | None:
    """One ticket's part, keyed by id for the sort."""
    body = as_object(ticket)
    ident = body.get("id")
    points = body.get("points")
    refined = body.get("refined")
    epic = body.get("epic")

    # `bool` is an `int` in Python, and never an id or a point count.
    if not isinstance(ident, int) or isinstance(ident, bool):
        return None

    if points is not None and (not isinstance(points, int) or isinstance(points, bool)):
        return None

    if not isinstance(refined, bool) or (epic is not None and not isinstance(epic, str)):
        return None

    shown_points = NO_VALUE if points is None else str(points)
    shown_refined = REFINED_MARK if refined else NO_VALUE
    return ident, f"#{ident}:{column}:{shown_points}:{shown_refined}:{epic or NO_VALUE}"
