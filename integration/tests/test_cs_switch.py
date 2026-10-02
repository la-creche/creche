"""Packet CS: a sandbox switch, with the real supervisor on both sides.

The unit tests of `sessiond/` prove the switch against a fake supervisor.
This proves it against the built bundle: `stop_process` and `shutdown` are
messages the real supervisor parses, and it exits on the second one. A switch
that only a fake accepted would pass there and hang here.

One scenario, contract 05 §5, `mode: drain` on an idle family:

1. A chat answers on `chat-s1`. A real supervisor holds its pi process.
2. `managerd` publishes `chat-s2` and makes the one call.
3. The old supervisor exits, so the sandbox is free the moment the answer
   lands and `managerd` may run §4.4 on it.
4. The same chat answers again, on a second real supervisor, in `chat-s2`.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from agent_sessiond.auth import Principal
from conftest import chat_body, chat_id, message_id, owui_headers, session_of
from stack import FAMILY, SANDBOX, Stack, until

SWITCH_PATH = "/internal/switch-sandbox"
CHAT_PATH = "/v1/chat/completions"

HTTP_OK = 200

NEXT_SANDBOX = "chat-s2"
REASON = "mounts changed: added the code-sandbox work root rw"
TURN_SETTLED = "turn_settled"
SWITCH_NOTE = "sandbox_switched"

SETTLE_TIMEOUT_S = 30.0
EXIT_TIMEOUT_S = 10.0


async def test_a_drain_moves_a_live_chat_to_a_new_sandbox(stack: Stack) -> None:
    chat = chat_id()
    session = session_of(chat)
    await _answer(stack, chat, "which sensor dropped out")
    await _settled(stack, session, 1)

    retiring = stack.channel_pids()

    assert len(retiring) == 1, "the first turn should hold one supervisor open"

    # `managerd` publishes the replacement, then makes its one call.
    stack.write_status(sandboxes=((SANDBOX, "ready"), (NEXT_SANDBOX, "ready")))
    body = await _switch(stack)

    assert body["switched"] is True
    assert body["outcome"] == "drained"
    assert body["turns_running_at_start"] == 0
    assert body["sessions"] == 1

    # The answer means the outgoing sandbox is free (§5.3 rule 9), so nothing
    # of this service may still be attached to it.
    await until(lambda: _gone(retiring[0]), "the old supervisor to exit", EXIT_TIMEOUT_S)

    # Rule 6: the chat can be told why, and rule 4: it is still the same chat.
    assert _notes(stack, session) == [SWITCH_NOTE]

    await _answer(stack, chat, "and the one before that")
    await _settled(stack, session, 2)

    assert stack.channel_pids() != retiring
    assert _sandboxes(stack, session) == [SANDBOX, NEXT_SANDBOX]


async def _switch(stack: Stack) -> dict[str, Any]:
    """`managerd`'s one call, with the one token that reaches `/internal/*`."""
    client = stack.sessiond_as(Principal.MANAGERD)
    response = await client.post(
        SWITCH_PATH,
        json={
            "family": FAMILY,
            "from": SANDBOX,
            "to": NEXT_SANDBOX,
            "mode": "drain",
            "reason": REASON,
        },
    )

    assert response.status_code == HTTP_OK, response.text

    return dict(response.json())


async def _answer(stack: Stack, chat: str, text: str) -> None:
    """One whole turn through the door, read to the end."""
    client = stack.client

    assert client is not None

    async with client.stream(
        "POST",
        CHAT_PATH,
        headers=owui_headers(chat, message_id()),
        json=chat_body(text, stream=True),
    ) as response:
        assert response.status_code == HTTP_OK, await response.aread()
        await _drain(response)


async def _drain(response: httpx.Response) -> None:
    async for _ in response.aiter_text():
        continue


async def _settled(stack: Stack, session: str, count: int) -> None:
    """The journal lands after the client's `[DONE]`, so wait for the line."""
    await until(
        lambda: _kinds(stack, session).count(TURN_SETTLED) == count,
        f"{session} to reach {count} settled turns",
        SETTLE_TIMEOUT_S,
    )


def _kinds(stack: Stack, session: str) -> list[str]:
    return [str(line.get("kind", "")) for line in stack.journal_lines(session)]


def _notes(stack: Stack, session: str) -> list[str]:
    """Every `note` line's own note name, in order."""
    return [
        str(_body(line).get("note", ""))
        for line in stack.journal_lines(session)
        if line.get("kind") == "note"
    ]


def _sandboxes(stack: Stack, session: str) -> list[str]:
    """The sandbox each turn started on, in order."""
    return [
        str(_body(line).get("sandbox", ""))
        for line in stack.journal_lines(session)
        if line.get("kind") == "turn_started"
    ]


def _body(line: dict[str, Any]) -> dict[str, Any]:
    body = line.get("body")

    return body if isinstance(body, dict) else {}


def _gone(pid: int) -> bool:
    """True once this process is reaped. `signal 0` asks and sends nothing."""
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return True

    return False
