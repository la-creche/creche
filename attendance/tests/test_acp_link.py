"""The ACP link to the facade of one session, over its vmm ACP stream."""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

import pytest
from attendance.acp_link import (
    ACP_SCHEMA_SHA256,
    ACP_SDK_VERSION,
    HANDSHAKE_ID,
    METHOD_CANCEL,
    METHOD_PROMPT,
    SCHEMA_FILE,
    AcpCallFailed,
    AcpHandshakeError,
    AcpLink,
    AcpLinkLost,
    AgentAnswer,
    LinkState,
    PromptEnd,
    StopReason,
    cancel_notice,
    handshake,
    initialize_request,
    prompt_request,
)
from attendance.channel import ChannelClosed
from attendance_harness import FAMILY
from fake_channel import FakeChannel
from fake_facade import ANSWER, ERROR_CODE, FakeFacade, PromptMode, chunk, serve

PINNED_SDK = "2.0.2"
PINNED_SHA256 = "4bcd1d2051ff7ed379bd9b234fb9b09c5ab9245478698e9fab805bd9b52a33a6"
SESSION = "78c1103d-6e82-4492-a598-bfb2e1e4eff1"
OTHER_SESSION = "9a2f7b44-0c1d-4e6f-8a3b-5d7e9f1a2b3c"
SMALL_CAP = 256

READY_RESULT: dict[str, Any] = {
    "protocolVersion": 1,
    "agentCapabilities": {"loadSession": True, "sessionCapabilities": {"list": {}}},
}


async def answer(channel: FakeChannel, result: dict[str, Any]) -> dict[str, Any]:
    """Read the next host line and answer it with `result`."""
    request = json.loads(await channel.next_host_line())
    await channel.feed(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}))
    return request


async def open_link(channel: FakeChannel) -> AcpLink:
    link = AcpLink(FAMILY, SESSION, channel)
    start = asyncio.create_task(link.start())
    await answer(channel, READY_RESULT)
    await start
    return link


SERVED: set[asyncio.Task[None]] = set()


async def served_link(mode: PromptMode = PromptMode.OK) -> tuple[AcpLink, FakeFacade]:
    link, facade, _ = await served(mode)
    return link, facade


async def served(mode: PromptMode) -> tuple[AcpLink, FakeFacade, FakeChannel]:
    channel = FakeChannel()
    facade = FakeFacade(prompt_mode=mode)
    task = asyncio.get_running_loop().create_task(serve(channel, facade))
    SERVED.add(task)
    task.add_done_callback(SERVED.discard)
    link = AcpLink(FAMILY, SESSION, channel)
    await link.start()
    return link, facade, channel


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


def test_the_client_calls_fit_the_schema() -> None:
    defs = json.loads(SCHEMA_FILE.read_text())["$defs"]
    calls = {
        "PromptRequest": prompt_request(2, SESSION, "hello"),
        "CancelNotification": cancel_notice(SESSION),
    }

    for name, call in calls.items():
        schema = defs[name]
        params = call["params"]

        assert set(schema["required"]) <= set(params), name
        assert set(params) <= set(schema["properties"]), name

    assert calls["PromptRequest"]["method"] == METHOD_PROMPT
    assert calls["PromptRequest"]["params"]["prompt"] == [{"type": "text", "text": "hello"}]
    assert "_meta" not in calls["PromptRequest"]["params"]
    assert calls["CancelNotification"]["method"] == METHOD_CANCEL
    assert "id" not in calls["CancelNotification"]
    assert {one["const"] for one in defs["StopReason"]["oneOf"]} == {
        reason.value for reason in StopReason
    }


async def test_an_answered_initialize_reads_ready() -> None:
    channel = FakeChannel()
    link = AcpLink(FAMILY, SESSION, channel)
    start = asyncio.create_task(link.start())
    request = await answer(channel, READY_RESULT)

    assert await start == AgentAnswer(protocol_version=1, load_session=True, session_list=True)
    assert request["id"] == HANDSHAKE_ID
    assert link.state is LinkState.READY
    await link.close()
    assert channel.closed


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
async def test_a_wrong_answer_fails_the_handshake(result: dict[str, Any]) -> None:
    channel = FakeChannel()
    start = asyncio.create_task(handshake(channel))
    await answer(channel, result)

    with pytest.raises(AcpHandshakeError):
        await start


