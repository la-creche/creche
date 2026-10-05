"""The index builder `library`: surfaces with the prefix `library.`.

- `library.chunk_text`: a text to its chunks.
- `library.file_hash`: the bytes of a file to its digest.
- `library.read_document`: the bytes of a text file to its text.

Each file of a vector is in a temporary directory. No path of that directory
goes into a vector.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from library.library import (
    CHUNK_OVERLAP,
    CHUNK_SIZE,
    chunk_text,
    file_hash,
    read_document,
)

from vectors.core import (
    Json,
    Raised,
    Surface,
    Vector,
    accepted,
    attempt,
    bytes_input,
    expand,
    quiet_logs,
    raised,
    repeat_input,
    text_input,
)

GROUP: Final = "library"

#: No design contract holds the index builder. `library/AGENTS.md` has its rules.
RULES: Final = "no contract: the rules of library/AGENTS.md"

# --- chunk_text ---------------------------------------------------------------

#: The text of `library/tests/test_library.py` for the bounds of a chunk.
_TEST_TEXT: Final = "\n\n".join(f"paragraph {number} " + "x" * 300 for number in range(10))

#: Each character that `str.strip` removes and `\n` is not: a paragraph of
#: these characters alone is no paragraph.
_PYTHON_SPACE: Final = " \t\r\x0b\x0c\x1c\x1d\x1e\x1f\x85\xa0\u1680\u2000\u2028\u2029\u202f\u3000"


@dataclass(frozen=True)
class ChunkCase:
    """One text and the two numbers that `chunk_text` cuts it with."""

    id: str
    text: str = ""
    size: int = CHUNK_SIZE
    overlap: int = CHUNK_OVERLAP
    #: A long text, written as repeated parts in place of `text`.
    parts: tuple[tuple[str, int], ...] = ()

    def given(self) -> dict[str, Json]:
        return repeat_input(self.parts) if self.parts else text_input(self.text)

    def source(self) -> str:
        return expand(self.parts) if self.parts else self.text


def _cut(case_id: str, text: str, size: int, overlap: int) -> ChunkCase:
    return ChunkCase(case_id, text, size, overlap)


CHUNK_CASES: Final[tuple[ChunkCase, ...]] = (
    # --- no chunk ---
    ChunkCase("empty", ""),
    ChunkCase("only-spaces", "   "),
    ChunkCase("only-newlines", "\n\n\n\n\n"),
    ChunkCase("only-white-space", f"{_PYTHON_SPACE}\n\n{_PYTHON_SPACE}\n"),
    # --- paragraphs ---
    ChunkCase("one-paragraph", "The meeting is on Tuesday in the lobby."),
    ChunkCase(
        "two-paragraphs-join", "The meeting is on Tuesday in the lobby.\n\nBring your badge."
    ),
    ChunkCase("one-newline-is-no-break", "first line\nsecond line\n\nnext paragraph"),
    ChunkCase("three-newlines", "first\n\n\nsecond"),
    ChunkCase("four-newlines", "first\n\n\n\nsecond"),
    ChunkCase("five-newlines", "first\n\n\n\n\nsecond"),
    ChunkCase("newlines-at-the-two-ends", "\n\n\nfirst\n\nsecond\n\n\n"),
    ChunkCase("crlf-is-no-break", "first\r\n\r\nsecond"),
    ChunkCase("crlf-beside-a-break", "\r\nfirst\r\n\n\nsecond\r\n"),
    ChunkCase("cr-is-no-break", "first\r\rsecond"),
    ChunkCase(
        "spaces-at-the-ends-of-a-paragraph", "  first \n\n\tsecond\t\n\n \x0b\x0cthird\x0c\x0b "
    ),
    ChunkCase("separators-at-the-ends-of-a-paragraph", "\x1c\x1dfirst\x1e\x1f\n\n\x1fsecond\x1c"),
    ChunkCase("separators-inside-a-paragraph", "fir\x1cst\n\nsec\x1fond"),
    ChunkCase("next-line-at-the-ends-of-a-paragraph", "\x85first\x85\n\n\x85second\x85"),
    ChunkCase("no-break-space-at-the-ends-of-a-paragraph", "\xa0first\xa0\n\n\xa0second\xa0"),
    ChunkCase("space-of-unicode-at-the-ends-of-a-paragraph", "\u2028first\u3000\n\n\u1680second"),
    ChunkCase("paragraph-of-separators-alone", "first\n\n\x1c\x1d\x1e\x1f\n\nsecond"),
    ChunkCase("paragraph-of-white-space-alone", f"first\n\n{_PYTHON_SPACE}\n\nsecond"),
    ChunkCase("zero-width-space-is-no-space", "\u200bfirst\u200b\n\n\ufeffsecond\u180e"),
    ChunkCase("nul-and-replacement-character", "fir\x00st\n\nsec\ufffdond"),
    # --- the sum of two paragraphs against the size ---
    _cut("sum-exactly-the-size", "abcd\n\nefgh", 10, 3),
    _cut("sum-one-over-the-size", "abcd\n\nefghi", 10, 3),
    _cut("sum-counts-code-points", "\U0001f600\U0001f601\n\n\U0001f602\U0001f603", 6, 2),
    _cut("sum-one-over-in-code-points", "\U0001f600\U0001f601\n\n\U0001f602\U0001f603\u00e9", 6, 2),
    _cut("three-paragraphs-two-chunks", "alpha beta\n\ngamma\n\ndelta epsilon\n\nzeta", 20, 5),
    _cut("each-paragraph-is-a-chunk", "abcdef\n\nghijkl\n\nmnopqr", 10, 2),
    # --- a paragraph against the size ---
    _cut("paragraph-exactly-the-size", "abcdefghij", 10, 3),
    _cut("paragraph-one-over-the-size", "abcdefghijk", 10, 3),
    _cut("long-paragraph", "abcdefghijklmnopqrstuvwxyz", 10, 3),
    _cut("long-paragraph-after-a-short-one", "abc\n\nabcdefghijklmnopqrstuvwxyz", 10, 3),
    _cut("long-paragraph-before-a-short-one", "abcdefghijklmnopqrstuvwxyz\n\nabc", 10, 3),
    _cut("two-long-paragraphs", "abcdefghijklmnopq\n\nABCDEFGHIJKLMNOPQ", 10, 3),
    _cut("cut-leaves-a-space-at-an-end", "abcd efgh ij", 5, 1),
    _cut("cut-inside-a-run-of-spaces", "a            b", 5, 0),
    _cut("cut-at-a-character-outside-the-bmp", "ab\U0001f600cd", 3, 1),
    _cut("cut-after-a-character-outside-the-bmp", "a\U0001f600b\U0001f601c\U0001f602d", 2, 0),
    _cut("cut-before-a-combining-mark", "abe\u0301cd", 3, 0),
    _cut("cut-inside-a-crlf", "ab\r\ncd", 3, 0),
    # --- the overlap ---
    _cut("overlap-zero", "abcd\n\nefghi\n\nabcdefghijklmnopqrstuvwxyz", 10, 0),
    _cut("overlap-one", "abcd\n\nefghi\n\nabcdefghijklmnopqrstuvwxyz", 10, 1),
    _cut("overlap-one-less-than-the-size", "abcdefgh", 4, 3),
    _cut("overlap-starts-with-a-space", "abc de\n\nfghijkl", 10, 3),
    _cut("overlap-starts-inside-a-break", "ab\n\ncd\n\nefghijkl", 10, 3),
    _cut("overlap-longer-than-the-chunk", "ab\n\ncdefghijk", 10, 8),
    _cut("overlap-makes-a-chunk-past-the-size", "abcdefgh\n\nijklmnopqr", 10, 8),
    _cut("size-one", "ab\n\nc", 1, 0),
    # --- the sizes of the index builder ---
    ChunkCase("sizes-of-the-test", _TEST_TEXT),
    ChunkCase("default-sizes-long-paragraph", parts=(("0123456789", 250),)),
    ChunkCase("default-sizes-paragraphs-fill-a-chunk", parts=(("word " * 49 + "end.\n\n", 9),)),
)


def _chunk_vector(case: ChunkCase) -> Vector:
    if not 0 <= case.overlap < case.size:
        raise ValueError(f"{case.id}: the overlap must be less than the size")

    params = {"size": case.size, "overlap": case.overlap}
    text = case.source()
    outcome = attempt(lambda: chunk_text(text, size=case.size, overlap=case.overlap))
    if isinstance(outcome, Raised):
        return raised(case.id, case.given(), outcome.exc, params=params)

    return accepted(case.id, case.given(), outcome, params=params)


def _chunk_surface() -> Surface:
    return Surface(
        name=f"{GROUP}.chunk_text",
        path=f"{GROUP}/chunk_text.json",
        entry="library.library.chunk_text",
        contract=RULES,
        notes=(
            "The input is the text. params.size and params.overlap are the two other "
            "arguments. value is the list of the chunks, in order.",
            "A size and an overlap count code points. The overlap of each vector is less "
            "than its size.",
            "context.defaults holds the size and the overlap that index_scope uses.",
        ),
        context={"defaults": {"size": CHUNK_SIZE, "overlap": CHUNK_OVERLAP}},
        vectors=tuple(_chunk_vector(case) for case in CHUNK_CASES),
    )


# --- file_hash ----------------------------------------------------------------

#: Text with a period of 62 bytes, so that the 16 words of a block of the hash
#: all differ.
_ALPHABET: Final = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"

#: A count of bytes from which an input has the `repeat` form.
_REPEAT_FROM: Final = 1024

#: The name of the file that takes the bytes of a vector.
_HASH_FILE: Final = "content"

MEBIBYTE: Final = 1 << 20


@dataclass(frozen=True)
class Content:
    """The bytes of one file: an id and the bytes, or the parts of a long text."""

    id: str
    raw: bytes = b""
    parts: tuple[tuple[str, int], ...] = ()

    def given(self) -> dict[str, Json]:
        return repeat_input(self.parts) if self.parts else bytes_input(self.raw)

    def source(self) -> bytes:
        return expand(self.parts).encode("utf-8") if self.parts else self.raw


def _sized(count: int) -> Content:
    """`count` bytes of the alphabet, again and again."""
    whole, rest = divmod(count, len(_ALPHABET))
    if count < _REPEAT_FROM:
        return Content(f"bytes-{count}", (_ALPHABET * (whole + 1))[:count].encode("ascii"))

    parts = ((_ALPHABET, whole), (_ALPHABET[:rest], 1))

    return Content(f"bytes-{count}", parts=tuple(part for part in parts if part[0]))


HASH_CONTENTS: Final[tuple[Content, ...]] = (
    Content("empty"),
    Content("one-byte", b"a"),
    Content("one-byte-nul", b"\x00"),
    Content("one-byte-not-utf8", b"\xff"),
    Content("abc", b"abc"),
    Content("text-not-ascii", "caf\u00e9 \U0001f600".encode()),
    Content("each-byte-value", bytes(range(256))),
    *(_sized(count) for count in (127, 128, 129, 255, 256, 257)),
    *(_sized(count) for count in (65535, 65536, 65537, 131072)),
    _sized(MEBIBYTE + 1),
)


def _hash_vector(content: Content, scratch: Path) -> Vector:
    target = scratch / content.id / _HASH_FILE
    target.parent.mkdir(parents=True)
    target.write_bytes(content.source())
    outcome = attempt(lambda: file_hash(target))
    if isinstance(outcome, Raised):
        return raised(content.id, content.given(), outcome.exc)

    return accepted(content.id, content.given(), outcome)


def _hash_surface(scratch: Path) -> Surface:
    return Surface(
        name=f"{GROUP}.file_hash",
        path=f"{GROUP}/file_hash.json",
        entry="library.library.file_hash",
        contract=RULES,
        notes=(
            "The input is the bytes of one file. The generator writes them to a file and "
            "gives the entry point the path.",
            "value is the digest as 32 hexadecimal digits in lower case: BLAKE2b with a "
            "digest of 16 bytes, no key, no salt and no personal text.",
        ),
        vectors=tuple(_hash_vector(content, scratch) for content in HASH_CONTENTS),
    )


# --- read_document ------------------------------------------------------------

#: The name of the file that takes the bytes of a vector. Its suffix selects
#: the text reader.
_TEXT_FILE: Final = "document.txt"

DOCUMENTS: Final[tuple[Content, ...]] = (
    Content("empty"),
    Content("plain", b"The meeting is on Tuesday.\n"),
    Content("no-final-newline", b"one\ntwo"),
    Content("text-not-ascii", "caf\u00e9 \u65e5\u672c \U0001f600\n".encode()),
    Content("last-code-point", b"\xf4\x8f\xbf\xbf"),
    Content("replacement-character-in-the-file", "a\ufffdb".encode()),
    # --- newlines ---
    Content("crlf", b"one\r\ntwo\r\n"),
    Content("cr", b"one\rtwo\r"),
    Content("cr-at-the-end", b"one\r"),
    Content("mixed-newlines", b"one\r\ntwo\rthree\nfour\r\r\nfive\n\rsix"),
    Content("crlf-twice-is-a-paragraph-break", b"one\r\n\r\ntwo"),
    Content(
        "other-line-breaks-stay", b"a\x0bb\x0cc\x1cd\x1de\x1ef\xc2\x85g\xe2\x80\xa8h\xe2\x80\xa9i"
    ),
    # --- bytes that a text keeps ---
    Content("byte-order-mark", b"\xef\xbb\xbfone\n"),
    Content("byte-order-mark-inside", b"one\xef\xbb\xbftwo"),
    Content("byte-order-mark-of-utf16", b"\xff\xfeo\x00n\x00e\x00"),
    Content("nul-byte", b"one\x00two"),
    Content("control-bytes", bytes(range(1, 32)) + b"\x7f"),
    # --- bytes that are not UTF-8 ---
    Content("continuation-byte-alone", b"a\x80b"),
    Content("two-continuation-bytes", b"a\x80\xbfb"),
    Content("cut-sequence-of-2-at-the-end", b"a\xc3"),
    Content("cut-sequence-of-2", b"a\xc3b"),
    Content("cut-sequence-of-3-at-the-end", b"a\xe2\x82"),
    Content("cut-sequence-of-3", b"a\xe2\x82b"),
    Content("cut-sequence-of-3-after-1-byte", b"a\xe2b"),
    Content("cut-sequence-of-4-at-the-end", b"a\xf0\x9f\x98"),
    Content("cut-sequence-of-4", b"a\xf0\x9f\x98b"),
    Content("cut-sequence-of-4-after-2-bytes", b"a\xf0\x9fb"),
    Content("cut-sequence-of-4-after-1-byte", b"a\xf0b"),
    Content("cut-sequence-before-a-sequence", b"\xe2\x82\xc3\xa9"),
    Content("overlong-form-of-2", b"\xc0\x80"),
    Content("overlong-form-of-2-last", b"\xc1\xbf"),
    Content("overlong-form-of-3", b"\xe0\x80\x80"),
    Content("overlong-form-of-3-last", b"\xe0\x9f\xbf"),
    Content("overlong-form-of-4", b"\xf0\x80\x80\x80"),
    Content("overlong-form-of-4-last", b"\xf0\x8f\xbf\xbf"),
    Content("surrogate-first", b"\xed\xa0\x80"),
    Content("surrogate-last", b"\xed\xbf\xbf"),
    Content("surrogate-pair", b"\xed\xa0\xbd\xed\xb8\x80"),
    Content("before-the-surrogates", b"\xed\x9f\xbf"),
    Content("above-the-last-code-point", b"\xf4\x90\x80\x80"),
    Content("lead-byte-f5", b"\xf5\x80\x80\x80"),
    Content("sequence-of-5", b"\xf8\x88\x80\x80\x80"),
    Content("byte-fe", b"a\xfeb"),
    Content("byte-ff", b"a\xffb"),
    Content("bad-byte-before-crlf", b"a\xff\r\nb\xc3\rc"),
    Content("latin-1-text", "caf\u00e9\n".encode("latin-1")),
)


def _read_vector(document: Content, scratch: Path) -> Vector:
    target = scratch / document.id / _TEXT_FILE
    target.parent.mkdir(parents=True)
    target.write_bytes(document.source())
    outcome = attempt(lambda: read_document(target))
    if isinstance(outcome, Raised):
        return raised(document.id, document.given(), outcome.exc)

    return accepted(document.id, document.given(), outcome)


def _read_surface(scratch: Path) -> Surface:
    return Surface(
        name=f"{GROUP}.read_document",
        path=f"{GROUP}/read_document.json",
        entry="library.library.read_document",
        contract=RULES,
        notes=(
            "The input is the bytes of one file. The generator writes them to a file with "
            f"the name {_TEXT_FILE} and gives the entry point the path.",
            "value is the text that the entry point returns. index_scope gives this text "
            "to chunk_text.",
            "No vector holds a file with the suffix .pdf.",
        ),
        vectors=tuple(_read_vector(document, scratch) for document in DOCUMENTS),
    )


def surfaces() -> tuple[Surface, ...]:
    with tempfile.TemporaryDirectory(prefix="vectors-library-") as scratch_name, quiet_logs():
        scratch = Path(scratch_name)

        return (
            _chunk_surface(),
            _hash_surface(scratch / "hash"),
            _read_surface(scratch / "read"),
        )
