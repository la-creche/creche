"""The framing of a byte stream from a sandbox (contract 03 §2).

One surface:

- `channel.frame`: chunks of bytes to `lines.LineSplitter`, the records it
  frames and the ones it refuses.

`channel.parse` and `channel.build` pinned `attendance.wire`, which left in
the phase 2 plan, row 25. Their files are frozen.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from attendance import lines
from vectors.core import Json, Raised, Surface, Vector, accepted, attempt, bytes_input, raised

CONTRACT: Final = "contract 03"


# --- framing -----------------------------------------------------------------


@dataclass(frozen=True)
class Stream:
    """Chunks of bytes, in the order the host reads them, and the cap."""

    id: str
    chunks: tuple[bytes, ...]
    max_line_bytes: int = lines.MAX_LINE_BYTES


_SMALL_CAP: Final = 16

STREAMS: Final[tuple[Stream, ...]] = (
    Stream("one-line", (b'{"type":"pong","nonce":"x"}\n',)),
    Stream("two-lines-one-chunk", (b'{"a":1}\n{"b":2}\n',)),
    Stream("line-over-two-chunks", (b'{"a"', b":1}\n")),
    Stream("line-byte-by-byte", tuple(bytes([one]) for one in b"ab\n")),
    Stream("no-final-lf", (b'{"a":1}',)),
    Stream("lf-only", (b"\n",)),
    Stream("three-empty-lines", (b"\n\n\n",)),
    Stream("crlf", (b'{"a":1}\r\n',)),
    Stream("cr-cr-lf", (b'{"a":1}\r\r\n',)),
    Stream("cr-only", (b'{"a":1}\r{"b":2}\n',)),
    Stream("cr-inside", (b"a\rb\n",)),
    Stream("crlf-split", (b'{"a":1}\r', b"\n")),
    Stream("line-separator-inside", ("a\u2028b\u2029c\n".encode(),)),
    Stream("next-line-inside", ("a\u0085b\n".encode(),)),
    Stream("nul-inside", (b"a\x00b\n",)),
    Stream("form-feed-and-tab", (b"a\x0c\tb\n",)),
    Stream("bom", (b'\xef\xbb\xbf{"a":1}\n',)),
    Stream("bad-utf8", (b"a\xffb\n",)),
    Stream("bad-utf8-then-good", (b"\xff\nok\n",)),
    Stream("utf8-surrogate", (b"\xed\xa0\x80\n",)),
    Stream("utf8-overlong", (b"\xc0\xaf\n",)),
    Stream("utf8-cut-at-lf", (b"\xc3\n\xa9\n",)),
    Stream("utf8-split-over-chunks", (b"caf\xc3", b"\xa9\n")),
    Stream("utf16-bytes", ("ab\n".encode("utf-16-le"),)),
    Stream("at-the-cap", (b"a" * _SMALL_CAP + b"\n",), _SMALL_CAP),
    Stream("at-the-cap-with-cr", (b"a" * (_SMALL_CAP - 1) + b"\r\n",), _SMALL_CAP),
    Stream("one-over-the-cap", (b"a" * (_SMALL_CAP + 1) + b"\nok\n",), _SMALL_CAP),
    Stream("cap-counts-the-cr", (b"a" * _SMALL_CAP + b"\r\nok\n",), _SMALL_CAP),
    Stream("cap-counts-bytes", ("\u00e9".encode() * 9 + b"\nok\n",), _SMALL_CAP),
    Stream(
        "over-the-cap-no-lf-yet",
        (b"a" * (_SMALL_CAP + 1), b"still the same record", b"\nok\n"),
        _SMALL_CAP,
    ),
    Stream(
        "over-the-cap-then-lf-in-one-chunk",
        (b"a" * 40 + b"\nok\n",),
        _SMALL_CAP,
    ),
    Stream(
        "partial-at-the-cap",
        (b"a" * _SMALL_CAP, b"\nok\n"),
        _SMALL_CAP,
    ),
    Stream(
        "dropping-then-two-lines",
        (b"a" * (_SMALL_CAP + 1), b"x\nfirst\nsecond\n"),
        _SMALL_CAP,
    ),
    Stream(
        "dropping-with-no-lf",
        (b"a" * (_SMALL_CAP + 1), b"more of the same record", b"and more"),
        _SMALL_CAP,
    ),
    Stream(
        "dropping-then-partial",
        (b"a" * (_SMALL_CAP + 1), b"x\nfir"),
        _SMALL_CAP,
    ),
    Stream(
        "dropping-then-over-the-cap",
        (b"a" * (_SMALL_CAP + 1), b"x\n" + b"b" * (_SMALL_CAP + 1), b"more", b"\nok\n"),
        _SMALL_CAP,
    ),
    Stream("empty-chunk", (b"", b"ok\n", b"")),
)


def _raw_line(raw: lines.RawLine) -> dict[str, object]:
    return {"text": raw.text, "size": raw.size, "refusal": raw.refusal}


def _frame_vector(stream: Stream) -> Vector:
    chunks: list[Json] = [bytes_input(chunk) for chunk in stream.chunks]
    given: dict[str, Json] = {"chunks": chunks}
    params = {"max_line_bytes": stream.max_line_bytes}

    def split() -> tuple[list[list[lines.RawLine]], int]:
        splitter = lines.LineSplitter(stream.max_line_bytes)
        records = [splitter.feed(chunk) for chunk in stream.chunks]

        return records, splitter.pending_bytes()

    outcome = attempt(split)
    if isinstance(outcome, Raised):
        return raised(stream.id, given, outcome.exc, params=params)

    records, pending = outcome
    feeds = [[_raw_line(raw) for raw in one] for one in records]

    return accepted(stream.id, given, {"feeds": feeds, "pending_bytes": pending}, params=params)


def _vectors[T](items: Sequence[T], make: Callable[[T], Vector]) -> tuple[Vector, ...]:
    return tuple(make(item) for item in items)


def surfaces() -> tuple[Surface, ...]:
    return (
        Surface(
            name="channel.frame",
            path="channel/frame.json",
            entry="attendance.lines.LineSplitter.feed",
            contract=f"{CONTRACT} §2",
            notes=(
                "The input is the chunks of one byte stream, in the order the host reads them.",
                "params.max_line_bytes is the cap the splitter starts with. The host uses "
                f"{lines.MAX_LINE_BYTES}. A vector about the cap uses a small one.",
                "value.feeds holds one array per chunk: the records that chunk completed, or "
                "refused. size is the bytes of the record with no LF. value.pending_bytes is "
                "what the splitter still holds after the last chunk.",
            ),
            vectors=_vectors(STREAMS, _frame_vector),
        ),
    )
