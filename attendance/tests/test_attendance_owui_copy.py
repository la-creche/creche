"""Writing a TUI-made session into Open WebUI (contract 02 §10.4).

The fake below stands in for Open WebUI's chat API. Probe 0c measured the
real thing on the host against `v0.11.3`; these tests pin the shapes that
probe proved, and the three rules a failure has to keep.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from attendance.auth import Principal
from attendance.clock import now
from attendance.models import LEASE_TTL_S, Holder, LineKind, OwuiRefs, Session
from attendance.owui_copy import ChatCopy, HttpChatApi, TurnCopy, read_api
from attendance.requests import CreateRequest, RunTurnRequest, WriterRequest
from attendance.service import SessionService
from attendance.states import SessionKind
from attendance_harness import (
    CHAT_SESSION,
    FAMILY,
    SANDBOX,
    FakeFleet,
    FakePlaypen,
    PlaypenPlan,
    make_config,
    settle_now,
    wait_until,
    write_status,
)

OWUI = Principal.DOOR_OWUI
TUI = Principal.DOOR_TUI
DOOR = "tui.4021"
TUI_SESSION = "tui-01JBQ7WZ0X4T9V6K2H8M3N5PQR"
PROMPT = "Which sensor dropped out last night?"
ANSWER = "Sensor kitchen_temp stopped reporting at 02:14."
CHAT_ID = "3f2a9c41-77b0-4a1e-9a4c-1d0e5f8b2c33"
#: How long `SlowChats` holds the writer's worker thread after the call is seen.
SLOW_API_S = 0.3
FOLDER_ID = "agents"

#: How long to let a read that must NOT happen fail to happen. Every other
#: wait in this file is on a condition; this one has no condition to wait on.
_OLD_IMAGE_GRACE_S = 0.5


@dataclass
class FakeChats:
    """Open WebUI's three calls, in memory."""

    created: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    appended: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    filed: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    #: Set to make the next write fail, the way an outage does.
    down: bool = False

    def create_chat(self, chat: dict[str, Any]) -> str | None:
        if self.down:
            return None

        self.created.append(chat)
        return CHAT_ID

    def append_chat(self, chat_id: str, history: dict[str, Any]) -> bool:
        if self.down:
            return False

        self.appended.append({"chat": chat_id, "history": history})
        return True

    def file_chat(self, chat_id: str, folder_id: str) -> bool:
        self.filed.append((chat_id, folder_id))
        return True


def session(kind: SessionKind = SessionKind.ATTENDED) -> Session:
    moment = now()
    return Session(
        family=FAMILY,
        session=TUI_SESSION,
        kind=kind,
        created_at=moment,
        updated_at=moment,
        title="Kitchen debug",
    )


def turn(index: int = 1) -> TurnCopy:
    return TurnCopy(prompt=f"{PROMPT} ({index})", answer=f"{ANSWER} ({index})")


# ------------------------------------------------------------- the two calls


def test_the_first_turn_creates_a_chat() -> None:
    """Step 1 of contract 02 §10.4's table, in probe 0c's own shape."""
    chats = FakeChats()
    copy = ChatCopy(chats, FOLDER_ID)
    record = session()

    assert copy.add_turn(record, turn())

    made = chats.created[0]
    history: dict[str, Any] = made["history"]
    messages: dict[str, Any] = history["messages"]
    roles = [one["role"] for one in messages.values()]

    assert record.owui_chat == CHAT_ID
    assert made["models"] == ["agent:chat"]
    assert made["title"] == "Kitchen debug"
    assert roles == ["user", "assistant"]
    assert history["currentId"] == record.owui_leaf
    assert chats.filed == [(CHAT_ID, FOLDER_ID)]


