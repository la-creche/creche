"""Read the door's SSE body back, the way Open WebUI reads it.

One event per frame, a blank line after each event, and `[DONE]` last. The
door's own writer is under test, so nothing here uses it: this module parses
the bytes, by the rules that an SSE client follows:

1. A blank line ends an event. What follows the last blank line is an event
   that no client gets.
2. The `data` lines of one event are one value, joined by newlines.
3. A line that starts with a colon is a comment.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final, cast

_DATA_FIELD: Final = "data:"
_DONE: Final = "[DONE]"
_LINE_END: Final = "\n"
_EVENT_END: Final = "\n\n"


@dataclass(frozen=True, slots=True)
class Frames:
    """One streamed answer, as the parts a test asserts on."""

    raw: str
    chunks: tuple[dict[str, Any], ...]
    ends_with_done: bool

    @property
    def text(self) -> str:
        """The assistant content, in arrival order."""
        return "".join(_delta_text(choice) for choice in self._choices())

    @property
    def finish_reasons(self) -> list[str]:
        reasons = [choice.get("finish_reason") for choice in self._choices()]

        return [reason for reason in reasons if isinstance(reason, str)]

    @property
    def error_chunks(self) -> list[dict[str, Any]]:
        return [chunk for chunk in self.chunks if "error" in chunk]

    @property
    def error_codes(self) -> list[str]:
        """The `error.code` of every error chunk."""
        return [str(_object(chunk.get("error")).get("code", "")) for chunk in self.error_chunks]

    def ids(self) -> set[str]:
        return {str(chunk["id"]) for chunk in self.chunks if "id" in chunk}

    def _choices(self) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []

        for chunk in self.chunks:
            choices = chunk.get("choices")

            if isinstance(choices, list):
                found.extend(_object(choice) for choice in cast("list[object]", choices))

        return found


def parse(raw: str) -> Frames:
    """Split an SSE body into its JSON chunks. A comment line is dropped.

    `ends_with_done` is true only when `[DONE]` is the last event. An event
    that is not JSON raises, as it stops a client.
    """
    values = _data_values(raw)
    chunks = tuple(_object(json.loads(value)) for value in values if value != _DONE)

    return Frames(raw=raw, chunks=chunks, ends_with_done=values[-1:] == [_DONE])


def _data_values(raw: str) -> list[str]:
    """The data of each event that a blank line ended, in arrival order."""
    text = raw.replace("\r\n", _LINE_END).replace("\r", _LINE_END)
    values: list[str] = []

    for event in text.split(_EVENT_END)[:-1]:
        lines = [line for line in event.split(_LINE_END) if line.startswith(_DATA_FIELD)]

        if lines:
            values.append(_LINE_END.join(_field_value(line) for line in lines))

    return values


def _field_value(line: str) -> str:
    """What follows `data:`, without the one space a writer may put there."""
    return line[len(_DATA_FIELD) :].removeprefix(" ")


def _delta_text(choice: dict[str, Any]) -> str:
    content = _object(choice.get("delta")).get("content")

    return content if isinstance(content, str) else ""


def _object(value: object) -> dict[str, Any]:
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}
