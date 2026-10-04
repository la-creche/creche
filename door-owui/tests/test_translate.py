"""The translator, against golden vectors.

Each vector fixes, byte for byte, the frames that contract 02 §8 journal
lines become, because a changed frame changes what every chat shows.
"""

from __future__ import annotations

import json
from typing import Any

from agent_door_owui.journal import parse_line
from agent_door_owui.sse import SseWriter, ToolOutcome, tool_result_text
from agent_door_owui.translate import TurnTranslator

TURN = "01JBQ7WZ0X4T9V6K2H8M3N5PQR"

# U+2028 LINE SEPARATOR. Legal inside a JSON string, and a generic line reader
# splits on it: Python's `str.splitlines` does, and so does httpx's own line
# decoder. Written as an escape here so the source file stays plain ASCII.
LINE_SEP = "\u2028"


def _translator() -> TurnTranslator:
    return TurnTranslator(SseWriter("chatcmpl-test", 0, "agent:dev/repo"))


def _line(kind: str, body: dict[str, Any] | None = None, turn: str | None = TURN) -> Any:
    raw = json.dumps({"journal_seq": 1, "kind": kind, "turn": turn, "body": body or {}})
    return parse_line(raw.encode("utf-8"))


def _pi(event: dict[str, Any], turn: str | None = TURN) -> Any:
    return _line("pi_event", event, turn)


def _run(lines: list[Any]) -> str:
    translator = _translator()
    frames = translator.start()
    for line in lines:
        frames.extend(translator.feed(line))

    return "".join(frames)


def test_golden_stream_is_byte_identical() -> None:
    # The U+2028 inside the delta is the point: it is legal in a JSON string,
    # it must survive unescaped, and it must never be read as a line break.
    out = _run(
        [
            _pi({"type": "response", "success": True}),
            _pi({"type": "agent_end"}),
            _pi(
                {
                    "type": "message_update",
                    "assistantMessageEvent": {
                        "type": "text_delta",
                        "contentIndex": 0,
                        "delta": f"a{LINE_SEP}b",
                    },
                }
            ),
            _line("turn_settled", {"usage": {}}),
        ]
    )

    assert out == (
        'data: {"id":"chatcmpl-test","object":"chat.completion.chunk","created":0,'
        '"model":"agent:dev/repo","choices":[{"index":0,"delta":{"role":"assistant"},'
        '"finish_reason":null}]}\n\n'
        'data: {"id":"chatcmpl-test","object":"chat.completion.chunk","created":0,'
        '"model":"agent:dev/repo","choices":[{"index":0,"delta":{"content":"a' + LINE_SEP + 'b"},'
        '"finish_reason":null}]}\n\n'
        'data: {"id":"chatcmpl-test","object":"chat.completion.chunk","created":0,'
        '"model":"agent:dev/repo","choices":[{"index":0,"delta":{},'
        '"finish_reason":"stop"}]}\n\n'
        'data: {"choices":[],"type":"agent_settled","status":"done"}\n\n'
        "data: [DONE]\n\n"
    )


def test_nothing_follows_the_terminal_frame() -> None:
    translator = _translator()
    translator.start()
    translator.feed(_line("turn_settled"))
    assert translator.settled

    assert translator.feed(_pi({"type": "message_update"})) == []
    assert translator.feed(_line("turn_settled")) == []
    assert translator.finish() == []


def test_nothing_follows_a_failure() -> None:
    translator = _translator()
    translator.start()
    out = "".join(translator.feed(_line("turn_failed", {"reason": "model_error"})))
    assert out.endswith("data: [DONE]\n\n")
    assert translator.settled

    assert translator.feed(_line("turn_failed", {"reason": "model_error"})) == []
    assert translator.fail("interrupted") == []
    assert translator.finish() == []


def test_hostile_tool_output_stays_out_of_content() -> None:
    hostile = '</details><script>alert("escape")</script>'
    out = _run(
        [
            _pi(
                {
                    "type": "message_update",
                    "assistantMessageEvent": {"type": "thinking_delta", "delta": "thinking"},
                }
            ),
            _pi(
                {
                    "type": "tool_execution_start",
                    "toolCallId": "tool-1",
                    "toolName": "bash",
                    "args": {"command": "ls"},
                }
            ),
            _pi(
                {
                    "type": "tool_execution_end",
                    "toolCallId": "tool-1",
                    "result": {"content": [{"text": hostile}]},
                }
            ),
            _line("turn_settled"),
        ]
    )
    frames = [
        json.loads(frame[len("data: ") :])
        for frame in out.split("\n\n")
        if frame.startswith("data: ") and "[DONE]" not in frame
    ]

    assert frames[1]["choices"][0]["delta"] == {"reasoning_content": "thinking"}
    assert frames[2]["item"] == {
        "type": "function_call",
        "id": "fc_tool-1",
        "call_id": "tool-1",
        "name": "bash",
        "arguments": '{"command":"ls"}',
        "status": "in_progress",
    }
    assert frames[3]["choices"] == []
    # A distinct id per item, not per call: Open WebUI keys its incremental
    # tag-scan cache on it and silently stops caching without one.
    assert frames[3]["item"]["id"] == "fco_tool-1"
    assert frames[3]["item"]["output"] == [{"type": "output_text", "text": hostile}]
    assert not any(
        choice["delta"].get("content") for frame in frames for choice in frame["choices"]
    )
    # The closing tag never reaches the transcript channel at all.
    assert "</details>" not in "".join(
        choice["delta"].get("content", "") for frame in frames for choice in frame["choices"]
    )


