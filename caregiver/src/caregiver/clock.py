"""The one clock every writer in this package stamps a file with.

RFC 3339, UTC, second resolution — the shape contract 05 §2.1 names for
`written_at`, `since`, `checked_at` and every other time field. It lives
below `status.py` so a leaf that needs a timestamp does not have to reach
sideways to a peer for one."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final

RFC3339_FORMAT: Final = "%Y-%m-%dT%H:%M:%SZ"


def now_rfc3339() -> str:
    return rfc3339(datetime.now(UTC))


def rfc3339(moment: datetime) -> str:
    """One format, spelled once. Fixed width and UTC, so two stamps compare
    as plain strings and no reader has to parse one to order them."""
    return moment.strftime(RFC3339_FORMAT)


def seconds_from_now(seconds: int) -> str:
    """A deadline this many seconds ahead, in the same shape. Used for
    LiteLLM's worker cache window (contract 05 §7 rule 6)."""
    return rfc3339(datetime.now(UTC) + timedelta(seconds=seconds))