@pytest.mark.parametrize(
    "line",
    ["not json", '{"jsonrpc":"2.0","id":5,"result":{}}', '{"jsonrpc":"2.0","method":"x"}'],
    ids=["not_json", "another_id", "a_notice"],
)
async def test_a_bad_first_line_fails_the_handshake(line: str) -> None:
    channel = FakeChannel()
    start = asyncio.create_task(handshake(channel))
    await channel.next_host_line()
    await channel.feed(line)

    with pytest.raises(AcpHandshakeError):
        await start


async def test_a_stream_that_ends_first_fails_the_handshake() -> None:
    channel = FakeChannel()
    start = asyncio.create_task(handshake(channel))
    await channel.next_host_line()
    await channel.drop()

    with pytest.raises(ChannelClosed):
        await start


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
    "other_session": chunk(OTHER_SESSION, "x"),
    "extra_member": '{"jsonrpc":"2.0","id":1,"result":{},"x":1}',
    "no_stop_reason": '{"jsonrpc":"2.0","id":1,"result":{}}',
}


@pytest.mark.parametrize("line", BAD_LINES.values(), ids=BAD_LINES.keys())
async def test_a_bad_line_loses_the_link_and_fails_the_prompt(line: str) -> None:
    channel = FakeChannel()
    link = await open_link(channel)
    asked = asyncio.create_task(link.prompt("hello"))
    await channel.next_host_line()
    await channel.feed(line)

    with pytest.raises(AcpLinkLost):
        await asked

    assert link.state is LinkState.LOST
    await link.close()


async def test_a_line_over_the_cap_loses_the_link() -> None:
    channel = FakeChannel(max_line_bytes=SMALL_CAP)
    link = await open_link(channel)
    await channel.feed(json.dumps({"jsonrpc": "2.0", "pad": "x" * SMALL_CAP}))

    await until(lambda: link.state is LinkState.LOST)
    await link.close()


async def test_a_prompt_collects_the_chunks_until_end_turn() -> None:
    link, facade = await served_link()

    end = await link.prompt("When was the boiler serviced?")

    assert end == PromptEnd(stop_reason=StopReason.END_TURN, text=ANSWER)
    assert facade.methods == ["initialize", METHOD_PROMPT]
    assert facade.received[-1]["params"]["sessionId"] == SESSION
    await link.close()


async def test_a_json_rpc_error_raises_call_failed() -> None:
    link, _ = await served_link(PromptMode.ERROR)

    with pytest.raises(AcpCallFailed) as failed:
        await link.prompt("hello")

    assert failed.value.code == ERROR_CODE
    await link.close()


async def test_a_link_lost_mid_prompt_raises_link_lost() -> None:
    link, _ = await served_link(PromptMode.LOSE)

    with pytest.raises(AcpLinkLost):
        await link.prompt("hello")

    assert link.state is LinkState.LOST
    await link.close()


async def test_cancel_ends_a_held_prompt_with_cancelled() -> None:
    link, facade = await served_link(PromptMode.HANG)
    asked = asyncio.create_task(link.prompt("hello"))
    await until(lambda: SESSION in facade.held)

    await link.cancel()

    assert (await asked).stop_reason is StopReason.CANCELLED
    assert facade.methods[-1] == METHOD_CANCEL
    await link.close()


async def test_a_late_answer_to_an_abandoned_prompt_is_dropped() -> None:
    link, facade, channel = await served(PromptMode.HOLD)
    asked = asyncio.create_task(link.prompt("hello"))
    await until(lambda: SESSION in facade.held)
    asked.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asked

    held = facade.held.pop(SESSION)
    late = {"jsonrpc": "2.0", "id": held.request_id, "result": {"stopReason": "end_turn"}}
    await channel.feed(json.dumps(late))
    facade.prompt_mode = PromptMode.OK
    end = await link.prompt("again")

    assert link.state is LinkState.READY
    assert end.stop_reason is StopReason.END_TURN
    await link.close()


async def test_a_closed_link_refuses_a_prompt() -> None:
    link, _ = await served_link()
    await link.close()

    with pytest.raises(AcpLinkLost):
        await link.prompt("hello")