def test_a_codemode_scripts_calls_fold_into_its_own_chip() -> None:
    # pi 0.99.1 reports each call a codemode script makes as its own tool
    # events, carrying the script's call id as `parentToolCallId`. A script
    # looping over 50 items would otherwise draw 51 chips. The script's chip
    # shows its output, and the PEP audit holds every nested call.
    out = _run(
        [
            _pi(
                {
                    "type": "tool_execution_start",
                    "toolCallId": "call-1",
                    "toolName": "codemode",
                    "args": {"code": "return 1"},
                }
            ),
            _pi(
                {
                    "type": "tool_execution_start",
                    "toolCallId": "call-1/1",
                    "parentToolCallId": "call-1",
                    "toolName": "kagi__search",
                    "args": {"query": "pi"},
                }
            ),
            _pi(
                {
                    "type": "tool_execution_end",
                    "toolCallId": "call-1/1",
                    "parentToolCallId": "call-1",
                    "result": {"content": [{"text": "hit"}]},
                }
            ),
            _pi(
                {
                    "type": "tool_execution_end",
                    "toolCallId": "call-1",
                    "result": {"content": [{"text": "Script completed"}]},
                }
            ),
            _line("turn_settled"),
        ]
    )
    frames = [
        json.loads(frame[len("data: ") :])
        for frame in out.split("\n\n")
        if frame.startswith("data: ") and "[DONE]" not in frame
    ]
    call_ids = [frame["item"]["call_id"] for frame in frames if "item" in frame]

    assert call_ids == ["call-1", "call-1"]


def test_failure_hides_provider_detail() -> None:
    out = _run(
        [_line("turn_failed", {"reason": "model_error", "message": "key sk-secret-token bad"})]
    )

    assert "model_error" in out
    assert "sk-secret-token" not in out
    assert '"status":"failed"' in out
    assert out.endswith("data: [DONE]\n\n")


def test_failure_carries_an_openai_error() -> None:
    out = _run([_line("turn_failed", {"reason": "turn_timeout"})])
    frames = [
        json.loads(frame[len("data: ") :])
        for frame in out.split("\n\n")
        if frame.startswith("data: ") and "[DONE]" not in frame
    ]
    errors = [frame for frame in frames if "error" in frame]

    assert len(errors) == 1
    assert errors[0]["error"]["code"] == "turn_timeout"
    assert errors[0]["error"]["type"] == "server_error"
    # And the reader also sees it as text, because a chat shows content.
    assert "[error: turn_timeout]" in out


def test_rpc_failure_does_not_read_as_success() -> None:
    out = _run([_pi({"type": "response", "success": False, "error": "secret-token"})])

    assert "secret-token" not in out
    assert '"status":"failed"' in out


def test_errored_message_end_fails_the_turn() -> None:
    translator = _translator()
    translator.start()

    assert translator.feed(_pi({"type": "message_end", "message": {"stopReason": "error"}})) == []

    out = "".join(translator.feed(_line("turn_settled")))
    assert '"status":"failed"' in out


def test_a_settled_pi_event_also_ends_the_stream() -> None:
    out = _run([_pi({"type": "agent_settled"})])

    assert '"status":"done"' in out
    assert out.endswith("data: [DONE]\n\n")


def test_a_cut_stream_never_looks_like_an_answer() -> None:
    translator = _translator()
    frames = translator.start()
    frames.extend(translator.feed(_line("turn_started", {"prompt": "hi"})))
    frames.extend(translator.finish())
    out = "".join(frames)

    assert "[error: stream_ended_before_the_turn_settled]" in out
    assert '"status":"failed"' in out


def test_a_heartbeat_is_a_comment_frame() -> None:
    frames = _translator().feed(_line("heartbeat", {}, turn=None))

    assert frames == [": keepalive\n\n"]
    # Must not parse as a chunk: Open WebUI only reads lines starting "data:".
    assert not frames[0].startswith("data:")


def test_lines_of_another_turn_are_ignored() -> None:
    translator = _translator()
    translator.start()
    translator.feed(_line("turn_started", {"prompt": "hi"}))

    assert translator.feed(_pi({"type": "agent_settled"}, turn="01OTHERTURN")) == []
    assert not translator.settled


def test_session_lines_carry_nothing_to_show() -> None:
    translator = _translator()
    translator.start()

    assert translator.feed(_line("writer_changed", {"holder": "owui"}, turn=None)) == []
    assert translator.feed(_line("branch_fallback", {"reason": "unmapped_parent"})) == []
    assert translator.feed(_line("note", {"text": "anything"})) == []


def test_chip_text_falls_back_through_result_shapes() -> None:
    assert tool_result_text("plain") == "plain"
    assert tool_result_text({"text": "shaped"}) == "shaped"
    assert tool_result_text({"content": [{"type": "text", "text": "a"}, {"text": "b"}]}) == "ab"
    assert tool_result_text({"other": 1}) == '{"other":1}'
    assert tool_result_text(None) == ""


def test_a_failed_call_says_so_in_the_body() -> None:
    shown = tool_result_text({"content": [{"text": "denied by policy"}]}, ToolOutcome.FAILED)

    assert "⚠️" in shown
    assert "denied by policy" in shown
    assert tool_result_text({"text": "fine"}, ToolOutcome.OK) == "fine"


def test_a_huge_result_is_capped_for_display() -> None:
    shown = tool_result_text({"content": [{"text": "x" * 40000}]})

    assert len(shown) < 2200
    assert "the model received all of it" in shown


def test_items_stay_distinct_without_a_call_id() -> None:
    writer = SseWriter("id", 0, "agent:x")
    first = writer.tool_start("", "a", {})
    second = writer.tool_start("", "b", {})

    assert (
        json.loads(first[len("data: ") :])["item"]["id"]
        != (json.loads(second[len("data: ") :])["item"]["id"])
    )