def test_a_later_turn_sends_only_what_changed() -> None:
    """Rule 1. A partial history deep-merges, so nothing is read back."""
    chats = FakeChats()
    copy = ChatCopy(chats)
    record = session()
    copy.add_turn(record, turn(1))
    first_leaf = record.owui_leaf

    copy.add_turn(record, turn(2))

    history: dict[str, Any] = chats.appended[0]["history"]
    messages: dict[str, Any] = history["messages"]

    assert chats.appended[0]["chat"] == CHAT_ID
    assert len(chats.created) == 1
    # The old leaf is patched, never rewritten: its `childrenIds` and nothing
    # else, so turn 1's own content survives the merge.
    assert first_leaf is not None
    assert set(messages[first_leaf]) == {"childrenIds"}
    assert history["currentId"] == record.owui_leaf
    assert record.owui_leaf != first_leaf


def test_the_feature_is_off_without_a_client() -> None:
    """Rule 4. No base URL or no key file changes nothing else."""
    copy = ChatCopy(None)
    record = session()

    assert not copy.enabled
    assert copy.add_turn(record, turn())
    assert record.owui_chat is None


def probe_0c_create(request: httpx.Request) -> httpx.Response:
    """The create answer probe 0c read on the host, step 2.

    The probe took the chat id from `.id`, at the TOP level. `.chat` holds
    the chat body the caller submitted, which carries no id of its own,
    because nothing put one there.
    """
    submitted: Any = json.loads(request.content)

    return httpx.Response(200, json={"id": CHAT_ID, "title": "Kitchen debug", **submitted})


def test_a_create_reads_the_id_probe_0c_measured() -> None:
    """§10.4 rule 2 is about the chat BODY, not about the chat's own id.

    Read the other way round, every create against the real Open WebUI
    answers None, `attendance` calls it a failure and posts `/chats/new`
    again on the next turn, so one terminal session fills the sidebar with
    a new chat per turn and the copy never catches up.
    """
    api = HttpChatApi(
        "http://192.0.2.10:8181",
        "FIXTURE-OWUI-KEY",
        httpx.Client(
            base_url="http://192.0.2.10:8181", transport=httpx.MockTransport(probe_0c_create)
        ),
    )

    assert api.create_chat({"title": "Kitchen debug", "history": {"messages": {}}}) == CHAT_ID


def test_a_create_still_reads_a_nested_id() -> None:
    """A build that answers the id under `.chat` is read too."""
    nested = CHAT_ID.replace("3f2a", "4a3b")
    api = HttpChatApi(
        "http://192.0.2.10:8181",
        "FIXTURE-OWUI-KEY",
        httpx.Client(
            base_url="http://192.0.2.10:8181",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"chat": {"id": nested}})
            ),
        ),
    )

    assert api.create_chat({"title": "Kitchen debug"}) == nested


def test_a_missing_key_file_leaves_it_off(tmp_path: Path) -> None:
    assert read_api("http://192.0.2.10:8080", tmp_path / "absent.key") is None
    assert read_api("", tmp_path / "absent.key") is None


def test_an_empty_key_file_leaves_it_off(tmp_path: Path) -> None:
    path = tmp_path / "owui.key"
    path.write_text("\n", encoding="utf-8")

    assert read_api("http://192.0.2.10:8080", path) is None


# ----------------------------------------------------------------- a failure


def test_a_failed_write_is_retried_next_turn() -> None:
    """Rule 3. The host journal is the record, so nothing fails a turn."""
    chats = FakeChats(down=True)
    copy = ChatCopy(chats)
    record = session()

    assert not copy.add_turn(record, turn(1))
    assert copy.pending(FAMILY, TUI_SESSION) == 1

    chats.down = False

    assert copy.add_turn(record, turn(2))
    assert copy.pending(FAMILY, TUI_SESSION) == 0
    # Both turns arrive, oldest first, so the transcript keeps its order.
    contents = [one["content"] for one in chats.created[0]["history"]["messages"].values()]
    assert contents[0].endswith("(1)")


