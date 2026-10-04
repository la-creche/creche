"""One line of `attendance`'s event stream, validated before use.

Contract 02 §8 fixes the shape. The stream crosses a process boundary, so
every line is untrusted input: shape and size are checked before anything
reads a field (invariants 12 and 14).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum

from .untrusted import as_object, as_text, field_text, is_object

# Contract 03 §2 caps a channel line at 1 MiB and contract 03 §8 caps a
# wrapped pi event at 256 KiB, so a journal line that wraps one has no
# business being larger. A line over the cap is refused, never truncated:
# half a JSON object is not a smaller JSON object.
MAX_LINE_BYTES = 1048576


class LineKind(StrEnum):
    """Contract 02 §8.1's kinds, limited to the ones this door reads."""

    TURN_QUEUED = "turn_queued"
    TURN_STARTED = "turn_started"
    PI_EVENT = "pi_event"
    TURN_SETTLED = "turn_settled"
    TURN_FAILED = "turn_failed"
    TURN_ABORTED = "turn_aborted"
    HEARTBEAT = "heartbeat"


class BadLine(Exception):
    """A line the door refuses. It names what failed, never the payload."""


@dataclass(frozen=True)
class JournalLine:
    """A parsed, shape-checked line of the event stream."""

    kind: str
    turn: str | None
    body: dict[str, object]

    def text(self, field: str) -> str:
        """A string field of the body, or the empty string. Never raises."""
        return field_text(self.body, field)


def parse_line(raw: bytes) -> JournalLine:
    """Validate one NDJSON line and return it. Raises `BadLine`."""
    if len(raw) > MAX_LINE_BYTES:
        raise BadLine(f"line over {MAX_LINE_BYTES} bytes")

    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BadLine("line is not valid UTF-8") from exc

    try:
        parsed: object = json.loads(decoded)
    except (ValueError, RecursionError) as exc:
        # RecursionError: a line that nests too deep is not a ValueError.
        raise BadLine("line is not valid JSON") from exc

    if not is_object(parsed):
        raise BadLine("line is not a JSON object")

    kind = field_text(parsed, "kind")
    if not kind:
        raise BadLine("line carries no kind")

    turn = as_text(parsed.get("turn"))

    return JournalLine(
        kind=kind,
        turn=turn or None,
        body=as_object(parsed.get("body")),
    )
