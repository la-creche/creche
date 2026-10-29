"""The read-only routes: the session list, one transcript, and `/busy`.

Each view request opens one relay to the family's facade, sends
`initialize` and one read method, and closes it. The relay is a
`FakeChannel` that `FacadeFleet.relay` serves with scripted answers.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from attendance.acp_door import Close, DoorPrompts
from attendance.api import build_app
from attendance.auth import Principal, TokenBook
from attendance.config import Config
from attendance.service import SessionService
from attendance_harness import FAMILY, make_config, wait_until, write_status, write_tokens
from creche_vectors import meta_key
from fake_facade import FacadeFleet, PromptMode

from attendance import acp_view

SESSIONS = f"/families/{FAMILY}/sessions"
SESSION_ID = "3f2a9c41-77b0-4a1e-9a4c-1d0e5f8b2c33"
ONE = f"{SESSIONS}/{SESSION_ID}"
HTTP_OK = 200
HTTP_BAD_REQUEST = 400
HTTP_FORBIDDEN = 403
HTTP_NOT_FOUND = 404
HTTP_UNAVAILABLE = 503
JOB_FAMILY = "issue-worker"
JOB_SANDBOX = "issue-worker-s1"
TRIGGER = {"kind": "dispatch", "name": "scrum-lead", "fired_at": "2026-10-25T12:00:00Z"}


def row(number: int, **meta: Any) -> dict[str, Any]:
    """One `session/list` row as the facade sends it."""
    fields: dict[str, Any] = {
        "sessionId": f"s-{number}",
        "cwd": "/srv/agents/sessions/chat",
        "title": f"Talk {number}",
        "updatedAt": f"2026-10-2{number}T10:00:00.000Z",
    }
    return {**fields, "_meta": meta} if meta else fields


def text(kind: str, said: str) -> dict[str, Any]:
    return {"sessionUpdate": kind, "content": {"type": "text", "text": said}}


def tool_call(call: str, title: str) -> dict[str, Any]:
    return {
        "sessionUpdate": "tool_call",
        "toolCallId": call,
        "title": title,
        "kind": "other",
        "status": "in_progress",
        "rawInput": {"path": "/srv"},
    }


def tool_end(call: str, status: str, said: str) -> dict[str, Any]:
    content = [{"type": "content", "content": {"type": "text", "text": said}}]
    return {
        "sessionUpdate": "tool_call_update",
        "toolCallId": call,
        "status": status,
        "content": content,
    }


@dataclass(slots=True)
class Rig:
    config: Config
    service: SessionService
    client: httpx.AsyncClient
    tokens: dict[Principal, str]
    facades: FacadeFleet

    def head(self, principal: Principal = Principal.NOTICEBOARD_RO) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens[principal]}"}

    async def get(
        self, path: str, principal: Principal = Principal.NOTICEBOARD_RO
    ) -> httpx.Response:
        return await self.client.get(path, headers=self.head(principal))


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    config = make_config(tmp_path)
    write_status(config.state_root)
    made = write_tokens(config.state_root)
    book = TokenBook(config.state_root)
    book.load()
    facades = FacadeFleet(prompt_mode=PromptMode.HOLD)
    service = SessionService(
        config, acp_factory=facades.factory, relay_factory=facades.relay, cold_start_wait_s=1.0
    )
    service.start()
    transport = httpx.ASGITransport(app=build_app(service, book))
    client = httpx.AsyncClient(transport=transport, base_url="http://sessiond")

    yield Rig(config, service, client, made, facades)

    await client.aclose()
    await service.close()
    await facades.stop()


def read_methods(rig: Rig) -> list[list[str]]:
    return [facade.methods for facade in rig.facades.relays]


async def test_the_session_list_pages_with_the_facade_cursor(rig: Rig) -> None:
    meta = {meta_key("job"): "auto-01JBQ7ZZ9D6M0Q4RXT2J8HYVBK", meta_key("trigger"): TRIGGER}
    rig.facades.pages[""] = {"sessions": [row(2, **meta), row(1)], "nextCursor": "51"}
    rig.facades.pages["51"] = {"sessions": [row(0)], "nextCursor": None}

    first = await rig.get(SESSIONS)
    second = await rig.get(f"{SESSIONS}?cursor=51")

    assert first.status_code == HTTP_OK
    assert first.json() == {
        "sessions": [
            {
                "id": "s-2",
                "title": "Talk 2",
                "created": None,
                "updated": "2026-10-22T10:00:00.000Z",
                "job_id": "auto-01JBQ7ZZ9D6M0Q4RXT2J8HYVBK",
                "delegation": None,
                "trigger": TRIGGER,
            },
            {
                "id": "s-1",
                "title": "Talk 1",
                "created": None,
                "updated": "2026-10-21T10:00:00.000Z",
                "job_id": None,
                "delegation": None,
                "trigger": None,
            },
        ],
        "next_cursor": "51",
    }
    assert [one["id"] for one in second.json()["sessions"]] == ["s-0"]
    assert second.json()["next_cursor"] is None
    assert read_methods(rig) == [["initialize", "session/list"], ["initialize", "session/list"]]
    assert rig.facades.relays[1].received[1]["params"] == {"cursor": "51"}


async def test_a_transcript_holds_each_update_kind(rig: Rig) -> None:
    rig.facades.replays[SESSION_ID] = [
        text("user_message_chunk", "Is the boiler serviced?"),
        text("agent_thought_chunk", "Check the log."),
        tool_call("t1", "read"),
        tool_end("t1", "completed", "serviced 2026-03-11"),
        tool_call("t2", "bash"),
        tool_end("t2", "failed", "exit 1"),
        text("agent_message_chunk", "Yes, "),
        text("agent_message_chunk", "in March."),
        {"sessionUpdate": "session_info_update", "title": "Boiler"},
    ]

    answer = await rig.get(ONE)

    assert answer.status_code == HTTP_OK
    assert answer.json() == {
        "session_id": SESSION_ID,
        "entries": [
            {"position": 0, "kind": "user", "text": "Is the boiler serviced?"},
            {"position": 1, "kind": "thought", "text": "Check the log."},
            {
                "position": 2,
                "kind": "tool_call",
                "tool_call_id": "t1",
                "title": "read",
                "status": "completed",
                "result": "serviced 2026-03-11",
            },
            {
                "position": 3,
                "kind": "tool_call",
                "tool_call_id": "t2",
                "title": "bash",
                "status": "failed",
                "result": "exit 1",
            },
            {"position": 4, "kind": "agent", "text": "Yes, in March."},
        ],
        "next": None,
        "truncated": False,
    }
    assert read_methods(rig) == [["initialize", "session/load"]]


async def test_a_transcript_pages_by_position(rig: Rig) -> None:
    rig.facades.replays[SESSION_ID] = [
        text("user_message_chunk", "one"),
        text("agent_message_chunk", "two"),
        text("user_message_chunk", "three"),
    ]

    first = (await rig.get(f"{ONE}?limit=2")).json()
    rest = (await rig.get(f"{ONE}?start=2&limit=2")).json()

    assert [entry["position"] for entry in first["entries"]] == [0, 1]
    assert first["next"] == 2
    assert rest["entries"] == [{"position": 2, "kind": "user", "text": "three"}]
    assert rest["next"] is None


async def test_a_long_text_is_cut_at_the_cap(rig: Rig) -> None:
    rig.facades.replays[SESSION_ID] = [text("agent_message_chunk", "é" * acp_view.MAX_TEXT_BYTES)]

    entry = (await rig.get(ONE)).json()["entries"][0]

    assert len(entry["text"].encode("utf-8")) == acp_view.MAX_TEXT_BYTES


async def test_a_replay_over_the_update_cap_is_truncated(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(acp_view, "MAX_UPDATES", 2)
    rig.facades.replays[SESSION_ID] = [text("user_message_chunk", str(n)) for n in range(5)]
    rig.facades.replays[SESSION_ID][1] = text("agent_message_chunk", "1")

    answer = (await rig.get(ONE)).json()

    assert [entry["text"] for entry in answer["entries"]] == ["0", "1"]
    assert answer["truncated"] is True


@pytest.mark.parametrize(
    "query",
    ["limit=0", f"limit={acp_view.MAX_PAGE_ENTRIES + 1}", "start=-1", "start=x", "limit=1e3"],
)
async def test_a_bad_page_is_bad_request(rig: Rig, query: str) -> None:
    answer = await rig.get(f"{ONE}?{query}")

    assert answer.status_code == HTTP_BAD_REQUEST
    assert rig.facades.relays == []


@pytest.mark.parametrize("cursor", ["x" * (acp_view.MAX_CURSOR_BYTES + 1), "a b"])
async def test_a_bad_cursor_is_bad_request(rig: Rig, cursor: str) -> None:
    answer = await rig.client.get(SESSIONS, params={"cursor": cursor}, headers=rig.head())

    assert answer.status_code == HTTP_BAD_REQUEST
    assert rig.facades.relays == []


async def test_an_unknown_session_is_not_found(rig: Rig) -> None:
    answer = await rig.get(ONE)

    assert answer.status_code == HTTP_NOT_FOUND
    assert answer.json()["error"]["code"] == "not_found"


async def test_an_unknown_family_is_not_found(rig: Rig) -> None:
    answer = await rig.get("/families/nobody/sessions")

    assert answer.status_code == HTTP_NOT_FOUND
    assert answer.json()["error"]["code"] == "family_unknown"


async def test_a_family_with_no_sandbox_is_unavailable(rig: Rig) -> None:
    write_status(rig.config.state_root, sandboxes=())

    for path in (SESSIONS, ONE):
        answer = await rig.get(path)

        assert answer.status_code == HTTP_UNAVAILABLE
        assert answer.json()["error"]["code"] == "sandbox_unavailable"

    assert rig.facades.relays == []


async def test_a_relay_with_no_facade_is_unavailable(rig: Rig) -> None:
    rig.facades.relay_gone = True

    answer = await rig.get(SESSIONS)

    assert answer.status_code == HTTP_UNAVAILABLE
    assert answer.json()["error"]["code"] == "sandbox_unavailable"


@pytest.mark.parametrize("path", [SESSIONS, ONE, "/busy"])
@pytest.mark.parametrize(
    "principal", [Principal.DOOR_DISPATCH, Principal.DOOR_ACP, Principal.CAREGIVER]
)
async def test_each_other_principal_is_forbidden(rig: Rig, path: str, principal: Principal) -> None:
    answer = await rig.get(path, principal)

    assert answer.status_code == HTTP_FORBIDDEN
    assert rig.facades.relays == []


async def test_busy_is_false_with_nothing_in_flight(rig: Rig) -> None:
    answer = await rig.get("/busy")

    assert answer.status_code == HTTP_OK
    assert answer.json() == {
        "busy": False,
        "prompts_in_flight": 0,
        "jobs_running": 0,
        "jobs_queued": 0,
    }


async def test_busy_counts_running_and_queued_jobs(rig: Rig) -> None:
    write_status(
        rig.config.state_root,
        family=JOB_FAMILY,
        kind="autonomous",
        sandboxes=((JOB_SANDBOX, "ready"),),
        accepts_dispatch=True,
        max_running_turns=1,
    )
    body = {
        "caller_family": FAMILY,
        "target_family": JOB_FAMILY,
        "delegation_id": "01JBQ7WZ0X4T9V6K2H8M3N5PQR",
        "message": "Work the ticket.",
    }

    for _ in range(2):
        sent = await rig.client.post(
            "/dispatch", json=body, headers=rig.head(Principal.DOOR_DISPATCH)
        )
        assert sent.status_code == HTTP_OK

    facades = rig.facades.facades
    await wait_until(lambda: JOB_SANDBOX in facades and len(facades[JOB_SANDBOX].held) == 1)
    answer = await rig.get("/busy")

    assert answer.json() == {
        "busy": True,
        "prompts_in_flight": 1,
        "jobs_running": 1,
        "jobs_queued": 1,
    }


def test_the_door_counts_a_prompt_until_its_answer() -> None:
    prompts = DoorPrompts()

    with prompts.connection() as one, prompts.connection() as two:
        one.sent('{"jsonrpc":"2.0","id":"p","method":"session/prompt","params":{}}')
        two.sent('{"jsonrpc":"2.0","id":4,"method":"session/prompt","params":{}}')
        two.sent('{"jsonrpc":"2.0","id":5,"method":"session/list","params":{}}')
        assert prompts.in_flight == 2

        two.answered('{"jsonrpc":"2.0","id":"4","result":{"stopReason":"end_turn"}}')
        two.answered('{"jsonrpc":"2.0","id":4,"result":{"stopReason":"end_turn"}}')
        assert prompts.in_flight == 1

    assert prompts.in_flight == 0


def prompt(rpc_id: str) -> str:
    """A `session/prompt` request with `rpc_id` as its id, in JSON text."""
    return f'{{"jsonrpc":"2.0","id":{rpc_id},"method":"session/prompt","params":{{}}}}'


def answer(rpc_id: str) -> str:
    """The answer to a prompt with `rpc_id` as its id, in JSON text."""
    return f'{{"jsonrpc":"2.0","id":{rpc_id},"result":{{"stopReason":"end_turn"}}}}'


@pytest.mark.parametrize(
    ("sent", "answered"),
    [
        ("1.0", "1"),
        ("1", "1.0"),
        ("1e0", "1"),
        ("1.5", "1.5"),
        ("9007199254740993", "9007199254740992"),
    ],
    ids=["float_then_int", "int_then_float", "exponent", "fraction", "past_2_53"],
)
def test_the_door_keys_a_number_id_by_its_double(sent: str, answered: str) -> None:
    prompts = DoorPrompts()

    with prompts.connection() as one:
        assert one.sent(prompt(sent)) is None
        assert prompts.in_flight == 1

        one.answered(answer(answered))
        assert prompts.in_flight == 0


def test_the_door_keeps_a_string_id_apart_from_a_number_id() -> None:
    prompts = DoorPrompts()

    with prompts.connection() as one:
        one.sent(prompt('"1"'))
        one.sent(prompt("1"))
        assert prompts.in_flight == 2

        one.answered(answer("1.0"))
        assert prompts.in_flight == 1

        one.answered(answer('"1"'))
        assert prompts.in_flight == 0


@pytest.mark.parametrize(
    "rpc_id", ["1e999", "-1e999", "9" * 400], ids=["exponent", "negative", "long_integer"]
)
def test_the_door_refuses_a_prompt_id_past_the_double_range(rpc_id: str) -> None:
    prompts = DoorPrompts()

    with prompts.connection() as one:
        assert one.sent(prompt(rpc_id)) is Close.BAD_FRAME
        assert prompts.in_flight == 0
