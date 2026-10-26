"""The thirteen stage 1 scenarios, with each service in its own process.

`integration/tests/test_stage1_gate.py` holds the same thirteen with both
services inside the test process. Here the door and `attendance` start from
their real entry points, and every assertion reads something that crossed a
process boundary: an HTTP status, an HTTP body, the SSE stream, a journal
file, a file in a mount, or what a stand-in program recorded.

The numbers are the numbers of the old suite, so one scenario has one name in
both.
"""

from __future__ import annotations

import httpx
from proc_chat import (
    CHAT_PATH,
    MODELS_PATH,
    chat_body,
    message_id,
    owui_headers,
)
from proc_owui import OwuiStack
from proc_tree import MODEL

# The playpen fences a persona before it reaches pi. The tag is the stable
# part of that fence, and it is inside the 24 characters the fake pi echoes.
PERSONA_TAG = "<folder-prompt"

#: A turn long enough to act inside: 60 deltas, 30 ms apart.
LONG_TURN = {"events": 60, "delay_ms": 30}
SHORT_TURN = {"events": 3, "delay_ms": 1}


async def test_models_lists_the_chat_family(door: httpx.AsyncClient) -> None:
    """Scenario 1. The picker offers `agent:chat` and nothing else."""
    response = await door.get(MODELS_PATH)

    assert response.status_code == httpx.codes.OK
    assert [entry["id"] for entry in response.json()["data"]] == [MODEL]


async def test_an_empty_chat_id_is_refused(owui: OwuiStack, door: httpx.AsyncClient) -> None:
    """Scenario 10. An absent header arrives present but empty, and must fail."""
    response = await door.post(
        CHAT_PATH,
        headers=owui_headers("", message_id()),
        json=chat_body("hello", stream=True),
    )

    assert response.status_code == httpx.codes.BAD_REQUEST
    assert response.json()["error"]["code"] == "missing_chat_id"

    # Nothing may fall back to a shared session: every chat would be in it.
    assert list(owui.tree.mounts().sessions.iterdir()) == []
    assert owui.pi_starts() == []
