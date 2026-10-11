"""Each cap of a text from the playpen counts bytes of UTF-8 (contract 03 §8)."""

from __future__ import annotations

import json

import pytest
from attendance.wire import (
    MAX_ENTRY_BYTES,
    MAX_LOG_BYTES,
    MAX_REASON_BYTES,
    EntriesLine,
    FailedLine,
    FatalLine,
    LogLine,
    OpenedLine,
    cut_bytes,
    parse,
)

SESSION = "owui-3f2a9c41"
TURN = "01JBQ7WZ0X4T9V6K2H8M3N5PQR"

# One character for each count of bytes that UTF-8 has past one byte.
TWO_BYTES = "é"
THREE_BYTES = "€"
FOUR_BYTES = "\U0001f600"

# One half of a surrogate pair. It has no UTF-8 form.
HALF_PAIR = "\ud83d"
HALF_PAIR_BYTES = 3

# Each line whose `message` the host cuts, without the message.
MESSAGE_LINES: dict[str, dict[str, object]] = {
    "session_opened": {"type": "session_opened", "session": SESSION},
    "fatal": {"type": "fatal", "reason": "mount_dir_unset"},
    "log": {"type": "log"},
    "turn_failed": {
        "type": "turn_failed",
        "session": SESSION,
        "turn": TURN,
        "turn_seq": 1,
        "reason": "internal",
    },
}

# Each line whose `reason` the host keeps as a name, without the reason.
REASON_LINES: dict[str, dict[str, object]] = {
    "session_opened": {"type": "session_opened", "session": SESSION},
    "entries": {"type": "entries", "request": TURN, "session": SESSION},
}


def _line(**fields: object) -> str:
    """One compact JSON object, as the playpen writes a line."""
    return json.dumps(fields, separators=(",", ":"), ensure_ascii=False)


def _size(text: str) -> int:
    return len(text.encode("utf-8"))


@pytest.mark.parametrize("kind", sorted(MESSAGE_LINES))
def test_a_message_keeps_the_log_cap_in_bytes(kind: str) -> None:
    long = TWO_BYTES * MAX_LOG_BYTES

    line = parse(_line(**MESSAGE_LINES[kind], message=long))

    assert isinstance(line, (OpenedLine, FatalLine, LogLine, FailedLine))
    assert line.message == TWO_BYTES * (MAX_LOG_BYTES // 2)
    assert _size(line.message) == MAX_LOG_BYTES


@pytest.mark.parametrize("kind", sorted(MESSAGE_LINES))
def test_a_message_at_the_log_cap_stays_whole(kind: str) -> None:
    at_cap = TWO_BYTES * (MAX_LOG_BYTES // 2)

    line = parse(_line(**MESSAGE_LINES[kind], message=at_cap))

    assert isinstance(line, (OpenedLine, FatalLine, LogLine, FailedLine))
    assert line.message == at_cap


def test_the_text_of_an_entry_keeps_the_entry_cap_in_bytes() -> None:
    long = TWO_BYTES * MAX_ENTRY_BYTES
    entry = {"id": "e-1", "role": "user", "text": long}

    line = parse(_line(type="entries", request=TURN, session=SESSION, ok=True, entries=[entry]))

    assert isinstance(line, EntriesLine)
    assert line.entries[0].text == TWO_BYTES * (MAX_ENTRY_BYTES // 2)
    assert _size(line.entries[0].text) == MAX_ENTRY_BYTES


def test_the_text_of_an_entry_at_the_entry_cap_stays_whole() -> None:
    at_cap = TWO_BYTES * (MAX_ENTRY_BYTES // 2)
    entry = {"id": "e-1", "role": "user", "text": at_cap}

    line = parse(_line(type="entries", request=TURN, session=SESSION, ok=True, entries=[entry]))

    assert isinstance(line, EntriesLine)
    assert line.entries[0].text == at_cap


@pytest.mark.parametrize("kind", sorted(REASON_LINES))
def test_a_reason_name_keeps_the_reason_cap_in_bytes(kind: str) -> None:
    long = TWO_BYTES * MAX_REASON_BYTES

    line = parse(_line(**REASON_LINES[kind], reason=long))

    assert isinstance(line, (OpenedLine, EntriesLine))
    assert line.reason == TWO_BYTES * (MAX_REASON_BYTES // 2)


def test_the_three_caps_are_counts_of_bytes() -> None:
    assert MAX_LOG_BYTES == 4096
    assert MAX_ENTRY_BYTES == 65_536
    assert MAX_REASON_BYTES == 64


@pytest.mark.parametrize("wide", [TWO_BYTES, THREE_BYTES, FOUR_BYTES])
def test_a_cut_ends_on_a_whole_character(wide: str) -> None:
    cap = 64
    width = _size(wide)

    for room in range(width):
        # `room` bytes are free before the cap, and the character needs more.
        text = "m" * (cap - room) + wide + "tail"

        assert cut_bytes(text, cap) == "m" * (cap - room)

    exact = "m" * (cap - width) + wide

    assert cut_bytes(exact + "tail", cap) == exact


def test_a_text_that_fits_stays_as_it_is() -> None:
    for text in ("", "exit 137", TWO_BYTES * 32, FOUR_BYTES * 16):
        assert cut_bytes(text, 64) == text


def test_a_cut_at_no_byte_gives_the_empty_text() -> None:
    assert cut_bytes("exit 137", 0) == ""
    assert cut_bytes("exit 137", -1) == ""
    assert cut_bytes(TWO_BYTES, 1) == ""


def test_half_a_pair_counts_as_three_bytes_and_stays() -> None:
    text = "a" + HALF_PAIR + "b"

    assert cut_bytes(text, 1 + HALF_PAIR_BYTES + 1) == text
    assert cut_bytes(text, 1 + HALF_PAIR_BYTES) == "a" + HALF_PAIR
    assert cut_bytes(text, HALF_PAIR_BYTES) == "a"
    assert cut_bytes(HALF_PAIR * 4, 2 * HALF_PAIR_BYTES + 1) == HALF_PAIR * 2


def test_a_message_of_half_pairs_is_cut_and_not_refused() -> None:
    halves = MAX_LOG_BYTES // HALF_PAIR_BYTES
    escaped = "\\ud83d" * (halves + 1)

    line = parse('{"type":"log","message":"' + escaped + '"}')

    assert isinstance(line, LogLine)
    assert line.message == HALF_PAIR * halves