def test_a_deleted_session_is_forgotten() -> None:
    chats = FakeChats(down=True)
    copy = ChatCopy(chats)
    record = session()
    copy.add_turn(record, turn())

    copy.forget(FAMILY, TUI_SESSION)

    assert copy.pending(FAMILY, TUI_SESSION) == 0


# ------------------------------------------------------- through the service


class Rig:
    def __init__(self, service: SessionService, fleet: FakeFleet, chats: FakeChats) -> None:
        self.service = service
        self.fleet = fleet
        self.chats = chats

    async def full_turn(self, principal: Principal, wanted: str, owui: OwuiRefs | None) -> None:
        self.service.create_or_find(principal, CreateRequest(family=FAMILY, session=wanted))
        await self.one_turn(principal, wanted, owui)

    async def one_turn(
        self,
        principal: Principal,
        wanted: str,
        owui: OwuiRefs | None,
        entry_ids: tuple[str, str] | None = None,
    ) -> None:
        """A turn on a session that already exists.

        Contract 02 §3.1: the session-id prefix check runs on CREATE only,
        so a second door may run a turn on a session it could not have
        made. That is what invariant 3 asks for and what this reproduces.

        `entry_ids` names the two pi entries this turn left, for a test that
        also writes them into the fake playpen's store.
        """
        live = await self.service.run_turn(
            principal, FAMILY, wanted, RunTurnRequest(prompt=PROMPT, owui=owui), DOOR
        )
        playpen = self.fleet.playpen()
        await playpen.next_start()
        await playpen.play_turn(wanted, live.record.turn, ANSWER)

        if entry_ids is None:
            await playpen.settle(wanted, live.record.turn)
        else:
            await playpen.settle(wanted, live.record.turn, *entry_ids)

        await settle_now(live.done)

    async def written(self, count: int) -> None:
        """Wait for the writer task to catch up.

        §10.4 rule 5: the copy left the turn-settle path, so a settled turn
        says nothing about a write having happened yet. A test that read
        the fake the instant a turn settled would be a flaky test.

        And "the fake saw the call" is still one step early: the record is
        saved after the call returns. Two tests read the record right here and
        were red on CI's slower runner for 46 runs while a faster machine passed them.
        """
        await wait_until(lambda: len(self.chats.created) + len(self.chats.appended) >= count)
        await self.service.owui_drained()

    async def stop(self) -> None:
        await self.service.close()
        await self.fleet.stop()


async def build(tmp_path: Path, chats: FakeChats | None = None) -> Rig:
    config = make_config(tmp_path)
    write_status(config.state_root)
    fleet = FakeFleet()
    chats = chats if chats is not None else FakeChats()
    service = SessionService(config, factory=fleet.factory, cold_start_wait_s=1.0, owui=chats)
    service.start()
    # The writer task belongs to the running loop, like the fsync loop.
    service.start_upkeep()
    return Rig(service, fleet, chats)


class SlowChats(FakeChats):
    """The API call is recorded, and then the worker thread is held for a
    moment before it returns. It makes CI's slower runner on any machine: the
    record is saved only after the call returns, so a reader that waits for
    "the fake saw the call" reads the record too early, every time."""

    def create_chat(self, chat: dict[str, Any]) -> str | None:
        made = super().create_chat(chat)
        time.sleep(SLOW_API_S)
        return made


async def test_written_waits_for_the_record_and_not_only_for_the_api(tmp_path: Path) -> None:
    """`written()` waits for the saved record, not only for the fake's call:
    a test that reads the record after the call alone races the save, and a
    slower runner loses."""
    rig = await build(tmp_path, SlowChats())

    await rig.full_turn(TUI, TUI_SESSION, None)
    await rig.written(1)
    record = rig.service.store.load(FAMILY, TUI_SESSION)

    assert record is not None
    assert record.owui_chat == CHAT_ID
    await rig.stop()


