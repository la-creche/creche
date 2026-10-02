"""Reading the door's SSE back, the way Open WebUI reads it.

One `data:` line per frame, `[DONE]` last. The door's own writer is the thing
under test, so nothing here reuses it: this module parses the bytes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

DATA_PREFIX = "data: "
DONE = "[DONE]"
KEEPALIVE_PREFIX = ":"


@dataclass(frozen=True)
class Frames:
    """One streamed answer, split into the parts a test asserts on."""

    raw: str
    chunks: list[dict[str, Any]]
    ends_with_done: bool

    @property
    def text(self) -> str:
        """The assistant content, concatenated in arrival order."""
        return "".join(_delta_text(chunk) for chunk in self.chunks)

    @property
    def finish_reasons(self) -> list[str]:
        out: list[str] = []
        for chunk in self.chunks:
            for choice in _choices(chunk):
                reason = choice.get("finish_reason")
                if isinstance(reason, str):
                    out.append(reason)

        return out

    @property
    def error_chunks(self) -> list[dict[str, Any]]:
        return [chunk for chunk in self.chunks if "error" in chunk]

    def ids(self) -> set[str]:
        return {str(chunk.get("id")) for chunk in self.chunks if "id" in chunk}


def parse(raw: str) -> Frames:
    """Split an SSE body into its JSON chunks. Keepalive comments are dropped."""
    chunks: list[dict[str, Any]] = []
    done = False

    for line in raw.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith(KEEPALIVE_PREFIX):
            continue
        if not stripped.startswith(DATA_PREFIX):
            continue

        payload = stripped[len(DATA_PREFIX) :]
        if payload == DONE:
            done = True
            continue

        parsed: Any = json.loads(payload)
        if isinstance(parsed, dict):
            chunks.append(parsed)  # pyright: ignore[reportUnknownArgumentType]

    return Frames(raw=raw, chunks=chunks, ends_with_done=done)


def _choices(chunk: dict[str, Any]) -> list[dict[str, Any]]:
    choices = chunk.get("choices")
    if not isinstance(choices, list):
        return []

    return [choice for choice in choices if isinstance(choice, dict)]  # pyright: ignore[reportUnknownVariableType]


def _delta_text(chunk: dict[str, Any]) -> str:
    out = ""
    for choice in _choices(chunk):
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            continue

        content = delta.get("content")  # pyright: ignore[reportUnknownMemberType]
        if isinstance(content, str):
            out += content

    return out
