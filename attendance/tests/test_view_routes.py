"""The read-only routes: the session list, one transcript, and `/busy`.

The session list is a page of the host list, with no boot (contract 02 §7
rule 1). A transcript opens the session's vmm stream, sends `initialize`
and `session/load`, and closes it (§7 rule 3). `FakeVmm` keeps the rows,
and its facades answer each stream with scripted replays.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from attendance.acp_door import DoorPrompts
from attendance.acp_link import loads_strict
from attendance.api import build_app
from attendance.auth import Principal, TokenBook
from attendance.config import Config
from attendance.paths import SANDBOX_MOUNT_ENV
from attendance.service import SessionService
from attendance_harness import (
    FAMILY,
    make_config,
    make_service,
    wait_until,
    write_status,
    write_tokens,
)
from creche_wire import (
    Auto,
    AutoJobId,
    AutoOrigin,
    CreateSession,
    Delegate,
    DelegateOrigin,
    DelegationId,
    Dispatch,
    DispatchChain,
    DispatchTrigger,
    Door,
    DoorOrigin,
    FamilyName,
    JobId,
    SessionCwd,
    SessionId,
    SessionOrigin,
    Timer,
    TimerTrigger,
    Trigger,
    UnixMillis,
    VmState,
)
from fake_facade import PromptMode
from fake_vmm import BUSY, FakeVmm

from attendance import acp_view

SESSIONS = f"/families/{FAMILY}/sessions"
SESSION_ID = "3f2a9c41-77b0-4a1e-9a4c-1d0e5f8b2c33"
ONE = f"{SESSIONS}/{SESSION_ID}"
HTTP_OK = 200
HTTP_BAD_REQUEST = 400
HTTP_FORBIDDEN = 403
HTTP_NOT_FOUND = 404
HTTP_CONFLICT = 409
HTTP_INTERNAL = 500
HTTP_UNAVAILABLE = 503
JOB_FAMILY = "issue-worker"
CWD = "/srv/agents/sessions/chat"
#: 2026-10-25T12:00:00Z, as a wire time.
CREATED_MS = 1_792_929_600_000
CREATED = "2026-10-25T12:00:00Z"
AUTO_JOB = "auto-01JBQ7ZZ9D6M0Q4RXT2J8HYVBK"
THIN_JOB = "job-01JBQ7ZZ9D6M0Q4RXT2J8HYVBK"
DELEGATION = "01JBQ7WZ0X4T9V6K2H8M3N5PQR"


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


def door_origin() -> SessionOrigin:
    return SessionOrigin(DoorOrigin(door=Door()))


@dataclass(slots=True)
class Rig:
    config: Config
    service: SessionService
    client: httpx.AsyncClient
    tokens: dict[Principal, str]
    vmm: FakeVmm

    def head(self, principal: Principal = Principal.NOTICEBOARD) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens[principal]}"}

    async def get(self, path: str, principal: Principal = Principal.NOTICEBOARD) -> httpx.Response:
        return await self.client.get(path, headers=self.head(principal))

    async def row(self, session: str, origin: SessionOrigin | None = None) -> None:
        """One row of the host list, as attendance makes it."""
        await self.vmm.create(
            CreateSession(
                created_at=UnixMillis(CREATED_MS),
                cwd=SessionCwd(CWD),
                family=FamilyName(FAMILY),
                id=SessionId(session),
                origin=origin if origin is not None else door_origin(),
            )
        )
        self.vmm.creates.clear()

    async def replay(self, updates: list[dict[str, Any]]) -> None:
        await self.row(SESSION_ID)
        self.vmm.replays[SESSION_ID] = updates


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    config = make_config(tmp_path)
    write_status(config.state_root)
    made = write_tokens(config.state_root)
    book = TokenBook(config.state_root)
    book.load()
    vmm = FakeVmm(prompt_mode=PromptMode.HOLD)
    service = make_service(config, vmm)
    service.start()
    transport = httpx.ASGITransport(app=build_app(service, book))
    client = httpx.AsyncClient(transport=transport, base_url="http://sessiond")

    yield Rig(config, service, client, made, vmm)

    await client.aclose()
    await service.close()
    await vmm.stop()


def read_methods(rig: Rig) -> list[list[str]]:
    return [facade.methods for facade in rig.vmm.facades.values()]


async def test_the_session_list_pages_the_host_list(rig: Rig) -> None:
    """Contract 02 §7 rules 1 and 2: newest created first, no boot."""
    ids = [f"3f2a9c41-77b0-4a1e-9a4c-1d0e5f8b2c3{n}" for n in range(3)]
    dispatch = Dispatch(
        chain=DispatchChain([FamilyName("chat"), FamilyName("scrum-lead")]),
        name=FamilyName("scrum-lead"),
    )
    auto = Auto(job=AutoJobId(AUTO_JOB), trigger=Trigger(DispatchTrigger(dispatch=dispatch)))
    delegate = Delegate(delegation=DelegationId(DELEGATION), job=JobId(THIN_JOB))
    await rig.row(ids[0])
    await rig.row(ids[1], SessionOrigin(DelegateOrigin(delegate=delegate)))
    await rig.row(ids[2], SessionOrigin(AutoOrigin(auto=auto)))

    first = await rig.get(SESSIONS)
    second = await rig.get(f"{SESSIONS}?cursor={first.json()['next_cursor']}")

    assert first.status_code == HTTP_OK
    assert first.json() == {
        "sessions": [
            {
                "id": ids[2],
                "title": None,
                "created": CREATED,
                "updated": CREATED,
                "job_id": AUTO_JOB,
                "delegation": None,
                "trigger": {
                    "kind": "dispatch",
                    "name": "scrum-lead",
                    "chain": ["chat", "scrum-lead"],
                },
                "vm": "none",
            },
            {
                "id": ids[1],
                "title": None,
                "created": CREATED,
                "updated": CREATED,
                "job_id": THIN_JOB,
                "delegation": DELEGATION,
                "trigger": None,
                "vm": "none",
            },
        ],
        "next_cursor": "2",
    }
    assert [one["id"] for one in second.json()["sessions"]] == [ids[0]]
    assert second.json()["next_cursor"] is None
    assert rig.vmm.opened == []


@pytest.mark.parametrize("vm", list(VmState))
async def test_a_view_row_passes_on_the_vm_of_the_list_row(rig: Rig, vm: VmState) -> None:
    """Contract 02 §7 rule 2 minor 16: `vm` is the vmm's word as it is
    (decisions CH21-1, CH33 rule 9, CH79 rule 4). A list boots no VM."""
    await rig.row(SESSION_ID)
    rig.vmm.vms[SESSION_ID] = vm

    (listed,) = (await rig.get(SESSIONS)).json()["sessions"]

    assert listed["vm"] == vm.value
    assert rig.vmm.opened == []


async def test_a_timer_row_has_a_trigger_with_no_name(rig: Rig) -> None:
    auto = Auto(job=AutoJobId(AUTO_JOB), trigger=Trigger(TimerTrigger(timer=Timer())))
    await rig.row(SESSION_ID, SessionOrigin(AutoOrigin(auto=auto)))

    (listed,) = (await rig.get(SESSIONS)).json()["sessions"]

    assert listed["trigger"] == {"kind": "timer", "name": None}


async def test_a_transcript_holds_each_update_kind(rig: Rig) -> None:
    await rig.replay(
        [
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
    )

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
    assert rig.vmm.channels[SESSION_ID].closed


async def test_a_transcript_pages_by_position(rig: Rig) -> None:
    await rig.replay(
        [
            text("user_message_chunk", "one"),
            text("agent_message_chunk", "two"),
            text("user_message_chunk", "three"),
        ]
    )

    first = (await rig.get(f"{ONE}?limit=2")).json()
    rest = (await rig.get(f"{ONE}?start=2&limit=2")).json()

    assert [entry["position"] for entry in first["entries"]] == [0, 1]
    assert first["next"] == 2
    assert rest["entries"] == [{"position": 2, "kind": "user", "text": "three"}]
    assert rest["next"] is None


async def test_a_long_text_is_cut_at_the_cap(rig: Rig) -> None:
    await rig.replay([text("agent_message_chunk", "é" * acp_view.MAX_TEXT_BYTES)])

    entry = (await rig.get(ONE)).json()["entries"][0]

    assert len(entry["text"].encode("utf-8")) == acp_view.MAX_TEXT_BYTES


async def test_a_replay_over_the_update_cap_is_truncated(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(acp_view, "MAX_UPDATES", 2)
    updates = [text("user_message_chunk", str(n)) for n in range(5)]
    updates[1] = text("agent_message_chunk", "1")
    await rig.replay(updates)

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
    assert rig.vmm.opened == []


@pytest.mark.parametrize("cursor", ["0", "01", "x", "9007199254740992", "a b"])
async def test_a_bad_cursor_is_bad_request(rig: Rig, cursor: str) -> None:
    answer = await rig.client.get(SESSIONS, params={"cursor": cursor}, headers=rig.head())

    assert answer.status_code == HTTP_BAD_REQUEST
    assert rig.vmm.lists == []


async def test_an_unknown_session_is_not_found_with_no_boot(rig: Rig) -> None:
    answer = await rig.get(ONE)

    assert answer.status_code == HTTP_NOT_FOUND
    assert answer.json()["error"]["code"] == "not_found"
    assert rig.vmm.facades == {}


async def test_an_sbx_era_id_is_not_found_with_no_vmm_call(rig: Rig) -> None:
    """Decision CH10-9: an id that is no session id never reaches the vmm."""
    answer = await rig.get(f"{SESSIONS}/sess-1")

    assert answer.status_code == HTTP_NOT_FOUND
    assert rig.vmm.opened == []


async def test_a_session_the_facade_cannot_load_is_not_found(rig: Rig) -> None:
    await rig.row(SESSION_ID)

    answer = await rig.get(ONE)

    assert answer.status_code == HTTP_NOT_FOUND


async def test_an_unknown_family_is_not_found(rig: Rig) -> None:
    answer = await rig.get("/families/nobody/sessions")

    assert answer.status_code == HTTP_NOT_FOUND
    assert answer.json()["error"]["code"] == "family_unknown"


async def test_a_held_stream_is_busy(rig: Rig) -> None:
    """Decision CH83 rule 4: one holder of a session's stream at a time."""
    await rig.replay([])
    rig.vmm.refuse["acp"] = BUSY

    answer = await rig.get(ONE)

    assert answer.status_code == HTTP_CONFLICT
    assert answer.json()["error"]["code"] == "busy"