async def test_a_tui_session_reaches_open_webui(tmp_path: Path) -> None:
    """The phone shows what was typed in a terminal."""
    rig = await build(tmp_path)

    await rig.full_turn(TUI, TUI_SESSION, None)
    await rig.written(1)
    record = rig.service.store.load(FAMILY, TUI_SESSION)

    assert record is not None
    assert record.owui_chat == CHAT_ID
    assert len(rig.chats.created) == 1
    assert rig.chats.created[0]["history"]["messages"]
    await rig.stop()


async def test_a_settle_never_waits_for_open_webui(tmp_path: Path) -> None:
    """§10.4 rule 5. One process serves every family on one event loop.

    A write on that loop is every chat on the host frozen for as long as
    Open WebUI takes to answer, and `HttpChatApi` waits ten seconds. So
    the settle path may not reach the chat API at all.
    """
    rig = await build(tmp_path)
    rig.chats.down = True

    await rig.full_turn(TUI, TUI_SESSION, None)

    # The turn is settled and the writer has not been let run yet, so the
    # settle plainly did not do the write itself.
    assert rig.chats.created == []
    assert rig.chats.appended == []
    await rig.stop()


async def test_a_chat_born_in_open_webui_is_not_copied(tmp_path: Path) -> None:
    """It already holds this turn: copying it would double every message."""
    rig = await build(tmp_path)
    refs = OwuiRefs(chat_id=CHAT_ID, message_id="b7c1e2d0", user_message_id="a1b2c3d4")

    await rig.full_turn(OWUI, CHAT_SESSION, refs)
    record = rig.service.store.load(FAMILY, CHAT_SESSION)

    assert record is not None
    assert record.owui_chat is None
    assert rig.chats.created == []
    await rig.stop()


async def test_another_door_never_copies_a_chat_session(tmp_path: Path) -> None:
    """§10.4 turns on where the SESSION was born, not on one turn's refs.

    Invariant 3's own case: a chat started on the phone and continued in
    the terminal. That turn carries no `owui` refs, so a check on the refs
    reads it as a session with no chat and creates a SECOND one for a
    session Open WebUI already holds.
    """
    rig = await build(tmp_path)
    refs = OwuiRefs(chat_id=CHAT_ID, message_id="b7c1e2d0", user_message_id="a1b2c3d4")

    await rig.full_turn(OWUI, CHAT_SESSION, refs)
    await rig.one_turn(TUI, CHAT_SESSION, None)
    record = rig.service.store.load(FAMILY, CHAT_SESSION)

    assert record is not None
    assert record.owui_chat is None
    assert rig.chats.created == []
    await rig.stop()


# ---------------------------------------- a terminal's exchanges (§10.5)


async def dialled(rig: Rig) -> FakePlaypen:
    """The sandbox's fake playpen, once the pre-start has dialled it."""
    await wait_until(lambda: SANDBOX in rig.fleet.playpens)
    return rig.fleet.playpen()


async def terminal_exchange(rig: Rig, session_id: str, prompt: str, answer: str) -> None:
    """What a terminal does: pi writes two entries, then the lease ends.

    The TUI door hands its tty to pi inside the sandbox (contract 03 §7.6),
    so nothing of this passes `attendance` as a turn. The host learns it when
    the lease it does hold is given back.
    """
    playpen = await dialled(rig)
    playpen.write_entry("user", prompt)
    playpen.write_entry("assistant", answer)
    rig.service.take_writer(TUI, FAMILY, session_id, WriterRequest(holder=Holder.TUI), "tui.4021")
    rig.service.release_writer(TUI, FAMILY, session_id, "tui.4021")
    await wait_until(lambda: len(playpen.reads) > 0)


def exchanges(rig: Rig, session_id: str) -> list[dict[str, Any]]:
    return [
        line.body
        for line in rig.service.store.journal.replay(FAMILY, session_id)
        if line.kind is LineKind.TERMINAL_EXCHANGE
    ]


