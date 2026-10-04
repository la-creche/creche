"""What Open WebUI sends, and how a scenario waits for what comes back.

Every helper here speaks HTTP or reads a file under the root. None holds an
object of a service.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Final

import httpx
import proc_sse
from proc_tree import MODEL, Tree

CHAT_PATH: Final = "/v1/chat/completions"
MODELS_PATH: Final = "/v1/models"
SESSIONS_PATH: Final = "/v1/sessions"

# The journal line kinds of contract 02 §8.1 that a scenario reads back.
SESSION_CREATED: Final = "session_created"
TURN_STARTED: Final = "turn_started"
TURN_SETTLED: Final = "turn_settled"
TURN_FAILED: Final = "turn_failed"

SETTLE_DEADLINE_S: Final = 30.0
_DEFAULT_DEADLINE_S: Final = 10.0
_POLL_S: Final = 0.01


def chat_id() -> str:
    """One Open WebUI chat id. A UUID, as Open WebUI mints them."""
    return str(uuid.uuid4())


def message_id() -> str:
    return str(uuid.uuid4())


def session_of(chat: str) -> str:
    """Contract 02 §2: an Open WebUI chat becomes `owui-<chat id>`."""
    return f"owui-{chat}"


def owui_headers(chat: str, message: str) -> dict[str, str]:
    """The custom headers the door reads on each request."""
    return {"x-owui-chat-id": chat, "x-owui-message-id": message}


def chat_body(text: str, *, stream: bool, system: str | None = None) -> dict[str, object]:
    """One OpenAI chat completion request, as Open WebUI sends it."""
    messages: list[dict[str, object]] = []

    if system is not None:
        messages.append({"role": "system", "content": system})

    messages.append({"role": "user", "content": text})

    return {"model": MODEL, "messages": messages, "stream": stream}


async def run_stream(
    door: httpx.AsyncClient,
    chat: str,
    text: str,
    *,
    message: str | None = None,
    system: str | None = None,
) -> proc_sse.Frames:
    """One streamed turn through the door, read to its end."""
    headers = owui_headers(chat, message if message is not None else message_id())
    body = chat_body(text, stream=True, system=system)

    async with door.stream("POST", CHAT_PATH, headers=headers, json=body) as response:
        assert response.status_code == httpx.codes.OK, await response.aread()
        raw = await read_rest(response.aiter_text())

    return proc_sse.parse(raw)


async def first_chunk(chunks: AsyncIterator[str]) -> str:
    """Pull one frame, which proves the turn runs on the host.

    One response is read through ONE iterator. httpx refuses a second one on
    a stream it already read, so a caller that acts during a turn keeps this
    iterator and hands the same one to `read_rest`.
    """
    async for chunk in chunks:
        if chunk.strip():
            return chunk

    raise AssertionError("the stream closed before its first frame")


async def read_rest(chunks: AsyncIterator[str]) -> str:
    return "".join([chunk async for chunk in chunks])


async def until(
    check: Callable[[], bool], what: str, deadline_s: float = _DEFAULT_DEADLINE_S
) -> None:
    """Poll until a condition holds, or say what never happened."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + deadline_s

    while not check():
        if loop.time() > deadline:
            raise AssertionError(f"timed out after {deadline_s} s waiting for {what}")

        await asyncio.sleep(_POLL_S)


async def await_settled(tree: Tree, session: str, count: int = 1) -> None:
    """Wait for the journal, which lands AFTER the client's `[DONE]`.

    The door ends its stream at pi's own `agent_settled` event. `attendance`
    writes the `turn_settled` line when the playpen's `turn_settled` message
    arrives, and that is later (contract 02 §8.2). A test that reads the
    journal when a stream ends reads too early.
    """
    await until(
        lambda: tree.journal_kinds(session).count(TURN_SETTLED) >= count,
        f"{session} to reach {count} {TURN_SETTLED} lines",
        SETTLE_DEADLINE_S,
    )
