"""Line framing for every byte stream from a sandbox (contract 03 §2, §8).

Every byte from a sandbox is untrusted. The facade runs inside the sandbox,
and the agent process is untrusted by design (invariant 12), so a line is
checked for size and UTF-8 before anything reads it, and a bad line is a
refusal rather than an exception.

Framing is LF only. A generic line reader is wrong here: Node's `readline`
also splits on U+2028 and U+2029, which are legal inside a JSON string
(contract 03 §2 rule 4).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

MAX_LINE_BYTES = 1_048_576

_LF_BYTE = b"\n"
_CR = "\r"

# The error handler that encodes one half of a surrogate pair as its three
# bytes. A count of bytes then has a value for each text.
_KEEP_HALF_PAIRS = "surrogatepass"

# A byte of UTF-8 with the bits `10xxxxxx` is not the first byte of a
# character.
_CONTINUATION_MASK = 0xC0
_CONTINUATION_BITS = 0x80


class LineRefusal(StrEnum):
    """Why a line from a sandbox was dropped before any reader saw it."""

    TOO_LARGE = "too_large"
    BAD_UTF8 = "bad_utf8"


@dataclass(slots=True)
class RawLine:
    """One framed record, or the reason it was refused."""

    text: str | None
    size: int
    refusal: LineRefusal | None = None


class LineSplitter:
    """Splits an incoming byte stream on LF only (contract 03 §2).

    A record over the cap is refused, and the bytes up to the next LF are
    discarded so the stream stays usable. Probe 0a measured that exact
    behaviour: one byte over was refused and the channel kept working (§14,
    A8).

    The buffer holds the start of one record and never more than the cap.
    No byte of a chunk enters it before the size check, in each state.
    """

    def __init__(self, max_line_bytes: int = MAX_LINE_BYTES) -> None:
        self._max = max_line_bytes
        self._buffer = bytearray()
        self._dropping = False

    def feed(self, chunk: bytes) -> list[RawLine]:
        lines: list[RawLine] = []
        view = memoryview(chunk)
        start = 0

        while True:
            index = chunk.find(_LF_BYTE, start)

            if index < 0:
                break

            tail = view[start:index]
            start = index + 1

            if self._dropping:
                self._dropping = False
                continue

            lines.append(self._record(tail))
            self._buffer.clear()

        # The record that is being dropped has no LF yet. Its bytes go.
        if self._dropping:
            return lines

        rest = view[start:]
        size = len(self._buffer) + len(rest)

        # A partial record already over the cap can be refused now. Holding it
        # would let one bad sender grow this buffer without bound.
        if size > self._max:
            lines.append(RawLine(text=None, size=size, refusal=LineRefusal.TOO_LARGE))
            self._buffer.clear()
            self._dropping = True
            return lines

        self._buffer.extend(rest)

        return lines

    def pending_bytes(self) -> int:
        return len(self._buffer)

    def _record(self, tail: memoryview) -> RawLine:
        """The record that `tail` completes. The size check comes first."""
        size = len(self._buffer) + len(tail)

        if size > self._max:
            return RawLine(text=None, size=size, refusal=LineRefusal.TOO_LARGE)

        return _decode_record(bytes(self._buffer) + bytes(tail))


def _decode_record(record: bytes) -> RawLine:
    size = len(record)

    try:
        text = record.decode("utf-8")
    except UnicodeDecodeError:
        return RawLine(text=None, size=size, refusal=LineRefusal.BAD_UTF8)

    # One optional trailing CR is stripped. Nothing else is (§2 rule 3).
    if text.endswith(_CR):
        text = text[:-1]

    return RawLine(text=text, size=size)


def cut_bytes(text: str, cap: int) -> str:
    """The longest start of `text` that has at most `cap` bytes of UTF-8.

    Contract 03 §8 gives each cap of a text in bytes. The cut is between two
    characters, so the result holds no part of a character. A text that fits
    is returned as it is.

    CONTRACT-QUESTION: §8 counts bytes and gives no count of bytes for one
    half of a surrogate pair. Such a half has no UTF-8 form. It counts as
    three bytes here, and it stays in the text: the reading that changes no
    character of a text. The other reading puts U+FFFD in the place of the
    half. A change costs one step of this function.
    """
    data = text.encode("utf-8", _KEEP_HALF_PAIRS)

    if len(data) <= cap:
        return text

    end = max(cap, 0)

    while end > 0 and (data[end] & _CONTINUATION_MASK) == _CONTINUATION_BITS:
        end -= 1

    return data[:end].decode("utf-8", _KEEP_HALF_PAIRS)