async def test_a_terminal_exchange_is_journalled(tmp_path: Path) -> None:
    """§10.5 steps 3 and 4. The operator had three exchanges and saw two."""
    rig = await build(tmp_path)
    refs = OwuiRefs(chat_id=CHAT_ID, message_id="b7c1e2d0", user_message_id="a1b2c3d4")

    await rig.full_turn(OWUI, CHAT_SESSION, refs)
    await terminal_exchange(rig, CHAT_SESSION, "in the terminal", "answered there")
    await wait_until(lambda: len(exchanges(rig, CHAT_SESSION)) == 1)
    record = rig.service.store.load(FAMILY, CHAT_SESSION)

    assert exchanges(rig, CHAT_SESSION)[0]["prompt"] == "in the terminal"
    assert record is not None
    assert record.terminal_total == 1
    assert record.turns_total == 2
    await rig.stop()


async def test_a_terminal_exchange_reaches_the_chat_it_belongs_to(tmp_path: Path) -> None:
    """§10.5 rule 1. The session's own chat, and never a second one."""
    rig = await build(tmp_path)
    refs = OwuiRefs(chat_id=CHAT_ID, message_id="b7c1e2d0", user_message_id="a1b2c3d4")

    await rig.full_turn(OWUI, CHAT_SESSION, refs)
    await terminal_exchange(rig, CHAT_SESSION, "in the terminal", "answered there")
    await rig.written(1)

    assert rig.chats.created == []
    assert len(rig.chats.appended) == 1
    assert rig.chats.appended[0]["chat"] == CHAT_ID
    messages: dict[str, Any] = rig.chats.appended[0]["history"]["messages"]
    written = [one for one in messages.values() if "content" in one]
    # Rule 4. A reader sees where these two came from.
    assert [one["content"] for one in written] == ["in the terminal", "answered there"]
    assert all(one["terminal"] is True for one in written)
    await rig.stop()


async def test_the_next_phone_message_finds_its_parent(tmp_path: Path) -> None:
    """§10.5 rule 5, and the `unmapped_parent` line the operator's journal held.

    Open WebUI replies under the newest message of the chat, which after a
    terminal exchange is the one the write-back minted. Unless the map
    covers it, §10.2 rule 4 writes `branch_fallback`.
    """
    rig = await build(tmp_path)
    first = OwuiRefs(chat_id=CHAT_ID, message_id="b7c1e2d0", user_message_id="a1b2c3d4")

    await rig.full_turn(OWUI, CHAT_SESSION, first)
    await terminal_exchange(rig, CHAT_SESSION, "in the terminal", "answered there")
    await rig.written(1)
    record = rig.service.store.load(FAMILY, CHAT_SESSION)

    assert record is not None
    parent = record.owui_leaf
    assert parent is not None
    assert record.owui_map[parent] == "e2"

    await rig.one_turn(
        OWUI,
        CHAT_SESSION,
        OwuiRefs(
            chat_id=CHAT_ID, message_id="c9d2f3a1", user_message_id="b3c4d5e6", parent_id=parent
        ),
    )
    fallbacks = [
        line
        for line in rig.service.store.journal.replay(FAMILY, CHAT_SESSION)
        if line.kind is LineKind.BRANCH_FALLBACK
    ]

    assert fallbacks == []
    await rig.stop()


