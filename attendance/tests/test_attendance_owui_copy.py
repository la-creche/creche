"""Writing a TUI-made session into Open WebUI (contract 02 §10.4).

The fake below stands in for Open WebUI's chat API. Probe 0c measured the
real thing on the host against `v0.11.3`; these tests pin the shapes that
probe proved, and the three rules a failure has to keep.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from attendance.clock import now
from attendance.models import Session
from attendance.owui_copy import ChatCopy, HttpChatApi, TurnCopy, read_api
from attendance.states import SessionKind
from attendance_harness import FAMILY, wait_until

TUI_SESSION = "tui-01JBQ7WZ0X4T9V6K2H8M3N5PQR"
PROMPT = "Which sensor dropped out last night?"
ANSWER = "Sensor kitchen_temp stopped reporting at 02:14."
CHAT_ID = "3f2a9c41-77b0-4a1e-9a4c-1d0e5f8b2c33"
FOLDER_ID = "agents"


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


def test_an_answer_nested_too_deep_reads_as_an_empty_answer() -> None:
    """§10.4 rule 3. The JSON reader raises RecursionError on this body."""
    api = HttpChatApi(
        "http://192.0.2.10:8181",
        "FIXTURE-OWUI-KEY",
        httpx.Client(
            base_url="http://192.0.2.10:8181",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"[" * 200_000)),
        ),
    )

    assert api.create_chat({"title": "Kitchen debug"}) is None
    assert api.append_chat(CHAT_ID, {"messages": {}}) is True


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


async def test_a_writer_that_ends_with_an_error_says_so(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The writer loop guards each write. An error outside that guard ends
    the task, and the log must name the task."""
    copy = ChatCopy(FakeChats())
    copy.start()
    queue = copy._queue
    assert queue is not None

    with caplog.at_level(logging.ERROR, logger="attendance"):
        # Not the pair that `submit` adds, so the read of the queue raises.
        queue.put_nowait(None)  # pyright: ignore[reportArgumentType]
        await wait_until(lambda: "ended with an error" in caplog.text)

    assert "task owui writer ended with an error" in caplog.text


async def test_close_ends_after_the_writer_ended_with_an_error() -> None:
    """`close` is an early step of the close of the service. The error of a
    writer that is gone must not stop the steps after it."""
    copy = ChatCopy(FakeChats())
    copy.start()
    queue = copy._queue
    writer = copy._writer
    assert queue is not None
    assert writer is not None
    # Not the pair that `submit` adds, so the read of the queue raises.
    queue.put_nowait(None)  # pyright: ignore[reportArgumentType]
    await wait_until(writer.done)

    await copy.close()

    assert copy._writer is None


def test_a_deleted_session_is_forgotten() -> None:
    chats = FakeChats(down=True)
    copy = ChatCopy(chats)
    record = session()
    copy.add_turn(record, turn())

    copy.forget(FAMILY, TUI_SESSION)

    assert copy.pending(FAMILY, TUI_SESSION) == 0


# ------------------------------------------------------- through the service
