"""The one call of `caregiver` into `attendance`, at the HTTP boundary.

The scenarios that ran a switch to its end left in phase 2 row 20: the
handshake is ACP `initialize` on the incoming facade, and this topology has
no facade. What stays is the token check and the refusal of a sandbox that
cannot start, which come before the handshake succeeds.
"""

from __future__ import annotations

from typing import Any

import httpx
from proc_chat import (
    TURN_STARTED,
    await_settled,
    chat_id,
    run_stream,
    session_of,
)
from proc_owui import OwuiStack
from proc_tree import FAMILY, SANDBOX, Tree, write_playpen_env, write_status

SWITCH_PATH = "/internal/switch-sandbox"
CAREGIVER = "managerd"
NEXT_SANDBOX = "chat-s2"

DRAIN = "drain"
ADDITION = "sandbox.cpus changed: 2 to 4"
NOTE = "note"
SWITCH_NOTE = "sandbox_switched"

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


def _publish_incoming(tree: Tree) -> None:
    """Contract 05 §4.3: the env file, then the incoming sandbox in the document."""
    write_playpen_env(tree, FAMILY, NEXT_SANDBOX)
    write_status(tree, sandboxes=((SANDBOX, "ready"), (NEXT_SANDBOX, "creating")))


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
