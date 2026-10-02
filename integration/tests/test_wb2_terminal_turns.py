"""Packet WB2: a terminal's turns show up in the chat they belong to.

The operator's stage 4 test on gate 3, 2026-09-20: a chat on the phone, the SAME
session in `agent-tui chat`, then the phone again. The model remembered
everything, so invariant 3 held. Their journal showed the cost:

    writer_changed  holder=tui   reason=taken_over
    writer_changed  holder=tui   reason=released
    writer_changed  holder=owui  reason=granted
    branch_fallback wanted_entry=null reason=unmapped_parent

The exchange they had in the terminal was in no Open WebUI transcript, the
phone's next message named a parent `sessiond` could not map, and
`turns_total` said 2 where they had three exchanges.

    the phone  ─http─► door-owui ─uds─► sessiond ─► supervisor ─► fake pi
    the terminal ────► door-tui  ─uds─►    │  ▲        get_entries   │
                                           │  └───────────────────── ┘
                                           └─http─► FakeOwui

`wb2.py` writes what a terminal's pi process leaves behind and says why.
Everything after the lease release is the real path.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from agent_sessiond.auth import Principal
from conftest import chat_id, message_id, session_of
from stack import FAMILY, Stack
from stage4 import FakeOwui, OwuiMood, Stage4, owui_config, serving_owui, settle
from wb2 import copied, terminal_visit

pytestmark = pytest.mark.slow

TERMINAL = "tui.7001"
HTTP_OK = 200

TERMINAL_PROMPT = "what did the sensor read"
TERMINAL_ANSWER = "21.4 degrees at 09:12"


@pytest.fixture
async def stage(roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Stage4]:
    """Stage 4's own stack. Only the scenario below is this packet's."""
    tree, socket_dir = roots
    built = Stack(tree, socket_dir)
    built.build_fixture()
    owui = FakeOwui()
    ready = Stage4(built, owui)

    with serving_owui(owui) as url:
        owui_config(ready, url, monkeypatch)

        for name, value in ready.sandbox_environ().items():
            monkeypatch.setenv(name, value)

        await built.serve()

        try:
            yield ready
        finally:
            await built.close()


async def phone_turn(
    stage: Stage4, chat: str, text: str, parent: str | None = None
) -> tuple[str, str]:
    """One phone turn, mapping both of its messages (contract 02 §10.1)."""
    user = message_id()
    assistant = message_id()
    answer = await stage.owui_turn(chat, assistant, text, user_message=user, parent=parent)

    assert answer.status_code == HTTP_OK, answer.text

    return user, assistant


def exchanges(stage: Stage4, session: str) -> list[str]:
    """Every exchange the journal holds, in order, as its prompt.

    A turn and a terminal exchange are different line kinds and the same
    thing to a reader of the transcript: the session service is the
    transcript truth.
    """
    wanted = ("turn_started", "terminal_exchange")

    return [str(line["body"]["prompt"]) for line in stage.lines_of(session, *wanted)]


async def test_a_terminal_exchange_reaches_the_phone(stage: Stage4) -> None:
    """Phone turn, terminal exchange, phone turn: the operator's own three."""
    chat = chat_id()
    session = session_of(chat)

    await phone_turn(stage, chat, "first, on the phone")
    await terminal_visit(stage, session, TERMINAL, TERMINAL_PROMPT, TERMINAL_ANSWER)

    await settle(
        lambda: len(stage.lines_of(session, "terminal_exchange")) == 1,
        "the terminal exchange to be journalled",
    )
    await settle(lambda: len(stage.owui.appends()) == 1, "the write-back to send it")

    # The chat gains the terminal's exchange and no second chat is made:
    # §10.4 rule 6 fixes which chat, and §10.5 rule 1 keeps it.
    assert stage.owui.creates() == []
    assert stage.owui.appends()[0].path.endswith(chat)
    assert copied(stage) == [(TERMINAL_PROMPT, TERMINAL_ANSWER, True)]

    # The phone answers under the chat's newest message, which is the one
    # the write-back just minted.
    parent = stage.owui.appends()[0].current_id()
    await phone_turn(stage, chat, "third, on the phone again", parent=parent)

    assert exchanges(stage, session) == [
        "first, on the phone",
        TERMINAL_PROMPT,
        "third, on the phone again",
    ]
    # §10.5 rule 5. The map covers the terminal's entries, so §10.2 rule 4
    # never runs. This line is the one the operator saw.
    assert stage.lines_of(session, "branch_fallback") == []


async def test_the_journal_counts_a_terminal_exchange(stage: Stage4) -> None:
    """§10.5 step 4. `turns_total` said 2 where the operator had three."""
    chat = chat_id()
    session = session_of(chat)

    await phone_turn(stage, chat, "first, on the phone")
    await terminal_visit(stage, session, TERMINAL, TERMINAL_PROMPT, TERMINAL_ANSWER)

    await settle(
        lambda: len(stage.lines_of(session, "terminal_exchange")) == 1,
        "the terminal exchange to be journalled",
    )
    client = stage.stack.sessiond_as(Principal.DOOR_TUI)

    try:
        answer = await client.get(f"/v1/sessions/{FAMILY}/{session}")
    finally:
        await client.aclose()

    body = answer.json()

    assert body["turns_total"] == 2
    assert body["terminal_total"] == 1


async def test_a_failing_open_webui_loses_nothing(stage: Stage4) -> None:
    """§10.5's last paragraph, and §10.4 rule 3's retry.

    The journal is the record, so a refused write costs the copy and never
    the exchange. The next write sends what is waiting, oldest first.
    """
    chat = chat_id()
    session = session_of(chat)

    await phone_turn(stage, chat, "first, on the phone")
    stage.owui.mood = OwuiMood.REFUSE
    await terminal_visit(stage, session, TERMINAL, TERMINAL_PROMPT, TERMINAL_ANSWER)

    await settle(
        lambda: len(stage.lines_of(session, "terminal_exchange")) == 1,
        "the exchange to be journalled anyway",
    )
    await settle(lambda: len(stage.owui.appends()) == 1, "the refused write")

    assert copied(stage) == []

    stage.owui.mood = OwuiMood.ANSWER
    await terminal_visit(stage, session, TERMINAL, "asked again", "answered again")

    await settle(lambda: len(copied(stage)) == 2, "both exchanges to arrive")

    # Oldest first: the transcript keeps its order through an outage.
    assert copied(stage) == [
        (TERMINAL_PROMPT, TERMINAL_ANSWER, True),
        ("asked again", "answered again", True),
    ]