async def test_a_turn_is_never_read_back_as_a_terminal_exchange(tmp_path: Path) -> None:
    """§10.5 rule 3. The cursor covers turns too, or every turn copies twice."""
    rig = await build(tmp_path)
    refs = OwuiRefs(chat_id=CHAT_ID, message_id="b7c1e2d0", user_message_id="a1b2c3d4")

    rig.service.create_or_find(OWUI, CreateRequest(family=FAMILY, session=CHAT_SESSION))
    playpen = await dialled(rig)
    # The turn this service ran wrote its own two entries into the store.
    ids = (playpen.write_entry("user", PROMPT), playpen.write_entry("assistant", ANSWER))

    await rig.one_turn(OWUI, CHAT_SESSION, refs, entry_ids=ids)
    rig.service.take_writer(TUI, FAMILY, CHAT_SESSION, WriterRequest(holder=Holder.TUI), "tui.4021")
    rig.service.release_writer(TUI, FAMILY, CHAT_SESSION, "tui.4021")
    await wait_until(lambda: len(playpen.reads) > 0)

    assert playpen.reads[0]["since"] == ids[1]
    assert exchanges(rig, CHAT_SESSION) == []
    assert rig.chats.appended == []
    await rig.stop()


async def test_a_terminal_exchange_is_journalled_with_the_copy_off(tmp_path: Path) -> None:
    """§10.5 rule 6. The journal is the record, so it holds it either way."""
    config = make_config(tmp_path)
    write_status(config.state_root)
    fleet = FakeFleet()
    service = SessionService(config, factory=fleet.factory, cold_start_wait_s=1.0, owui=None)
    service.start()
    service.start_upkeep()
    rig = Rig(service, fleet, FakeChats())

    service.create_or_find(TUI, CreateRequest(family=FAMILY, session=TUI_SESSION))
    await terminal_exchange(rig, TUI_SESSION, "with no open webui", "still journalled")
    await wait_until(lambda: len(exchanges(rig, TUI_SESSION)) == 1)

    assert exchanges(rig, TUI_SESSION)[0]["answer"] == "still journalled"
    assert rig.chats.created == []
    await rig.stop()


async def test_a_refused_read_leaves_the_cursor_where_it_was(tmp_path: Path) -> None:
    """§10.5's last paragraph. The next release reads it again."""
    rig = await build(tmp_path)

    rig.service.create_or_find(TUI, CreateRequest(family=FAMILY, session=TUI_SESSION))
    playpen = await dialled(rig)
    playpen.entries_ok = False
    await terminal_exchange(rig, TUI_SESSION, "asked", "answered")
    record = rig.service.store.load(FAMILY, TUI_SESSION)

    assert exchanges(rig, TUI_SESSION) == []
    assert record is not None
    assert record.pi_cursor is None

    playpen.entries_ok = True
    rig.service.take_writer(TUI, FAMILY, TUI_SESSION, WriterRequest(holder=Holder.TUI), "tui.4021")
    rig.service.release_writer(TUI, FAMILY, TUI_SESSION, "tui.4021")
    await wait_until(lambda: len(exchanges(rig, TUI_SESSION)) == 1)

    assert exchanges(rig, TUI_SESSION)[0]["prompt"] == "asked"
    await rig.stop()


async def test_a_capped_answer_is_read_again_at_once(tmp_path: Path) -> None:
    """§10.5 rule 3. Contract 03 §8 caps one answer, not one terminal visit.

    Without the follow-up read, half of a long terminal session would wait
    on the platter until the operator next opened a terminal.
    """
    rig = await build(tmp_path)

    rig.service.create_or_find(TUI, CreateRequest(family=FAMILY, session=TUI_SESSION))
    playpen = await dialled(rig)
    playpen.entries_cap = 2
    playpen.write_entry("user", "first")
    playpen.write_entry("assistant", "one")
    playpen.write_entry("user", "second")
    playpen.write_entry("assistant", "two")

    rig.service.take_writer(TUI, FAMILY, TUI_SESSION, WriterRequest(holder=Holder.TUI), "tui.4021")
    rig.service.release_writer(TUI, FAMILY, TUI_SESSION, "tui.4021")
    await wait_until(lambda: len(exchanges(rig, TUI_SESSION)) == 2)

    assert [one["prompt"] for one in exchanges(rig, TUI_SESSION)] == ["first", "second"]
    assert len(playpen.reads) == 2
    await rig.stop()


