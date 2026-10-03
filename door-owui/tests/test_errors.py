"""OpenAI-shaped refusals: the attendance error map, and a turn failure."""

from __future__ import annotations

import json

from agent_door_owui.errors import ErrorType, from_attendance, turn_failure
from agent_door_owui.journal import parse_line
from agent_door_owui.sse import SseWriter
from agent_door_owui.translate import TurnTranslator


def test_session_busy_maps_to_409_with_a_readable_sentence() -> None:
    error = from_attendance("session_busy", "another door holds the writer lease")

    assert error.status == 409
    assert error.code == "session_busy"
    assert "only one may write at a time" in error.message


def test_the_doors_own_token_never_reads_as_a_401() -> None:
    # `unauthorized`/`forbidden` here mean the DOOR's own attendance token was
    # refused, not the Open WebUI connection key. A 401 would send the
    # reader to fix the wrong credential.
    for code in ("unauthorized", "forbidden"):
        error = from_attendance(code, "token refused")

        assert error.status != 401
        assert error.error_type is ErrorType.SERVER


def test_an_unknown_code_still_answers_with_something() -> None:
    error = from_attendance("a_future_code_this_door_has_never_seen", "detail text")

    assert error.status == 502
    assert error.message == "detail text"
    assert error.code == "a_future_code_this_door_has_never_seen"


def test_an_unknown_code_with_no_message_still_names_itself() -> None:
    error = from_attendance("a_future_code", "")

    assert error.message == "a_future_code"


def test_a_turn_failure_names_the_reason_and_never_401s() -> None:
    error = turn_failure("turn_timeout")

    assert error.code == "turn_timeout"
    assert "turn_timeout" in error.message
    assert error.status == 502
    assert error.error_type is ErrorType.SERVER


def test_turn_failure_and_the_streaming_error_share_one_sentence() -> None:
    # The non-streaming path and translate.TurnTranslator.fail() must read
    # the same to a person, regardless of which `wait` mode answered.
    translator = TurnTranslator(SseWriter("id", 0, "agent:x"))
    translator.start()
    body = {
        "journal_seq": 1,
        "kind": "turn_failed",
        "turn": None,
        "body": {"reason": "model_error"},
    }
    line = parse_line(json.dumps(body).encode("utf-8"))
    frames = "".join(translator.feed(line))

    assert turn_failure("model_error").message in frames
