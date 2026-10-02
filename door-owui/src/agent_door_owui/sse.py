"""The SSE frames this door writes, byte for byte.

`tests/test_translate.py`'s golden vectors fix these bytes.

A run reports on three channels and only the first is the answer:

    text       `delta.content`            the reply
    reasoning  `delta.reasoning_content`  a live thinking block
    tools      `response.output_item.*`   the native tool chip

The tool channel is deliberately NOT markdown and NOT `<details>` HTML in
`content`. A tool result is untrusted data (invariant 14). Travelling as a
JSON string means a hostile result cannot forge a closing tag to escape its
own block, and the chips never enter `content`, so the next turn's history
carries the answer alone.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any

from .untrusted import field_text, is_list, is_object

# Open WebUI re-broadcasts the whole accumulated message over socket.io on
# every content delta, so a chip's text is not sent once: it is re-sent with
# every token that follows it and compounds per chip. A 16 KiB result froze
# the chat once. This is a display budget, not a free knob.
CHIP_TEXT_LIMIT = 2048

DONE_FRAME = "data: [DONE]\n\n"

# An SSE comment: bytes on the wire that carry no event. A tool call is
# otherwise completely silent for as long as it runs, which lets every proxy
# on the path call the stream idle. Open WebUI ignores it safely, because a
# line that does not start with `data:` is fed to a JSON parse inside a
# try/except that swallows the failure.
KEEPALIVE_FRAME = ": keepalive\n\n"

_CHUNK_OBJECT = "chat.completion.chunk"
_OUTPUT_ITEM_TYPE = "response.output_item.added"
_TRUNCATION_NOTE = "\n[truncated for display; the model received all of it]"
_TOOL_FAILED_PREFIX = "⚠️ the tool failed\n\n"


class ToolOutcome(StrEnum):
    """Whether a tool call's result is a result or a failure."""

    OK = "completed"
    FAILED = "failed"


def dumps(value: object) -> str:
    """JSON the way `JSON.stringify` writes it: compact, and not ASCII-escaped.

    `ensure_ascii=False` is load-bearing. A golden vector carries a raw
    U+2028 inside a JSON string, and an escaped copy would not match it.
    """
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def tool_result_text(result: object, outcome: ToolOutcome = ToolOutcome.OK) -> str:
    """A tool result as the chat surface should show it.

    A failure is called out in the body because it cannot be carried in the
    header: the surface decides a chip's state from the CALL item's status,
    which was already sent as `in_progress` before the outcome was known, and
    an append-only stream cannot go back and change it.
    """
    raw = _extract_tool_text(result)
    text = f"{_TOOL_FAILED_PREFIX}{raw}" if outcome is ToolOutcome.FAILED else raw
    if len(text) <= CHIP_TEXT_LIMIT:
        return text

    return f"{text[:CHIP_TEXT_LIMIT]}{_TRUNCATION_NOTE}"


def _extract_tool_text(result: object) -> str:
    if isinstance(result, str):
        return result
    if not is_object(result):
        return dumps(result) if result is not None else ""

    parts = result.get("content")
    if is_list(parts):
        joined = "".join(field_text(part, "text") for part in parts if is_object(part))
        if joined:
            return joined

    # An empty string is a result, not a miss, so it is returned as-is.
    text = result.get("text")
    if isinstance(text, str):
        return text

    return dumps(result)


class SseWriter:
    """Builds the frames of one response. One writer per request."""

    def __init__(self, stream_id: str, created: int, model: str) -> None:
        self._id = stream_id
        self._created = created
        self._model = model
        # Every output item needs its own `id`: Open WebUI keys its incremental
        # tag-scan cache on it and the guard is `if (!item_id) return 0`, so an
        # item without one makes it rescan the whole text on every delta. The
        # counter is only a fallback for a runtime that hands us no call id, so
        # two such calls cannot share an id.
        self._item_seq = 0

    def chunk(self, delta: dict[str, str], finish: str | None = None) -> str:
        return (
            "data: "
            + dumps(
                {
                    "id": self._id,
                    "object": _CHUNK_OBJECT,
                    "created": self._created,
                    "model": self._model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                }
            )
            + "\n\n"
        )

    def role(self) -> str:
        return self.chunk({"role": "assistant"})

    def text(self, delta: str) -> str:
        return self.chunk({"content": delta})

    def reasoning(self, delta: str) -> str:
        return self.chunk({"reasoning_content": delta})

    def stop(self) -> str:
        return self.chunk({}, "stop")

    def tool_start(self, call_id: str, name: str, args: object) -> str:
        return self._output_item(
            {
                "type": "function_call",
                "id": self._item_id("fc", call_id),
                "call_id": call_id,
                "name": name,
                "arguments": dumps(args if args is not None else {}),
                "status": "in_progress",
            }
        )

    def tool_end(self, call_id: str, text: str, outcome: ToolOutcome) -> str:
        return self._output_item(
            {
                "type": "function_call_output",
                "id": self._item_id("fco", call_id),
                "call_id": call_id,
                "status": str(outcome),
                "output": [{"type": "output_text", "text": text}],
            }
        )

    def error(self, body: dict[str, dict[str, str]]) -> str:
        """An OpenAI-shaped error, mid-stream. A failure is never silent."""
        return "data: " + dumps(body) + "\n\n"

    def settled(self, status: str) -> str:
        """The terminal marker, one chunk with no choices, sent just before `[DONE]`."""
        return "data: " + dumps({"choices": [], "type": "agent_settled", "status": status}) + "\n\n"

    def _output_item(self, item: dict[str, Any]) -> str:
        # Responses-API output items ride the same SSE stream as the chunks.
        # `response.output_item.added` appends whatever item it carries, so a
        # call needs no index and no second event to close it: the result item
        # arriving under the same call_id is what marks the chip done. The
        # empty `choices` keeps a strict OpenAI client from choking.
        return (
            "data: "
            + dumps(
                {
                    "id": self._id,
                    "object": _CHUNK_OBJECT,
                    "created": self._created,
                    "model": self._model,
                    "choices": [],
                    "type": _OUTPUT_ITEM_TYPE,
                    "item": item,
                }
            )
            + "\n\n"
        )

    def _item_id(self, prefix: str, call_id: str) -> str:
        if call_id:
            return f"{prefix}_{call_id}"

        self._item_seq += 1
        return f"{prefix}_anon{self._item_seq}"
