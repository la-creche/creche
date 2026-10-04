"""Read the door's SSE body back, the way Open WebUI reads it.

One `data:` line per frame, and `[DONE]` last. The door's own writer is under
test, so nothing here uses it: this module parses the bytes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final, cast

_DATA_PREFIX: Final = "data: "
_DONE: Final = "[DONE]"


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
    """Split an SSE body into its JSON chunks. A comment line is dropped."""
    chunks: list[dict[str, Any]] = []
    done = False

    for line in raw.split("\n"):
        stripped = line.strip()

        if not stripped.startswith(_DATA_PREFIX):
            continue

        payload = stripped[len(_DATA_PREFIX) :]

        if payload == _DONE:
            done = True
            continue

        chunks.append(_object(json.loads(payload)))

    return Frames(raw=raw, chunks=tuple(chunks), ends_with_done=done)


def _delta_text(choice: dict[str, Any]) -> str:
    content = _object(choice.get("delta")).get("content")

    return content if isinstance(content, str) else ""


def _object(value: object) -> dict[str, Any]:
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}
