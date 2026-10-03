"""Packet S6: the noticeboard reads a REAL `sessiond`, as the principal `view-ro`.

`noticeboard/tests` proves the reader against a fake transport. This proves it
against the running service: the socket, the `Authorization` header, the
list and detail bodies, the NDJSON replay, and contract 02 §3.1's "writes:
no" for this principal.

One scenario:

1. A chat answers through the door, so a session and a journal exist.
2. The noticeboard lists the family's sessions with its own token.
3. The noticeboard replays that session's journal and folds it into a
   transcript holding the prompt that was sent and the answer that came
   back.
4. The same token is refused on a write. `view-ro` has no code path to
   one in `noticeboard`, and `sessiond` refuses it as well.

The noticeboard's reader is synchronous, so every call runs in a thread. A sync
client awaited on the loop that also serves `sessiond` would deadlock.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator

from agent_sessiond.auth import Principal
from conftest import chat_body, chat_id, message_id, owui_headers, session_of
from noticeboard.sessiondhttp import HttpTransport, build_client
from noticeboard.sessions import SessionReader
from noticeboard.transcript import Voice, fold
from stack import FAMILY, Stack, until

CHAT_PATH = "/v1/chat/completions"
HTTP_OK = 200
HTTP_FORBIDDEN = 403
TURN_SETTLED = "turn_settled"
SETTLE_TIMEOUT_S = 30.0
PROMPT = "when is the boiler service due"

#: The noticeboard has no URL of its own over a socket. The name is never resolved.
SOCKET_BASE_URL = "http://sessiond"


async def test_the_noticeboard_reads_a_real_session_as_view_ro(stack: Stack) -> None:
    chat = chat_id()
    session = session_of(chat)
    await _answer(stack, chat, PROMPT)
    await _settled(stack, session)

    with _reader(stack) as reader:
        listed = await asyncio.to_thread(reader.sessions, FAMILY)
        detail = await asyncio.to_thread(reader.detail, FAMILY, session)
        stream = await asyncio.to_thread(reader.events, FAMILY, session)

    assert listed.problem == "", listed.problem
    assert session in [one.session for one in listed.rows]
    assert [one.door for one in listed.rows if one.session == session] == ["owui"]

    assert detail.problem == "", detail.problem
    assert detail.session is not None
    assert detail.turns
    assert detail.turns[0].state == "settled"

    assert stream.problems == ()

    entries = fold(stream.lines)
    prompts = [one.text for one in entries if one.voice is Voice.PROMPT]
    answers = [one.text for one in entries if one.voice is Voice.ANSWER]

    assert PROMPT in prompts
    assert answers, "the transcript should hold the assistant's reply"
    assert answers[0]


async def test_the_view_token_cannot_write(stack: Stack) -> None:
    """Contract 02 §3.1: `view-ro` writes nothing. Two fences, not one.

    `noticeboard` has no code that can post a turn, and `sessiond` refuses
    the token anyway. This asserts the second one, because the first is
    proved by the absence of code and nothing else.
    """
    chat = chat_id()
    session = session_of(chat)
    await _answer(stack, chat, PROMPT)
    await _settled(stack, session)

    client = stack.sessiond_as(Principal.VIEW_RO)
    response = await client.post(
        f"/v1/sessions/{FAMILY}/{session}/turns",
        json={"prompt": "write something"},
    )

    assert response.status_code == HTTP_FORBIDDEN, response.text


@contextlib.contextmanager
def _reader(stack: Stack) -> Iterator[SessionReader]:
    """The noticeboard's reader, over the socket `sessiond` is actually serving.

    The client is closed here. The service holds one for its whole life,
    but a test that leaked one would leak a socket per test.
    """
    assert stack.sessiond_socket is not None

    client = build_client(stack.sessiond_socket, SOCKET_BASE_URL)

    try:
        yield SessionReader(
            transport=HttpTransport(client),
            # Contract 02 §3 rule 5's path, written by the stack for every
            # principal. `noticeboard/README.md` names the mode question it raises.
            token_file=stack.state_root / "tokens" / f"{Principal.VIEW_RO.value}.token",
        )
    finally:
        client.close()


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

        async for _ in response.aiter_text():
            continue


async def _settled(stack: Stack, session: str) -> None:
    """The journal lands after the client's `[DONE]`, so wait for the line."""
    await until(
        lambda: TURN_SETTLED in _kinds(stack, session),
        f"{session} to settle",
        SETTLE_TIMEOUT_S,
    )


def _kinds(stack: Stack, session: str) -> list[str]:
    return [str(line.get("kind", "")) for line in stack.journal_lines(session)]
