"""The OpenAI wire shapes this door reads and writes.

One request becomes one turn. The door keeps no conversation: `attendance`
owns the transcript (invariant 5), so the history Open WebUI replays is
read for the prompt and the persona, and for nothing else.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import HTTP_BAD_REQUEST, HTTP_PAYLOAD_TOO_LARGE, DoorError
from .untrusted import field_text, is_list, is_object

MODEL_PREFIX = "agent:"
MODEL_OWNER = "agent-control"

# Contract 02 §5.4's cap on `prompt`. Refused here rather than at `attendance`,
# so the reader learns which message was too big.
MAX_PROMPT_BYTES = 256 * 1024

# The family name charset of contract 02 §2.
_FAMILY_PATTERN = re.compile(r"^[a-z][a-z0-9\-]{1,30}$")

_ROLE_USER = "user"
_ROLE_SYSTEM = "system"


@dataclass(frozen=True)
class ChatRequest:
    """One `/v1/chat/completions` body, after validation."""

    family: str
    prompt: str
    persona: str | None
    stream: bool

    @property
    def model(self) -> str:
        return f"{MODEL_PREFIX}{self.family}"


def parse_chat_request(body: object) -> ChatRequest:
    """Read one chat completion request. Raises `DoorError`."""
    if not is_object(body):
        raise DoorError(HTTP_BAD_REQUEST, "the request body is not a JSON object.", code="bad_body")

    family = family_of(field_text(body, "model"))
    messages = body.get("messages")
    if not is_list(messages):
        raise DoorError(HTTP_BAD_REQUEST, "messages must be an array.", code="bad_body")

    prompt = _last_user_text(messages)
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise DoorError(
            HTTP_PAYLOAD_TOO_LARGE,
            f"the message is over {MAX_PROMPT_BYTES} bytes.",
            code="payload_too_large",
        )

    return ChatRequest(
        family=family,
        prompt=prompt,
        persona=_persona(messages),
        stream=body.get("stream") is True,
    )


def family_of(model: str) -> str:
    """`agent:chat` to `chat`. Raises `DoorError` on anything else."""
    if not model.startswith(MODEL_PREFIX):
        raise DoorError(
            HTTP_BAD_REQUEST,
            f"model must be {MODEL_PREFIX}<family>, as /v1/models lists it.",
            code="bad_model",
        )

    family = model[len(MODEL_PREFIX) :]
    if _FAMILY_PATTERN.match(family) is None:
        raise DoorError(HTTP_BAD_REQUEST, f"{model} is not a family.", code="bad_model")

    return family


def is_family_name(name: str) -> bool:
    return _FAMILY_PATTERN.match(name) is not None


def models_body(families: list[str]) -> dict[str, object]:
    return {
        "object": "list",
        "data": [
            {
                "id": f"{MODEL_PREFIX}{family}",
                "object": "model",
                "created": 0,
                "owned_by": MODEL_OWNER,
            }
            for family in families
        ],
    }


def completion_body(
    completion_id: str,
    created: int,
    model: str,
    text: str,
    usage: dict[str, object],
) -> dict[str, object]:
    """The non-streaming answer. `usage` is advisory (contract 03 §13)."""
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": _count(usage, "input"),
            "completion_tokens": _count(usage, "output"),
            "total_tokens": _count(usage, "input") + _count(usage, "output"),
        },
    }


def _count(usage: dict[str, object], field: str) -> int:
    value = usage.get(field)
    # A bool is an int in Python and never a token count.
    if isinstance(value, int) and not isinstance(value, bool):
        return value

    return 0


def _last_user_text(messages: list[object]) -> str:
    """The prompt is the last user message. History is `attendance`'s job."""
    for message in reversed(messages):
        if not is_object(message):
            continue
        if field_text(message, "role") != _ROLE_USER:
            continue

        text = _text_of(message.get("content")).strip()
        if text:
            return text

    raise DoorError(
        HTTP_BAD_REQUEST,
        "this request carries no user message to answer.",
        code="no_prompt",
    )


def _persona(messages: list[object]) -> str | None:
    """Every system message, in order: the folder prompt (contract 02 §11).

    It is persona text and carries no authority. `attendance` puts it in a
    delimited block that says so, and no code path leads from it to a grant.
    """
    parts = [
        _text_of(message.get("content"))
        for message in messages
        if is_object(message) and field_text(message, "role") == _ROLE_SYSTEM
    ]
    joined = "\n".join(part for part in parts if part).strip()

    return joined or None


def _text_of(content: object) -> str:
    if isinstance(content, str):
        return content
    if not is_list(content):
        return ""

    return "\n".join(
        field_text(part, "text")
        for part in content
        if is_object(part) and field_text(part, "type") == "text"
    )
