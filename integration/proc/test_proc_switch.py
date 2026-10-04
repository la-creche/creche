"""The one call of `caregiver` into `attendance`, at the HTTP boundary.

`integration/tests/test_i2_stage2.py` drives a replacement through
`caregiver`'s own code, inside the test process. No `caregiver` process runs
in this suite yet, so a test plays `caregiver` here and does what contract
05 §4.3 and §5 give it to do:

1. Write the env file of the incoming sandbox.
2. Publish the incoming sandbox in the status document, as `creating`.
3. Call `POST /internal/switch-sandbox` with the `caregiver` token.
4. Publish the status document with the outgoing sandbox gone.

So these scenarios judge the `attendance` side of a replacement. They do not
prove that `caregiver` makes the call.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from proc_chat import (
    TURN_SETTLED,
    TURN_STARTED,
    await_settled,
    chat_id,
    run_stream,
    session_of,
    until,
)
from proc_owui import OwuiStack
from proc_standins import set_pi_env
from proc_tree import FAMILY, SANDBOX, Tree, write_playpen_env, write_status

SWITCH_PATH = "/internal/switch-sandbox"
CAREGIVER = "managerd"
NEXT_SANDBOX = "chat-s2"

DRAIN = "drain"
INTERRUPT = "interrupt"
ADDITION = "sandbox.cpus changed: 2 to 4"
REMOVAL = "a mount was removed"

TURN_ABORTED = "turn_aborted"
NOTE = "note"
SWITCH_NOTE = "sandbox_switched"

#: A turn long enough to switch inside: 60 deltas, 30 ms apart.
LONG_TURN = {"events": 60, "delay_ms": 30}
SHORT_TURN = {"events": 3, "delay_ms": 1}
STARTED_DEADLINE_S = 30.0

HTTP_OK = 200
HTTP_BAD_REQUEST = 400
HTTP_FORBIDDEN = 403
HTTP_UNAVAILABLE = 503


async def test_the_caregiver_token_clears_the_internal_check(owui: OwuiStack) -> None:
    """Contract 02 §3.1 and contract 05 §5.3 rule 8.

    The token authenticates and may make an internal call. Only the pair is
    refused, because `from` and `to` name one sandbox.
    """
    response = await _switch(owui, DRAIN, to=SANDBOX)

    assert response.status_code == HTTP_BAD_REQUEST
    assert response.json()["error"]["code"] == "bad_request"


async def test_no_door_token_reaches_the_internal_call(owui: OwuiStack) -> None:
    """Contract 02 §3.1: no door token reaches `/internal/*`."""
    _publish_incoming(owui.tree)

    response = await _switch(owui, DRAIN, principal="door-owui")

    assert response.status_code == HTTP_FORBIDDEN
    assert response.json()["error"]["code"] == "forbidden"
    assert len(owui.sbx_calls()) == 0, "a refused call dialled a sandbox"


async def test_a_drain_switch_lets_the_running_turn_finish(
    owui: OwuiStack, door: httpx.AsyncClient
) -> None:
    """Contract 05 §5.3 rule 2, on two real playpens.

    The running turn finishes on the outgoing sandbox. The next turn runs on
    the incoming one. The session and its journal stay.
    """
    # The pi process of a session starts once and serves every turn of it, so
    # the tuning is set before the first turn of the session.
    set_pi_env(owui.tree, **LONG_TURN)
    chat = chat_id()
    session = session_of(chat)
    first = await run_stream(door, chat, "one")
    assert first.error_chunks == []
    await await_settled(owui.tree, session)

    running = asyncio.create_task(run_stream(door, chat, "two"))
    await _started(owui.tree, session, 2)

    _publish_incoming(owui.tree)
    response = await _switch(owui, DRAIN)

    assert response.status_code == HTTP_OK, response.text
    answer = response.json()
    assert answer["switched"] is True
    assert answer["outcome"] == "drained"
    assert answer["turns_running_at_start"] == 1
    assert answer["turns_finished"] == 1
    assert answer["turns_aborted"] == 0
    assert answer["sessions"] == 1

    # The drain waited for the turn, and the turn finished.
    drained = await running
    assert drained.ends_with_done
    assert drained.error_chunks == []

    _publish_replaced(owui.tree)
    set_pi_env(owui.tree, **SHORT_TURN)
    third = await run_stream(door, chat, "three")
    assert third.error_chunks == []
    await await_settled(owui.tree, session, 3)

    assert _sandboxes_of(owui.tree, session) == [SANDBOX, SANDBOX, NEXT_SANDBOX]
    assert owui.tree.journal_kinds(session).count(TURN_SETTLED) == 3
    # Rule 6: one note in the journal of the session, with why the turn moved.
    assert _switch_notes(owui.tree, session) == [
        {
            "note": SWITCH_NOTE,
            "from": SANDBOX,
            "to": NEXT_SANDBOX,
            "mode": DRAIN,
            "reason": ADDITION,
        }
    ]


async def test_an_interrupt_switch_aborts_the_running_turn(
    owui: OwuiStack, door: httpx.AsyncClient
) -> None:
    """Contract 05 §5.3 rule 3. A removal applies at once (invariant 9).

    A turn that still holds the removed reach does not finish. The client is
    told why, and the next turn runs on the incoming sandbox.
    """
    set_pi_env(owui.tree, **LONG_TURN)
    chat = chat_id()
    session = session_of(chat)
    running = asyncio.create_task(run_stream(door, chat, "one"))
    await _started(owui.tree, session, 1)

    _publish_incoming(owui.tree)
    response = await _switch(owui, INTERRUPT, reason=REMOVAL)

    assert response.status_code == HTTP_OK, response.text
    answer = response.json()
    assert answer["switched"] is True
    assert answer["outcome"] == "interrupted"
    assert answer["turns_aborted"] == 1

    interrupted = await running
    assert interrupted.error_codes == ["permission_removed"]
    await until(
        lambda: TURN_ABORTED in owui.tree.journal_kinds(session), f"{session} to be aborted"
    )
    assert TURN_SETTLED not in owui.tree.journal_kinds(session)

    _publish_replaced(owui.tree)
    set_pi_env(owui.tree, **SHORT_TURN)
    again = await run_stream(door, chat, "two")
    assert again.error_chunks == []
    await await_settled(owui.tree, session)

    assert _sandboxes_of(owui.tree, session) == [SANDBOX, NEXT_SANDBOX]


async def test_a_switch_onto_a_sandbox_that_cannot_start_is_refused(
    owui: OwuiStack, door: httpx.AsyncClient
) -> None:
    """Contract 05 §5.3 rule 8: the handshake runs before anything moves.

    The env file of the incoming sandbox names none of its mounts, so the
    real playpen answers `fatal` and not `ready` (contract 03 §5.7). The call
    is refused, and the family keeps serving on the sandbox it served on.
    """
    chat = chat_id()
    session = session_of(chat)
    first = await run_stream(door, chat, "one")
    assert first.error_chunks == []
    await await_settled(owui.tree, session)

    _publish_incoming(owui.tree)
    owui.tree.playpen_env(FAMILY, NEXT_SANDBOX).write_text(
        f"AGENT_SANDBOX={NEXT_SANDBOX}\n", encoding="utf-8"
    )
    response = await _switch(owui, DRAIN)

    assert response.status_code == HTTP_UNAVAILABLE
    assert response.json()["error"]["code"] == "sandbox_unavailable"

    second = await run_stream(door, chat, "two")
    assert second.error_chunks == []
    await await_settled(owui.tree, session, 2)
    assert _sandboxes_of(owui.tree, session) == [SANDBOX, SANDBOX]
    assert _switch_notes(owui.tree, session) == []


async def test_a_repeated_switch_does_nothing_twice(owui: OwuiStack) -> None:
    """Contract 05 §5.3 rule 7. `caregiver` may call again after a timeout."""
    _publish_incoming(owui.tree)

    first = await _switch(owui, DRAIN)
    second = await _switch(owui, DRAIN)

    assert first.status_code == HTTP_OK, first.text
    assert second.status_code == HTTP_OK, second.text
    assert second.json() == first.json()
    assert [call.value_after("--sandbox") for call in owui.sbx_calls()] == [NEXT_SANDBOX]


def _publish_incoming(tree: Tree) -> None:
    """Contract 05 §4.3: the env file, then the incoming sandbox in the document."""
    write_playpen_env(tree, FAMILY, NEXT_SANDBOX)
    write_status(tree, sandboxes=((SANDBOX, "ready"), (NEXT_SANDBOX, "creating")))


def _publish_replaced(tree: Tree) -> None:
    """Contract 05 §4.4: the outgoing sandbox is gone, the incoming one serves."""
    write_status(tree, sandboxes=((NEXT_SANDBOX, "ready"),))


async def _switch(
    owui: OwuiStack,
    mode: str,
    *,
    to: str = NEXT_SANDBOX,
    reason: str = ADDITION,
    principal: str = CAREGIVER,
) -> httpx.Response:
    """One switch call (contract 05 §5.1), with the token of one principal."""
    body = {"family": FAMILY, "from": SANDBOX, "to": to, "mode": mode, "reason": reason}

    async with owui.attendance_client(principal) as client:
        return await client.post(SWITCH_PATH, json=body)


async def _started(tree: Tree, session: str, count: int) -> None:
    """Wait until the journal shows that a turn reached the sandbox."""
    await until(
        lambda: tree.journal_kinds(session).count(TURN_STARTED) >= count,
        f"{session} to start {count} turns",
        STARTED_DEADLINE_S,
    )


def _sandboxes_of(tree: Tree, session: str) -> list[str]:
    """The sandbox each turn started on, in order (contract 02 §8.1)."""
    return [
        str(_body(line).get("sandbox", ""))
        for line in tree.journal_lines(session)
        if line.get("kind") == TURN_STARTED
    ]


def _switch_notes(tree: Tree, session: str) -> list[dict[str, Any]]:
    """The body of every switch note in the journal of one session."""
    bodies = [_body(line) for line in tree.journal_lines(session) if line.get("kind") == NOTE]

    return [body for body in bodies if body.get("note") == SWITCH_NOTE]


def _body(line: dict[str, Any]) -> dict[str, Any]:
    body = line.get("body")

    return body if isinstance(body, dict) else {}  # pyright: ignore[reportUnknownVariableType]
