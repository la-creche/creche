"""The one clock. Every timestamp in this service is minted here.

One source means a test can freeze time in one place, and every RFC 3339
string in the journal, the session file and the fault file has the same shape.
"""

from __future__ import annotations

from datetime import UTC, datetime

_UTC_OFFSET_SUFFIX = "+00:00"
_ZULU = "Z"


def now() -> datetime:
    """Current UTC time."""
    return datetime.now(UTC)


def rfc3339(moment: datetime) -> str:
    """Second precision. Session, turn and fault fields (contract 02 §4.2).

    `isoformat` writes a year of four digits on each system. `strftime`
    writes a year below 1000 with a width that depends on the system, and
    `parse_rfc3339` does not read a year of fewer digits.
    """
    text = moment.astimezone(UTC).isoformat(timespec="seconds")
    return text.replace(_UTC_OFFSET_SUFFIX, _ZULU)


def rfc3339_ms(moment: datetime) -> str:
    """Millisecond precision. Journal line timestamps (contract 02 §8)."""
    text = moment.astimezone(UTC).isoformat(timespec="milliseconds")
    return text.replace(_UTC_OFFSET_SUFFIX, _ZULU)


def parse_rfc3339(text: str) -> datetime | None:
    """Read a timestamp that came from another process.

    The status document and the playpen lock file are written elsewhere, so
    a bad value is expected rather than exceptional. It reads as None
    (contract 05 §3.3.1 rule 6, contract 03 §13).
    """
    if not text:
        return None

    candidate = text[:-1] + _UTC_OFFSET_SUFFIX if text.endswith(_ZULU) else text

    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)

    # The offset can move the time in UTC out of the years 1 to 9999, which
    # are the years that a `datetime` holds.
    try:
        return parsed.astimezone(UTC)
    except OverflowError:
        return None


def age_seconds(moment: datetime, reference: datetime | None = None) -> float:
    """How old a timestamp is. Staleness checks read this (contract 05 §2)."""
    point = reference if reference is not None else now()
    return (point - moment).total_seconds()