async def test_a_copied_turn_is_mapped_too(tmp_path: Path) -> None:
    """§10.5 rule 5 holds for §10.4's own copy, for the same reason.

    A terminal-made session gains a chat. The operator then types in that chat,
    and the parent they name is a message id the WRITE-BACK minted. Unless
    the map covers it, §10.2 rule 4 writes `branch_fallback` there too.
    """
    rig = await build(tmp_path)

    await rig.full_turn(TUI, TUI_SESSION, None)
    await rig.written(1)
    record = rig.service.store.load(FAMILY, TUI_SESSION)

    assert record is not None
    parent = record.owui_leaf
    assert parent is not None
    assert record.owui_map[parent] == "e5f6a7b8"

    await rig.one_turn(
        OWUI,
        TUI_SESSION,
        OwuiRefs(
            chat_id=CHAT_ID, message_id="c9d2f3a1", user_message_id="b3c4d5e6", parent_id=parent
        ),
    )
    fallbacks = [
        line
        for line in rig.service.store.journal.replay(FAMILY, TUI_SESSION)
        if line.kind is LineKind.BRANCH_FALLBACK
    ]

    assert fallbacks == []
    await rig.stop()


async def test_a_killed_terminal_is_read_when_its_lease_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§10.5. A terminal that is killed never releases anything.

    Its lease runs out instead, and §7.3 rule 2 GRANTS to the next door
    rather than taking over. A check on the LIVE lease alone therefore
    never learns a terminal was here, and the exchange waits for ever.
    """
    rig = await build(tmp_path)

    rig.service.create_or_find(TUI, CreateRequest(family=FAMILY, session=TUI_SESSION))
    playpen = await dialled(rig)
    playpen.write_entry("user", "typed before the window closed")
    playpen.write_entry("assistant", "answered there")
    rig.service.take_writer(TUI, FAMILY, TUI_SESSION, WriterRequest(holder=Holder.TUI), "tui.4021")

    # No release: the process is gone. Only the TTL ends this lease, so the
    # lease book's clock moves past it.
    later = now() + timedelta(seconds=LEASE_TTL_S + 1)
    monkeypatch.setattr("attendance.leases.now", lambda: later)
    rig.service.take_writer(OWUI, FAMILY, TUI_SESSION, WriterRequest(holder=Holder.OWUI), DOOR)
    await wait_until(lambda: len(exchanges(rig, TUI_SESSION)) == 1)

    assert exchanges(rig, TUI_SESSION)[0]["prompt"] == "typed before the window closed"
    await rig.stop()


async def test_an_old_sandbox_image_is_named_rather_than_waited_on(tmp_path: Path) -> None:
    """Contract 03 §3. A bundle built before §4.8 answers nothing at all.

    Asking it anyway would leave a read waiting for ever and lose every
    terminal exchange in silence, which is the failure §10.5 exists to
    remove. `ready.caps` is what says whether to ask.
    """
    config = make_config(tmp_path)
    write_status(config.state_root)
    fleet = FakeFleet()
    fleet.plan(SANDBOX, PlaypenPlan(caps=("steer", "coalesce", "workspace_link")))
    service = SessionService(config, factory=fleet.factory, cold_start_wait_s=1.0, owui=FakeChats())
    service.start()
    service.start_upkeep()
    rig = Rig(service, fleet, FakeChats())

    service.create_or_find(TUI, CreateRequest(family=FAMILY, session=TUI_SESSION))
    playpen = await dialled(rig)
    playpen.write_entry("user", "asked")
    playpen.write_entry("assistant", "answered")
    service.take_writer(TUI, FAMILY, TUI_SESSION, WriterRequest(holder=Holder.TUI), "tui.4021")
    service.release_writer(TUI, FAMILY, TUI_SESSION, "tui.4021")
    await asyncio.sleep(_OLD_IMAGE_GRACE_S)

    assert playpen.reads == []
    assert exchanges(rig, TUI_SESSION) == []
    await rig.stop()
