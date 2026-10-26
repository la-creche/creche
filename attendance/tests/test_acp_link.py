"""The ACP link to one sandbox's facade: handshake, beat, shutdown, refusals."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from attendance.acp_link import (
    ACP_SCHEMA_SHA256,
    ACP_SDK_VERSION,
    BEAT_INTERVAL_S,
    EXIT_WAIT_S,
    FACADE_COMMAND,
    METHOD_BEAT,
    METHOD_CANCEL,
    METHOD_NEW_SESSION,
    METHOD_PROMPT,
    METHOD_SHUTDOWN,
    SCHEMA_FILE,
    AcpCallFailed,
    AcpHandshakeError,
    AcpLink,
    AcpLinkLost,
    AgentAnswer,
    LinkState,
    OrphanFacade,
    PromptEnd,
    StopReason,
    cancel_notice,
    initialize_request,
    new_session_request,
    prompt_request,
)
from attendance.atomic import write_json
from attendance.channel import Channel, FakeChannel, SandboxDial
from attendance.exec_channel import ExecChannel
from attendance.faults import FaultCode, FaultReporter
from attendance.paths import playpen_lock_file
from attendance.playpen_link import EXIT_WAIT_S as PLAYPEN_EXIT_WAIT_S
from attendance.wire import PING_INTERVAL_S
from attendance_harness import FAMILY, SANDBOX
from fake_facade import ANSWER, ERROR_CODE, FacadeFleet, PromptMode

PINNED_SDK = "2.0.2"
PINNED_SHA256 = "4bcd1d2051ff7ed379bd9b234fb9b09c5ab9245478698e9fab805bd9b52a33a6"
OTHER_SANDBOX = "chat-s2"
SMALL_CAP = 256
SHORT_S = 0.05
REPO = Path(__file__).resolve().parents[2]
FAKE_SBX = REPO / "integration" / "tests" / "fake_sbx.py"
FAKE_FACADE = Path(__file__).resolve().parent / "fake_facade.py"

READY_RESULT: dict[str, Any] = {
    "protocolVersion": 1,
    "agentCapabilities": {"loadSession": True, "sessionCapabilities": {"list": {}}},
}


class Fleet:
    """One `FakeChannel` per sandbox, and every channel loss in order."""

    def __init__(self, state_root: Path, max_line_bytes: int = 1_048_576) -> None:
        self.channels: dict[str, FakeChannel] = {}
        self.lost: list[str] = []
        self.faults = FaultReporter(state_root)
        self._max_line_bytes = max_line_bytes

    def factory(self, dial: SandboxDial) -> Channel:
        channel = FakeChannel(dial.sandbox, self._max_line_bytes)
        self.channels[dial.sandbox] = channel
        return channel

    async def on_lost(self, sandbox: str) -> None:
        self.lost.append(sandbox)


def make_link(tmp_path: Path, fleet: Fleet, **timing: float) -> AcpLink:
    return AcpLink(FAMILY, fleet.factory, fleet.faults, tmp_path, fleet.on_lost, **timing)


async def answer(channel: FakeChannel, result: dict[str, Any]) -> dict[str, Any]:
    """Read the next host line and answer it with `result`."""
    request = json.loads(await channel.next_host_line())
    await channel.feed(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}))
    return request


async def open_link(
    tmp_path: Path, fleet: Fleet, sandbox: str = SANDBOX, **timing: float
) -> AcpLink:
    timing.setdefault("exit_wait_s", SHORT_S)
    link = make_link(tmp_path, fleet, **timing)
    start = asyncio.create_task(link.start(SandboxDial(sandbox, "/env")))
    await asyncio.sleep(0)
    await answer(fleet.channels[sandbox], READY_RESULT)
    await start
    return link


async def until(condition: Any, timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


def test_the_vendored_schema_is_the_pinned_sdk_file() -> None:
    digest = hashlib.sha256(SCHEMA_FILE.read_bytes()).hexdigest()

    assert ACP_SDK_VERSION == PINNED_SDK
    assert ACP_SCHEMA_SHA256 == PINNED_SHA256
    assert digest == PINNED_SHA256


def test_the_initialize_request_fits_the_schema() -> None:
    defs = json.loads(SCHEMA_FILE.read_text())["$defs"]
    params = initialize_request(0)["params"]
    request = defs["InitializeRequest"]
    agent = defs["AgentCapabilities"]["properties"]

    assert set(request["required"]) <= set(params)
    assert set(params) <= set(request["properties"])
    assert set(params["clientCapabilities"]) <= set(defs["ClientCapabilities"]["properties"])
    assert params["clientCapabilities"]["terminal"] is False
    assert set(params["clientCapabilities"]["fs"].values()) == {False}
    assert {"loadSession", "sessionCapabilities"} <= set(agent)
    assert "list" in defs["SessionCapabilities"]["properties"]


async def test_an_answered_initialize_reads_ready(tmp_path: Path) -> None:
    fleet = Fleet(tmp_path)
    link = await open_link(tmp_path, fleet)

    assert link.status.state is LinkState.READY
    assert link.status.sandbox == SANDBOX
    assert link.status.agent == AgentAnswer(
        protocol_version=1, load_session=True, session_list=True
    )
    await link.stop()


@pytest.mark.parametrize(
    "result",
    [
        {**READY_RESULT, "protocolVersion": 2},
        {**READY_RESULT, "protocolVersion": True},
        {**READY_RESULT, "agentCapabilities": {"loadSession": False}},
        {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}},
    ],
    ids=["protocol_2", "protocol_true", "no_load", "no_list"],
)
async def test_a_wrong_answer_fails_the_handshake(tmp_path: Path, result: dict[str, Any]) -> None:
    fleet = Fleet(tmp_path)
    link = make_link(tmp_path, fleet)
    start = asyncio.create_task(link.start(SandboxDial(SANDBOX, "/env")))
    await asyncio.sleep(0)
    await answer(fleet.channels[SANDBOX], result)

    with pytest.raises(AcpHandshakeError):
        await start

    assert link.status.state is LinkState.CHANNEL_LOST
    assert [fault.code for fault in fleet.faults.open_faults(FAMILY)] == [
        FaultCode.PROTOCOL_MISMATCH
    ]


BAD_LINES = {
    "not_json": "not json",
    "nan": '{"jsonrpc":"2.0","id":1,"result":NaN}',
    "duplicate_key": '{"jsonrpc":"2.0","id":1,"id":1,"result":{}}',
    "not_an_object": "[]",
    "wrong_version": '{"jsonrpc":"1.0","id":1,"result":{}}',
    "unknown_id": '{"jsonrpc":"2.0","id":99,"result":{}}',
    "bool_id": '{"jsonrpc":"2.0","id":true,"result":{}}',
    "result_and_error": '{"jsonrpc":"2.0","id":1,"result":{},"error":{"code":1,"message":"x"}}',
    "bad_error": '{"jsonrpc":"2.0","id":1,"error":{"code":"x","message":"x"}}',
    "agent_request": '{"jsonrpc":"2.0","id":5,"method":"fs/read_text_file","params":{}}',
    "agent_notice": '{"jsonrpc":"2.0","method":"session/update","params":{}}',
    "extra_member": '{"jsonrpc":"2.0","id":1,"result":{},"x":1}',
}


@pytest.mark.parametrize("line", BAD_LINES.values(), ids=BAD_LINES.keys())
async def test_a_bad_line_ends_in_channel_lost_and_the_service_keeps_serving(
    tmp_path: Path, line: str
) -> None:
    fleet = Fleet(tmp_path)
    broken = await open_link(tmp_path, fleet, SANDBOX)
    other = await open_link(tmp_path, fleet, OTHER_SANDBOX, beat_interval_s=SHORT_S)

    await fleet.channels[SANDBOX].feed(line)
    await until(lambda: fleet.lost != [])
    await answer(fleet.channels[OTHER_SANDBOX], {})

    assert fleet.lost == [SANDBOX]
    assert broken.status.state is LinkState.CHANNEL_LOST
    assert [fault.code for fault in fleet.faults.open_faults(FAMILY)] == [
        FaultCode.PROTOCOL_VIOLATION
    ]
    assert other.status.state is LinkState.READY
    await other.stop()


async def test_a_line_over_the_cap_ends_in_channel_lost(tmp_path: Path) -> None:
    fleet = Fleet(tmp_path, max_line_bytes=SMALL_CAP)
    link = await open_link(tmp_path, fleet)

    await fleet.channels[SANDBOX].feed(json.dumps({"pad": "x" * SMALL_CAP}))
    await until(lambda: fleet.lost != [])

    assert fleet.lost == [SANDBOX]
    assert link.status.state is LinkState.CHANNEL_LOST


async def test_beats_keep_the_playpen_cadence(tmp_path: Path) -> None:
    fleet = Fleet(tmp_path)
    link = await open_link(tmp_path, fleet, beat_interval_s=SHORT_S)
    channel = fleet.channels[SANDBOX]

    first = await answer(channel, {})
    second = await answer(channel, {})

    assert BEAT_INTERVAL_S == PING_INTERVAL_S
    assert first["method"] == second["method"] == METHOD_BEAT
    assert second["id"] > first["id"]

    await until(lambda: fleet.lost != [])
    assert fleet.lost == [SANDBOX]
    assert link.status.state is LinkState.CHANNEL_LOST


async def test_stop_sends_shutdown_then_waits_the_grace(tmp_path: Path) -> None:
    fleet = Fleet(tmp_path)
    link = await open_link(tmp_path, fleet, exit_wait_s=SHORT_S * 4)
    begun = time.monotonic()

    await link.stop()
    shutdown = json.loads(await fleet.channels[SANDBOX].next_host_line())

    assert EXIT_WAIT_S == PLAYPEN_EXIT_WAIT_S
    assert shutdown == {"jsonrpc": "2.0", "method": METHOD_SHUTDOWN, "params": {}}
    assert time.monotonic() - begun >= SHORT_S * 4
    assert link.status.state is LinkState.STOPPED
    assert fleet.lost == []


async def test_a_live_lock_refuses_the_dial(tmp_path: Path) -> None:
    fleet = Fleet(tmp_path)
    lock = playpen_lock_file(tmp_path, FAMILY, SANDBOX)
    link = make_link(tmp_path, fleet, lock_stale_s=1.0, lock_poll_s=0.01, host_deadline_s=0.2)

    async def beat() -> None:
        for count in range(100):
            write_json(lock, {"beat": count})
            await asyncio.sleep(0.01)

    write_json(lock, {"beat": 0})
    beating = asyncio.create_task(beat())

    with pytest.raises(OrphanFacade):
        await link.start(SandboxDial(SANDBOX, "/env"))

    beating.cancel()
    assert fleet.channels == {}


def facade_dial(tmp_path: Path, mode: str) -> tuple[SandboxDial, Path]:
    record = tmp_path / "methods.txt"
    env_file = tmp_path / "facade.env"
    env_file.write_text(f"FAKE_FACADE_MODE={mode}\nFAKE_FACADE_RECORD={record}\n")
    return SandboxDial(SANDBOX, str(env_file)), record


def exec_factory(dial: SandboxDial) -> Channel:
    """`FACADE_COMMAND`, with `fake_sbx.py` for `sbx` and the fake for the facade."""
    command = FACADE_COMMAND.replace("sbx", f"{sys.executable} -I {FAKE_SBX}", 1)
    command = command.replace("creche-facade", f"{sys.executable} -I {FAKE_FACADE}")
    return ExecChannel(dial, command)


@pytest.mark.slow
async def test_the_facade_starts_through_sbx_and_stops(tmp_path: Path) -> None:
    dial, record = facade_dial(tmp_path, "serve")
    link = AcpLink(FAMILY, exec_factory, FaultReporter(tmp_path), tmp_path, Fleet(tmp_path).on_lost)

    await link.start(dial)
    assert link.status.state is LinkState.READY
    await link.stop()

    assert record.read_text().split() == ["initialize", METHOD_SHUTDOWN]


@pytest.mark.slow
async def test_a_facade_that_writes_garbage_is_lost(tmp_path: Path) -> None:
    dial, _ = facade_dial(tmp_path, "garbage")
    fleet = Fleet(tmp_path)
    link = AcpLink(FAMILY, exec_factory, FaultReporter(tmp_path), tmp_path, fleet.on_lost)

    await link.start(dial)
    await until(lambda: fleet.lost != [], 5.0)

    assert fleet.lost == [SANDBOX]


# ------------------------------------------------------- client calls


async def served_link(
    tmp_path: Path, mode: PromptMode = PromptMode.OK
) -> tuple[AcpLink, FacadeFleet, Fleet]:
    """A link whose far side is `FakeFacade`, in this process."""
    facades = FacadeFleet(prompt_mode=mode)
    fleet = Fleet(tmp_path)
    link = AcpLink(
        FAMILY, facades.factory, fleet.faults, tmp_path, fleet.on_lost, exit_wait_s=SHORT_S
    )
    await link.start(SandboxDial(SANDBOX, "/env"))
    return link, facades, fleet


def test_the_client_calls_fit_the_schema() -> None:
    defs = json.loads(SCHEMA_FILE.read_text())["$defs"]
    calls = {
        "NewSessionRequest": new_session_request(1, "/srv/work", {"creche/job": "job-1"}),
        "PromptRequest": prompt_request(2, "sess-1", "hello", {"creche/trigger": {}}),
        "CancelNotification": cancel_notice("sess-1"),
    }

    for name, call in calls.items():
        schema = defs[name]
        params = call["params"]

        assert set(schema["required"]) <= set(params), name
        assert set(params) <= set(schema["properties"]), name

    assert calls["NewSessionRequest"]["method"] == METHOD_NEW_SESSION
    assert calls["PromptRequest"]["method"] == METHOD_PROMPT
    assert calls["PromptRequest"]["params"]["prompt"] == [{"type": "text", "text": "hello"}]
    assert "id" not in calls["CancelNotification"]
    assert {one["const"] for one in defs["StopReason"]["oneOf"]} == {
        reason.value for reason in StopReason
    }


async def test_a_prompt_collects_the_chunks_until_end_turn(tmp_path: Path) -> None:
    link, facades, _ = await served_link(tmp_path)

    session = await link.new_session("/srv/work", {"creche/job": "job-1"})
    end = await link.prompt(session, "When was the boiler serviced?", None)

    assert end == PromptEnd(stop_reason=StopReason.END_TURN, text=ANSWER)
    assert facades.facades[SANDBOX].methods == [
        "initialize",
        METHOD_NEW_SESSION,
        METHOD_PROMPT,
    ]
    await link.stop()
    await facades.stop()


async def test_a_json_rpc_error_raises_call_failed(tmp_path: Path) -> None:
    link, facades, fleet = await served_link(tmp_path, PromptMode.ERROR)
    session = await link.new_session("/srv/work", None)

    with pytest.raises(AcpCallFailed) as refused:
        await link.prompt(session, "hello", None)

    assert refused.value.code == ERROR_CODE
    assert link.status.state is LinkState.READY
    assert fleet.lost == []
    await link.stop()
    await facades.stop()


async def test_a_link_lost_mid_prompt_raises_link_lost(tmp_path: Path) -> None:
    link, facades, fleet = await served_link(tmp_path, PromptMode.LOSE)
    session = await link.new_session("/srv/work", None)

    with pytest.raises(AcpLinkLost):
        await link.prompt(session, "hello", None)

    await until(lambda: fleet.lost != [])
    assert link.status.state is LinkState.CHANNEL_LOST
    await facades.stop()


async def test_cancel_ends_a_held_prompt_with_cancelled(tmp_path: Path) -> None:
    link, facades, _ = await served_link(tmp_path, PromptMode.HANG)
    session = await link.new_session("/srv/work", None)
    prompt = asyncio.create_task(link.prompt(session, "hello", None))
    await until(lambda: session in facades.facades[SANDBOX].held)

    await link.cancel(session)

    assert (await prompt).stop_reason is StopReason.CANCELLED
    assert facades.facades[SANDBOX].methods[-1] == METHOD_CANCEL
    await link.stop()
    await facades.stop()


async def test_a_late_answer_to_an_abandoned_prompt_is_dropped(tmp_path: Path) -> None:
    link, facades, fleet = await served_link(tmp_path, PromptMode.HOLD)
    session = await link.new_session("/srv/work", None)
    prompt = asyncio.create_task(link.prompt(session, "hello", None))
    await until(lambda: session in facades.facades[SANDBOX].held)

    prompt.cancel()
    await asyncio.gather(prompt, return_exceptions=True)
    await facades.settle(SANDBOX, session)
    again = await link.new_session("/srv/work", None)

    assert again != session
    assert fleet.lost == []
    assert link.status.state is LinkState.READY
    await link.stop()
    await facades.stop()


async def test_an_update_for_a_session_of_no_call_ends_the_link(tmp_path: Path) -> None:
    link, facades, fleet = await served_link(tmp_path)
    update = {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "x"}}
    notice = {"jsonrpc": "2.0", "method": "session/update", "params": {}}
    notice["params"] = {"sessionId": "sess-99", "update": update}

    await facades.channels[SANDBOX].feed(json.dumps(notice))
    await until(lambda: fleet.lost != [])

    assert [fault.code for fault in fleet.faults.open_faults(FAMILY)] == [
        FaultCode.PROTOCOL_VIOLATION
    ]
    await facades.stop()


@pytest.mark.parametrize(
    "result",
    [{}, {"sessionId": 7}, {"sessionId": ""}],
    ids=["no_id", "number", "empty"],
)
async def test_a_new_session_answer_without_an_id_ends_the_link(
    tmp_path: Path, result: dict[str, Any]
) -> None:
    fleet = Fleet(tmp_path)
    link = await open_link(tmp_path, fleet)
    call = asyncio.create_task(link.new_session("/srv/work", None))

    await answer(fleet.channels[SANDBOX], result)

    with pytest.raises(AcpLinkLost):
        await call

    await until(lambda: fleet.lost != [])
    assert fleet.lost == [SANDBOX]
