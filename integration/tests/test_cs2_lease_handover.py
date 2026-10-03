"""Packet CS2: an Open WebUI chat continues in a terminal, with no wait.

Invariant 3 says an Open WebUI chat and a TUI session within one family are
the same session, and the operator's stage 4 test is "start a coding session
on the phone, continue it in the terminal". Draft 5 of contract 02 refused
that terminal for the 60 seconds after every chat turn, because the idle
`owui` lease was still inside its TTL.

Draft 6 §7.3 rule 5 hands an idle lease to another door, §5.10 lets a door
give one back the moment it is done, and §5.11 closes the session's pi
process instead of waiting out `pi_idle_ttl_s`. This scenario proves all
three against a real `sessiond` and the real TUI door client, off the host.

```
  Open WebUI door --turn------------------> lease: owui, idle
  TUI door        --writer, acquire-------> lease: tui   (no --force, no wait)
  TUI door        --DELETE writer---------> no lease
  another terminal --writer---------------> lease: tui.5002
  TUI door        --writer, renew---------> lease_taken_over
```

The TUI door's client is synchronous, so every call runs in a worker thread
and the stack's own event loop stays free for `sessiond`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest
from agent_door_tui.config import TuiConfig
from agent_door_tui.sessiond import (
    CODE_LEASE_TAKEN_OVER,
    HttpSessiond,
    Intent,
    SessiondError,
    SessionRow,
    Takeover,
)
from conftest import chat_body, chat_id, message_id, owui_headers, session_of
from stack import FAMILY, Stack

CHAT_PATH = "/v1/chat/completions"
HTTP_OK = 200
FIRST_TERMINAL = "tui.5001"
SECOND_TERMINAL = "tui.5002"
CODE_SESSION_BUSY = "session_busy"


def tui_client(stack: Stack) -> HttpSessiond:
    """The TUI door's own client, pointed at the stack's `sessiond`."""
    socket = stack.sessiond_socket
    assert socket is not None, "the stack fixture always serves before it yields"
    token = (stack.state_root / "tokens" / "door-tui.token").read_text(encoding="utf-8")

    return HttpSessiond(
        TuiConfig(
            sessiond_token=token.strip(),
            sessiond_url="http://sessiond",
            sessiond_socket=socket,
            families_dir=stack.families_dir,
            sbx="/nonexistent/sbx",
            pi_launch="/nonexistent/agent-pi-launch.js",
            door_instance=FIRST_TERMINAL,
        )
    )


async def one_owui_turn(stack: Stack, chat: str) -> None:
    """One non-streamed turn through the Open WebUI door, as the phone does."""
    client = stack.client
    assert client is not None, "the stack fixture always serves before it yields"

    answer = await client.post(
        CHAT_PATH,
        headers=owui_headers(chat, message_id()),
        json=chat_body("hello", stream=False),
    )

    assert answer.status_code == HTTP_OK, answer.text


async def call[Answer](work: Callable[[], Answer]) -> Answer:
    """Run one synchronous door call without blocking the stack's loop."""
    return await asyncio.to_thread(work)


async def test_cs2_a_terminal_takes_an_idle_chat_lease(stack: Stack) -> None:
    """Contract 02 §7.3 rules 5 and 6, §5.10 and §7.4, end to end."""
    chat = chat_id()
    session = session_of(chat)
    await one_owui_turn(stack, chat)
    door = tui_client(stack)

    try:
        # The chat's lease is idle and well inside its 60-second TTL. No
        # `--force` and no wait: rule 5 hands it over, because no turn runs.
        await call(lambda: door.take_writer(FAMILY, session, FIRST_TERMINAL))

        # A second terminal still meets the refusal §7 exists for (rule 6).
        with pytest.raises(SessiondError) as busy:
            await call(lambda: door.take_writer(FAMILY, session, SECOND_TERMINAL))

        assert busy.value.code == CODE_SESSION_BUSY

        # §5.10: a terminal gives the session back when it closes, instead
        # of leaving the next door to wait out a timer this one caused.
        await call(lambda: door.release_writer(FAMILY, session, FIRST_TERMINAL))
        await call(lambda: door.take_writer(FAMILY, session, SECOND_TERMINAL))

        # §7.4: the first terminal's renewal timer learns it lost the
        # session, and may never take it back.
        with pytest.raises(SessiondError) as renewed:
            await call(
                lambda: door.take_writer(
                    FAMILY, session, FIRST_TERMINAL, Takeover.POLITE, Intent.RENEW
                )
            )
    finally:
        door.close()

    assert renewed.value.code == CODE_LEASE_TAKEN_OVER
    assert renewed.value.detail["holder"] == "tui"


async def test_cs2_a_terminal_releases_the_pi_process(stack: Stack) -> None:
    """Contract 02 §5.11. A terminal waits out no `pi_idle_ttl_s`."""
    chat = chat_id()
    session = session_of(chat)
    await one_owui_turn(stack, chat)
    door = tui_client(stack)

    try:
        await call(lambda: door.take_writer(FAMILY, session, FIRST_TERMINAL))
        # The turn settled, so the playpen still holds this session's pi
        # process. Asking for it back is one call, and never a session
        # delete: §5.11 rule 3 closes a process, invariant 1 keeps sessions.
        await call(lambda: door.release_process(FAMILY, session, FIRST_TERMINAL))
        row: SessionRow = await call(lambda: door.get_session(FAMILY, session))
    finally:
        door.close()

    assert row.session == session
    assert row.turns_total == 1
