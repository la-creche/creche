"""The status document tells the truth about a serving sandbox (packet FX2).

Gate 1b passed on the host on 2026-09-19 with its status document saying
`"state": "creating"` for a sandbox that had already answered a chat turn,
because nothing in the system ever wrote `ready`. The one view shows that
field, so the view was lying about a live family.

This is the cross-package half of the fix, which no unit test can reach:
`managerd`'s §5 call has to run the REAL handshake against the REAL
playpen bundle inside `sessiond`, and the answer to that call is the only
evidence `managerd` is allowed to have (contract 05 §1, §4.2, §4.3 step 7).

    reconcile_family ──§5──► sessiond ──channel──► playpen
            │                                          │
            └────────── ready ◄──── "switched: true" ◄──┘

One scenario, three facts: the document says `ready`, it says WHEN, and the
sandbox it says that about is the one that answers the next turn.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from conftest import chat_body, chat_id, message_id, owui_headers, session_of
from stack import SANDBOX, Stack
from stage2 import Manager, write_registry

CHAT_PATH = "/v1/chat/completions"
HTTP_OK = 200
TURN_STARTED = "turn_started"


async def test_the_sandbox_that_serves_is_published_ready(stack: Stack, tmp_path: Path) -> None:
    """Contract 05 §4.3 step 7, end to end.

    `ready` means "the channel handshake passed" (§4.1), and `sessiond` holds
    the only channel (§4.2). So this passes only if a real handshake ran and
    `managerd` wrote down what it was told.
    """
    manager = Manager(stack, write_registry(tmp_path / "registry"))

    await manager.pass_once()

    row = _sandbox_row(manager.status(), SANDBOX)
    assert row["state"] == "ready"
    assert row["ready_at"] is not None

    # `power` and `channel` are derived from the state, so both were wrong
    # for the whole life of every sandbox before this.
    assert row["power"] == "running"


async def test_the_ready_sandbox_is_the_one_that_answers(stack: Stack, tmp_path: Path) -> None:
    """The document would be no better if it named a sandbox nothing used.

    A turn records the sandbox it started on in the session journal
    (contract 02 §8), so the two can be compared rather than assumed.
    """
    manager = Manager(stack, write_registry(tmp_path / "registry"))
    await manager.pass_once()
    ready = [one["id"] for one in manager.status()["sandboxes"] if one["state"] == "ready"]

    chat = chat_id()
    await _one_turn(stack, chat)

    assert _sandboxes_of(stack, session_of(chat)) == ready


async def test_a_second_pass_asks_for_no_second_handshake(stack: Stack, tmp_path: Path) -> None:
    """`ready` is an event that happened, not a level to re-measure. The
    reconcile loop runs every two seconds, and an `sbx exec` on that path
    would cost 2.7 s of CLI start every time."""
    manager = Manager(stack, write_registry(tmp_path / "registry"))
    await manager.pass_once()

    second = await manager.pass_once()

    assert second.ran == ()
    assert _sandbox_row(manager.status(), SANDBOX)["state"] == "ready"


def _sandbox_row(status: dict[str, Any], sandbox: str) -> dict[str, Any]:
    found = [one for one in status["sandboxes"] if one["id"] == sandbox]
    assert len(found) == 1, status["sandboxes"]

    return found[0]


def _sandboxes_of(stack: Stack, session: str) -> list[str]:
    body: list[str] = []

    for line in stack.journal_lines(session):
        if line.get("kind") != TURN_STARTED:
            continue

        inner = line.get("body")
        body.append(str(inner.get("sandbox", "")) if isinstance(inner, dict) else "")

    return body


async def _one_turn(stack: Stack, chat: str) -> None:
    """One streamed turn through the real door."""
    client = stack.client

    assert client is not None

    async with client.stream(
        "POST",
        CHAT_PATH,
        headers=owui_headers(chat, message_id()),
        json=chat_body("hello", stream=True),
    ) as response:
        assert response.status_code == HTTP_OK, await response.aread()
        await response.aread()