@pytest.mark.parametrize("code", ["host_full", "sandbox_start_failed", "unreachable"])
async def test_a_vmm_refusal_is_unavailable(rig: Rig, code: str) -> None:
    await rig.replay([])
    rig.vmm.refuse["acp"] = code
    rig.vmm.refuse["list"] = code

    for path in (SESSIONS, ONE):
        answer = await rig.get(path)

        assert answer.status_code == HTTP_UNAVAILABLE
        assert answer.json()["error"]["code"] == "sandbox_unavailable"
        assert answer.json()["error"]["detail"] == {"reason": code}


async def test_a_cwd_that_the_wire_refuses_starts_no_stream(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decision CH21: the cwd is checked before the stream opens."""
    monkeypatch.setenv(SANDBOX_MOUNT_ENV, "sessions")

    answer = await rig.get(ONE)

    assert answer.status_code == HTTP_INTERNAL
    assert answer.json()["error"]["code"] == "internal"
    assert rig.vmm.opened == []


@pytest.mark.parametrize("path", [SESSIONS, ONE, "/busy"])
@pytest.mark.parametrize("principal", [Principal.CHAPERONE_DISPATCH, Principal.DOOR_ACP])
async def test_each_other_principal_is_forbidden(rig: Rig, path: str, principal: Principal) -> None:
    answer = await rig.get(path, principal)

    assert answer.status_code == HTTP_FORBIDDEN
    assert rig.vmm.opened == []
    assert rig.vmm.lists == []


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
        accepts_dispatch=True,
        max_running_turns=1,
    )
    body = {
        "caller_family": FAMILY,
        "target_family": JOB_FAMILY,
        "delegation_id": "01JBQ7WZ0X4T9V6K2H8M3N5PQR",
        "message": "Work the ticket.",
        "chain": [FAMILY, JOB_FAMILY],
    }

    for _ in range(2):
        sent = await rig.client.post(
            "/dispatch", json=body, headers=rig.head(Principal.CHAPERONE_DISPATCH)
        )
        assert sent.status_code == HTTP_OK

    facades = rig.vmm.facades
    await wait_until(lambda: sum(len(one.held) for one in facades.values()) == 1)
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
        one.sent(record('{"jsonrpc":"2.0","id":"p","method":"session/prompt","params":{}}'))
        two.sent(record('{"jsonrpc":"2.0","id":4,"method":"session/prompt","params":{}}'))
        two.sent(record('{"jsonrpc":"2.0","id":5,"method":"session/list","params":{}}'))
        assert prompts.in_flight == 2

        two.answered(record('{"jsonrpc":"2.0","id":"4","result":{"stopReason":"end_turn"}}'))
        two.answered(record('{"jsonrpc":"2.0","id":4,"result":{"stopReason":"end_turn"}}'))
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
        one.sent(record(prompt(sent)))
        assert prompts.in_flight == 1

        one.answered(record(answer(answered)))
        assert prompts.in_flight == 0


def test_the_door_keeps_a_string_id_apart_from_a_number_id() -> None:
    prompts = DoorPrompts()

    with prompts.connection() as one:
        one.sent(record(prompt('"1"')))
        one.sent(record(prompt("1")))
        assert prompts.in_flight == 2

        one.answered(record(answer("1.0")))
        assert prompts.in_flight == 1

        one.answered(record(answer('"1"')))
        assert prompts.in_flight == 0


def record(text: str) -> dict[str, Any]:
    """One client line as the door reads it."""
    parsed = loads_strict(text)
    assert isinstance(parsed, dict)
    return parsed  # pyright: ignore[reportUnknownVariableType]
