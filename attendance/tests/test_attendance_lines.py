"""Framing and the byte cap of a text.

What every stream to a facade shares: the LF framing of contract 03 §2 and
the cut of a text by bytes of contract 03 §8.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from attendance.atomic import read_json
from attendance.lines import MAX_LINE_BYTES, LineRefusal, LineSplitter, cut_bytes

# U+2028 and U+2029 are legal inside a JSON string. A generic line reader
# splits on them, which would tear one record into two (contract 03 §2 rule 4).
UNICODE_SEPARATORS = f"line{chr(0x2028)}sep{chr(0x2029)}arator"

# A cap of a few bytes, so a test passes it with a short record.
SMALL_CAP = 16

# One character for each count of bytes that UTF-8 has past one byte.
TWO_BYTES = "é"
THREE_BYTES = "€"
FOUR_BYTES = "\U0001f600"

# One half of a surrogate pair. It has no UTF-8 form.
HALF_PAIR = "\ud83d"
HALF_PAIR_BYTES = 3


def _size(text: str) -> int:
    return len(text.encode("utf-8"))


# ------------------------------------------------------------- framing


def test_the_splitter_uses_lf_and_nothing_else() -> None:
    splitter = LineSplitter()
    payload = json.dumps({"type": "log", "message": UNICODE_SEPARATORS})
    lines = splitter.feed(payload.encode("utf-8") + b"\n")

    assert len(lines) == 1
    assert lines[0].text == payload
    assert json.loads(payload)["message"] == UNICODE_SEPARATORS


def test_the_splitter_strips_one_trailing_cr() -> None:
    lines = LineSplitter().feed(b'{"type":"pong","nonce":"9f13"}\r\n')

    assert lines[0].text == '{"type":"pong","nonce":"9f13"}'


def test_a_partial_record_waits_for_its_lf() -> None:
    splitter = LineSplitter()

    assert splitter.feed(b'{"type":"pon') == []
    assert splitter.pending_bytes() == 12

    lines = splitter.feed(b'g","nonce":"1"}\n')
    assert len(lines) == 1


def test_an_oversized_record_is_refused_and_the_stream_recovers() -> None:
    """Probe 0a, A8: one byte over was refused and the channel stayed usable."""
    splitter = LineSplitter()
    lines = splitter.feed(b"x" * (MAX_LINE_BYTES + 1) + b"\n")

    assert len(lines) == 1
    assert lines[0].refusal is LineRefusal.TOO_LARGE

    good = splitter.feed(b'{"type":"pong","nonce":"1"}\n')
    assert good[0].text is not None


def test_an_oversized_partial_record_is_refused_before_its_lf() -> None:
    """A sender with no LF must not grow this buffer without bound."""
    splitter = LineSplitter()
    lines = splitter.feed(b"x" * (MAX_LINE_BYTES + 1))

    assert lines[0].refusal is LineRefusal.TOO_LARGE
    assert splitter.pending_bytes() == 0

    splitter.feed(b'rest of the bad line\n{"type":"pong","nonce":"1"}\n')


def test_the_splitter_keeps_no_byte_of_a_record_that_it_drops() -> None:
    """The bytes between the refusal and the next LF are not kept."""
    splitter = LineSplitter(SMALL_CAP)

    assert splitter.feed(b"x" * (SMALL_CAP + 1))[0].refusal is LineRefusal.TOO_LARGE

    for _ in range(5):
        assert splitter.feed(b"y" * 1000) == []
        assert splitter.pending_bytes() == 0

    lines = splitter.feed(b'the end of the record\n{"a":1}\n')

    assert [line.text for line in lines] == ['{"a":1}']
    assert splitter.pending_bytes() == 0


def test_a_record_at_the_cap_leaves_no_byte_for_the_next() -> None:
    """The record fills the buffer before its LF comes. The next record
    starts with an empty buffer."""
    splitter = LineSplitter(SMALL_CAP)

    assert splitter.feed(b"a" * SMALL_CAP) == []
    assert splitter.pending_bytes() == SMALL_CAP

    lines = splitter.feed(b"\nok\n")

    assert [line.text for line in lines] == ["a" * SMALL_CAP, "ok"]
    assert splitter.pending_bytes() == 0


@pytest.mark.parametrize("chunk_bytes", [1, 3, SMALL_CAP, SMALL_CAP + 1, 64, 1000])
def test_the_splitter_holds_the_cap_at_most_in_each_state(chunk_bytes: int) -> None:
    """The result does not change with the size of a chunk."""
    stream = (
        b"a" * 40
        + b"\nok\n"
        + b"b" * 100
        + b"\n"
        + b"c" * SMALL_CAP
        + b"\n"
        + b"d" * (SMALL_CAP + 1)
        + b"e" * 100
    )
    splitter = LineSplitter(SMALL_CAP)
    texts: list[str] = []
    refusals: list[LineRefusal] = []

    for start in range(0, len(stream), chunk_bytes):
        for line in splitter.feed(stream[start : start + chunk_bytes]):
            if line.text is not None:
                texts.append(line.text)

            if line.refusal is not None:
                refusals.append(line.refusal)

        assert splitter.pending_bytes() <= SMALL_CAP

    assert texts == ["ok", "c" * SMALL_CAP]
    assert refusals == [LineRefusal.TOO_LARGE] * 3
    assert splitter.pending_bytes() == 0


# --------------------------------------------------- the cap of a text


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


# ------------------------------------------------------------- sbx exec


@pytest.mark.parametrize(
    "content",
    ["[" * 200_000, '{"beat":' + "1" * 5_000 + "}"],
    ids=["deep_nesting", "long_integer"],
)
def test_read_json_reads_content_that_made_it_raise_as_nothing(
    tmp_path: Path, content: str
) -> None:
    """A file of another process is read with this reader."""
    path = tmp_path / "status.json"
    path.write_text(content, encoding="utf-8")

    assert read_json(path) is None
