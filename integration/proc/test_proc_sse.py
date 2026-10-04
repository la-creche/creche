"""The SSE reader, held to what a real SSE client does.

A door in another language writes its own frames. A reader that accepts more
than Open WebUI accepts would pass a door that Open WebUI cannot read. No
test here starts a service.
"""

from __future__ import annotations

import pytest
from proc_sse import parse

FIRST = 'data: {"id": "a", "choices": [{"delta": {"content": "one"}}]}'
SECOND = 'data: {"id": "a", "choices": [{"delta": {"content": " two"}}]}'
DONE = "data: [DONE]"
END = "\n\n"


def test_a_stream_that_ends_with_done_is_read() -> None:
    frames = parse(FIRST + END + SECOND + END + DONE + END)

    assert frames.text == "one two"
    assert frames.ends_with_done


def test_a_comment_is_dropped() -> None:
    frames = parse(": keepalive" + END + FIRST + END + ":" + END + DONE + END)

    assert frames.text == "one"
    assert frames.ends_with_done


def test_a_frame_after_done_is_not_an_end() -> None:
    frames = parse(FIRST + END + DONE + END + SECOND + END)

    assert not frames.ends_with_done


def test_a_done_with_no_blank_line_is_not_an_end() -> None:
    """A client sends an event on when the blank line arrives, and not before."""
    frames = parse(FIRST + END + DONE + "\n")

    assert not frames.ends_with_done


def test_two_events_with_one_newline_are_one_event() -> None:
    """A client joins the two lines into one event, and that event is no JSON."""
    with pytest.raises(ValueError, match="Extra data"):
        parse(FIRST + "\n" + SECOND + END + DONE + END)


def test_a_carriage_return_ends_a_line_too() -> None:
    frames = parse(FIRST + "\r\n\r\n" + DONE + "\r\n\r\n")

    assert frames.text == "one"
    assert frames.ends_with_done
