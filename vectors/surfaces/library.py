"""The index builder `library`: eight surfaces with the prefix `library.`.

- `library.chunk_text`: a text to its chunks.
- `library.file_hash`: the bytes of a file to its digest.
- `library.read_document`: the bytes of a text file to its text.
- `library.report`: the fields of a report to the text of the report.
- `library.tei_url`: the variables of the process to the URL of the embedder.
- `library.embedding`: the values of one vector to the bytes that the store
  keeps for it.
- `library.schema`: the tables of a new store.
- `library.index_scope`: a corpus and a list of steps to the rows of the
  store after each run.

The last three go through `index_scope`. The function that packs a vector and
the function that walks a corpus are private (`vectors/AGENTS.md`, "Known
gaps"). Each corpus and each store is in a temporary directory. No path of
that directory and no time of a run goes into a vector.
"""

from __future__ import annotations

import re
import sqlite3
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from library.__main__ import ConfigError, tei_url
from library.library import (
    CHUNK_OVERLAP,
    CHUNK_SIZE,
    DIMS,
    EMBED_MAX_BATCH,
    PROFILES,
    Corpus,
    IndexReport,
    chunk_text,
    file_hash,
    index_scope,
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
    normalize,
    quiet_logs,
    raised,
    refused,
    repeat_input,
    text_input,
)

GROUP: Final = "library"

#: No design contract holds the index builder. `library/AGENTS.md` has its rules.
RULES: Final = "no contract: the rules of library/AGENTS.md"

#: The reader of a store and the document that holds the schema.
STORE_CONTRACT: Final = "contract 03 §7.3 rule 6, and the store schema of library/AGENTS.md"

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
            "value is the digest as 32 hexadecimal digits in lower case. The hash is "
            "BLAKE2b with a digest of 16 bytes, no key, no salt and no personalization.",
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


# --- the report ---------------------------------------------------------------

#: The scope of each report. It is a path of no machine.
SCOPE: Final = "/corpus/notes"


@dataclass(frozen=True)
class ReportCase:
    """The fields of one report. A path of a field is under `SCOPE`."""

    id: str
    indexed: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    unchanged: int = 0
    chunks: int = 0
    #: A path and the text of its error, in the order of the report.
    errors: tuple[tuple[str, str], ...] = ()
    rebuilt: bool = False

    def args(self) -> dict[str, object]:
        return {
            "scope": SCOPE,
            "indexed": self.indexed,
            "removed": self.removed,
            "unchanged": self.unchanged,
            "chunks": self.chunks,
            "errors": self.errors,
            "rebuilt": self.rebuilt,
        }


def _under(*names: str) -> tuple[str, ...]:
    return tuple(f"{SCOPE}/{name}" for name in names)


def _error(name: str, text: str) -> tuple[str, str]:
    return f"{SCOPE}/{name}", text


REPORTS: Final[tuple[ReportCase, ...]] = (
    ReportCase("nothing"),
    ReportCase("one-file", indexed=_under("a.md"), chunks=1),
    ReportCase(
        "each-count",
        indexed=_under("B.md", "a/b.md"),
        removed=_under("old.md"),
        unchanged=3,
        chunks=7,
    ),
    ReportCase("only-unchanged", unchanged=12345),
    ReportCase("only-removed", removed=_under("a b.md", "a-b/c.md")),
    ReportCase("file-with-no-chunk", indexed=_under("empty.md")),
    ReportCase("large-counts", indexed=_under("a.md"), unchanged=1234567, chunks=4294967296),
    ReportCase("rebuilt", indexed=_under("a.md", "a/b.md"), chunks=2, rebuilt=True),
    ReportCase("rebuilt-and-nothing", rebuilt=True),
    ReportCase("one-error", errors=(_error("scan.pdf", "the file is no document"),)),
    ReportCase(
        "two-errors-in-order",
        indexed=_under("a.md"),
        chunks=1,
        errors=(
            _error("z/last.pdf", "the first error of the run"),
            _error("a/first.pdf", "the second error of the run"),
        ),
    ),
    ReportCase(
        "rebuilt-with-an-error",
        indexed=_under("a.md"),
        chunks=2,
        errors=(_error("scan.pdf", "the file is no document"),),
        rebuilt=True,
    ),
    ReportCase("error-text-with-a-newline", errors=(_error("a.md", "line one\nline two"),)),
    ReportCase("error-text-with-crlf", errors=(_error("a.md", "line one\r\nline two\r"),)),
    ReportCase("error-text-empty", errors=(_error("a.md", ""),)),
    ReportCase("error-text-with-spaces-at-the-ends", errors=(_error("a.md", "  text  "),)),
    ReportCase("error-text-of-200-code-points", errors=(_error("a.md", "\u00e9" * 200),)),
    ReportCase(
        "text-not-ascii",
        indexed=_under("caf\u00e9.md"),
        chunks=1,
        errors=(_error("\u65e5\u672c.md", "no text: \U0001f600"),),
    ),
    ReportCase("path-with-a-colon", errors=(_error("a: b.md", "text: with a colon"),)),
)


def _report_vector(case: ReportCase) -> Vector:
    given: dict[str, Json] = {"args": normalize(case.args())}
    report = IndexReport(
        scope=SCOPE,
        indexed=list(case.indexed),
        removed=list(case.removed),
        unchanged=case.unchanged,
        chunks=case.chunks,
        errors=dict(case.errors),
        rebuilt=case.rebuilt,
    )
    outcome = attempt(report.render)
    if isinstance(outcome, Raised):
        return raised(case.id, given, outcome.exc)

    return accepted(case.id, given, output=text_input(outcome))


def _report_surface() -> Surface:
    return Surface(
        name=f"{GROUP}.report",
        path=f"{GROUP}/report.json",
        entry="library.library.IndexReport.render",
        contract=RULES,
        notes=(
            "The input is the fields of one IndexReport, as args. args.errors is a list of "
            "pairs in the order of the report: a path, then the text of its error.",
            "output is the text that the entry point returns. It ends with no newline.",
            f"args.scope is {SCOPE} in each vector, and each path of a vector is under it. "
            "index_scope gives a report the full path of the scope and of each file.",
            "A file with no chunk is in args.indexed. No vector has chunks with an empty "
            "args.indexed. No error text is longer than 200 code points: index_scope cuts "
            "a longer text before the report holds it.",
        ),
        vectors=tuple(_report_vector(case) for case in REPORTS),
    )


# --- the URL of the embedder --------------------------------------------------

LAN: Final = "192.0.2.10"
TEI: Final = "http://192.0.2.10:8085"

#: A name of a variable in the text of a `ConfigError`.
_VARIABLE: Final = re.compile(r"[A-Z][A-Z0-9_]+")


@dataclass(frozen=True)
class Environment:
    """One environment of `index-scope`: an id and its variables."""

    id: str
    variables: dict[str, str]

    def given(self) -> dict[str, Json]:
        return {"args": normalize(self.variables)}


def _env(env_id: str, **variables: str) -> Environment:
    return Environment(env_id, variables)


#: Each URL and each LAN address below is a text that the strict config
#: types of the Rust crate take too. The entry point returns each text that is
#: not empty (`vectors/AGENTS.md`, "Known gaps").
ENVIRONMENTS: Final[tuple[Environment, ...]] = (
    # --- the cases of library/tests/test_library_tei_url.py ---
    _env("lan-address", AGENT_LAN_ADDRESS=LAN),
    _env("tei-url", TEI_URL="http://tei.test:1"),
    _env("no-variable"),
    # --- which variable wins ---
    _env("tei-url-wins", TEI_URL="http://tei.test:1", AGENT_LAN_ADDRESS=LAN),
    _env("tei-url-empty", TEI_URL=""),
    _env("tei-url-empty-and-lan-address", TEI_URL="", AGENT_LAN_ADDRESS=LAN),
    _env("tei-url-white-space-and-lan-address", TEI_URL=" \t\r\n", AGENT_LAN_ADDRESS=LAN),
    _env("tei-url-separators-and-lan-address", TEI_URL="\x1c\x1d\x1e\x1f", AGENT_LAN_ADDRESS=LAN),
    _env("lan-address-empty", AGENT_LAN_ADDRESS=""),
    _env("lan-address-white-space", AGENT_LAN_ADDRESS=" \t\r\n"),
    _env("lan-address-separators", AGENT_LAN_ADDRESS="\x1c\x1d\x1e\x1f"),
    _env("both-empty", TEI_URL="", AGENT_LAN_ADDRESS=""),
    _env("tei-url-and-lan-address-empty", TEI_URL=TEI, AGENT_LAN_ADDRESS=""),
    _env("other-variables", OTHER="1", tei_url="http://lower.test:1", AGENT_LAN_ADDRESS=LAN),
    # --- white space around a value ---
    _env("tei-url-spaces-around", TEI_URL=f"  {TEI}\n"),
    _env("tei-url-separators-around", TEI_URL=f"\x1c\x1d{TEI}\x1e\x1f"),
    _env("tei-url-space-of-unicode-around", TEI_URL=f"\x85\xa0{TEI}\u2028\u3000"),
    _env("lan-address-spaces-around", AGENT_LAN_ADDRESS=f" {LAN}\r\n"),
    _env("lan-address-separators-around", AGENT_LAN_ADDRESS=f"\x1f{LAN}\x1c"),
    _env("lan-address-space-of-unicode-around", AGENT_LAN_ADDRESS=f"\xa0{LAN}\x85"),
    # --- the forms of a value ---
    _env("tei-url-final-slash", TEI_URL=f"{TEI}/"),
    _env("tei-url-path-and-final-slash", TEI_URL=f"{TEI}/embedder/"),
    _env("tei-url-no-port", TEI_URL="http://tei.test"),
    _env("tei-url-host-name-and-port", TEI_URL="http://host-1.example:8085"),
    _env("lan-address-host-name", AGENT_LAN_ADDRESS="host-1.example"),
    _env("lan-address-one-label", AGENT_LAN_ADDRESS="localhost"),
)


def _named_variables(error: ConfigError) -> dict[str, list[str]]:
    """Each variable that the text of a `ConfigError` names, in the order of the text."""
    names: list[str] = _VARIABLE.findall(str(error))
    if not names:
        raise ValueError("a ConfigError that names no variable")

    return {"variables": names}


def _tei_url_vector(env: Environment) -> Vector:
    variables: Mapping[str, str] = dict(env.variables)
    outcome = attempt(lambda: tei_url(variables))
    if not isinstance(outcome, Raised):
        return accepted(env.id, env.given(), outcome)

    if isinstance(outcome.exc, ConfigError):
        return refused(env.id, env.given(), _named_variables(outcome.exc))

    return raised(env.id, env.given(), outcome.exc)


def _tei_url_surface() -> Surface:
    return Surface(
        name=f"{GROUP}.tei_url",
        path=f"{GROUP}/tei_url.json",
        entry="library.__main__.tei_url",
        contract=RULES,
        notes=(
            "The input is the variables of the process, as args.",
            "value is the base URL of the embedder, as the entry point returns it.",
            "A refused vector is an environment for which the entry point raises its "
            "ConfigError. refusal.variables is each variable that the error text names, in "
            "the order of the text.",
            "Each URL of a vector has the scheme http, a host, no user part and no space "
            "inside. Each LAN address of a vector is an IPv4 address or a host name, and it "
            "is not 0.0.0.0. The config types of the Rust crate take each such text.",
        ),
        vectors=tuple(_tei_url_vector(env) for env in ENVIRONMENTS),
    )


# --- a store ------------------------------------------------------------------

#: The model of a stub embedder when a vector names none.
MODEL: Final = "BAAI/bge-base-en-v1.5"

#: The two directories of one run, under the directory of a vector. The index
#: directory is outside the scope.
_SCOPE_DIR: Final = "scope"
_INDEX_DIR: Final = "index"

#: The file that `index_scope` publishes in the index directory.
_STORE_FILE: Final = "store.db"

#: The row of `meta` that holds the time of a run. No vector holds it.
_UPDATED_AT: Final = "updated_at"

#: The tables whose statement a vector holds. A reader of a store names each
#: one. `chunks_vec` has no reader outside the index builder.
_TABLES: Final = ("meta", "files", "chunks", "chunks_fts", "chunks_emb")


class Stub:
    """An embedder with no network. It counts the texts of each call."""

    def __init__(self, model: str, vector_of: Callable[[str], list[float]]) -> None:
        self.model = model
        self.batches: list[int] = []
        self._vector_of = vector_of

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.batches.append(len(texts))

        return [self._vector_of(text) for text in texts]


def _select(store: Path, statement: str) -> list[tuple[object, ...]]:
    """The rows of one statement. The connection loads no extension."""
    connection = sqlite3.connect(store)
    try:
        return cast("list[tuple[object, ...]]", connection.execute(statement).fetchall())
    finally:
        connection.close()


def _meta(store: Path) -> list[list[object]]:
    """Each row of `meta` but the time of the run, in the order of the keys."""
    rows = _select(store, "SELECT key, value FROM meta ORDER BY key")
    if _UPDATED_AT not in {row[0] for row in rows}:
        raise ValueError("a store with no time of its run")

    return [list(row) for row in rows if row[0] != _UPDATED_AT]


# --- one vector to its bytes in the store ---------------------------------------

#: The largest float32: each bit of the fraction is set.
F32_MAX: Final = (2.0 - 2.0**-23) * 2.0**127

#: The middle between the largest float32 and the next power of two. This
#: value and each larger one have no float32.
F32_PAST_MAX: Final = F32_MAX + 2.0**103

#: The smallest float32 above zero. It is a subnormal number.
F32_TINY: Final = 2.0**-149

#: The value of each place that a vector does not name.
FILL: Final = 0.25

#: The corpus of each vector: one file with one chunk.
_ONE_FILE: Final = "a.md"
_ONE_CHUNK: Final = "one chunk"

#: What the interpreter says for a value that has no float32.
_NO_FLOAT32: Final = "too large"

#: The refusal of a vector whose file `index_scope` reports as an error.
FILE_ERROR: Final = "file_error"

#: The input form of bytes that are not UTF-8. The output of each vector has
#: this form: the fill of a vector makes its bytes no UTF-8 text.
_BASE64: Final = "base64"


@dataclass(frozen=True)
class Values:
    """One vector of an embedder: its first values, then `fill` in each other place."""

    id: str
    first: tuple[float, ...]
    fill: float = FILL

    def vector(self) -> list[float]:
        return [*self.first, *(self.fill for _ in range(DIMS - len(self.first)))]


EMBEDDINGS: Final[tuple[Values, ...]] = (
    Values("tenth", (0.1,)),
    Values("third", (1 / 3,)),
    Values("integer", (1,)),
    Values("integer-past-24-bits", (16777217,)),
    Values("zero", (0.0,)),
    Values("negative-zero", (-0.0,)),
    Values("several-values", (0.1, -0.2, 0.3, -0.4, 1, -1, 0.5), fill=0.0),
    Values("no-first-value", (), fill=-0.75),
    Values("tie-rounds-down-to-even", (1.0 + 2.0**-24,)),
    Values("tie-rounds-up-to-even", (1.0 + 3 * 2.0**-24,)),
    # --- the largest values ---
    Values("largest-float32", (F32_MAX,)),
    Values("negative-largest-float32", (-F32_MAX,)),
    Values("rounds-down-to-the-largest-float32", (F32_PAST_MAX - 2.0**75,)),
    Values("halfway-past-the-largest-float32", (F32_PAST_MAX,)),
    Values("too-large", (1e39,)),
    Values("negative-too-large", (-1e39,)),
    Values("too-large-after-the-first-value", (0.1,), fill=1e39),
    Values("largest-float64", (1.7976931348623157e308,)),
    # --- the smallest values ---
    Values("smallest-normal", (2.0**-126,)),
    Values("largest-subnormal", (2.0**-126 - F32_TINY,)),
    Values("smallest-subnormal", (F32_TINY,)),
    Values("negative-smallest-subnormal", (-F32_TINY,)),
    Values("half-the-smallest-subnormal", (F32_TINY / 2,)),
    Values("above-half-the-smallest-subnormal", (F32_TINY / 2 + 2.0**-202,)),
    Values("below-each-float32", (1e-46,)),
    Values("negative-below-each-float32", (-1e-46,)),
    Values("smallest-float64", (5e-324,)),
)


def _embedding_vector(values: Values, scratch: Path) -> Vector:
    args = {"first": values.first, "fill": values.fill}
    given: dict[str, Json] = {"args": normalize(args)}
    scope = scratch / values.id / _SCOPE_DIR
    index_dir = scratch / values.id / _INDEX_DIR
    scope.mkdir(parents=True)
    (scope / _ONE_FILE).write_bytes(_ONE_CHUNK.encode("utf-8"))
    vector = values.vector()
    stub = Stub(MODEL, lambda _text: vector)
    outcome = attempt(lambda: index_scope(scope, stub, index_dir))
    if isinstance(outcome, Raised):
        return raised(values.id, given, outcome.exc)

    blobs = [row[0] for row in _select(index_dir / _STORE_FILE, "SELECT embedding FROM chunks_emb")]
    if outcome.errors:
        texts = list(outcome.errors.values())
        if blobs or len(texts) != 1 or _NO_FLOAT32 not in texts[0]:
            raise ValueError(f"{values.id}: an error that is not the error of a value")

        return refused(values.id, given, FILE_ERROR)

    if len(blobs) != 1 or not isinstance(blobs[0], bytes):
        raise ValueError(f"{values.id}: the store does not hold one vector")

    output = bytes_input(blobs[0])
    if _BASE64 not in output:
        raise ValueError(f"{values.id}: the bytes are UTF-8, so the output has two forms")

    return accepted(values.id, given, output=output)


def _embedding_surface(scratch: Path) -> Surface:
    return Surface(
        name=f"{GROUP}.embedding",
        path=f"{GROUP}/embedding.json",
        entry="library.library.index_scope",
        contract=STORE_CONTRACT,
        notes=(
            f"The input is one vector of {DIMS} values, as args. args.first holds the first "
            "values. Each other value is args.fill. A value is a float or an integer.",
            f"The corpus is one file, {_ONE_FILE}, with the text '{_ONE_CHUNK}'. The "
            "embedder is a stub that returns the vector for that one chunk.",
            "output is the bytes of the column embedding of the one row of chunks_emb: "
            f"{DIMS * 4} bytes. The bytes of each vector are not UTF-8, so output has the "
            f"form {_BASE64}.",
            "A refused vector holds a value that has no float32. index_scope then reports "
            f"the file as an error, and chunks_emb holds no row. refusal is {FILE_ERROR}.",
        ),
        vectors=tuple(_embedding_vector(values, scratch) for values in EMBEDDINGS),
    )


# --- the schema -----------------------------------------------------------------

MODELS: Final[tuple[tuple[str, str], ...]] = (
    ("model-of-the-tests", MODEL),
    ("model-unknown", "unknown"),
    ("model-with-quotes", 'it\'s a "model"'),
    ("model-not-ascii", "mod\u00e8le-\u65e5\u672c"),
)


def _zeros(_text: str) -> list[float]:
    return [0.0] * DIMS


def _schema_vector(vector_id: str, model: str, scratch: Path) -> Vector:
    given: dict[str, Json] = {"args": {"model": model}}
    scope = scratch / vector_id / _SCOPE_DIR
    index_dir = scratch / vector_id / _INDEX_DIR
    scope.mkdir(parents=True)
    outcome = attempt(lambda: index_scope(scope, Stub(model, _zeros), index_dir))
    if isinstance(outcome, Raised):
        return raised(vector_id, given, outcome.exc)

    store = index_dir / _STORE_FILE
    rows = _select(store, "SELECT name, sql FROM sqlite_master WHERE type = 'table'")
    found = {str(name): sql for name, sql in rows if name in _TABLES}
    if set(found) != set(_TABLES):
        raise ValueError(f"{vector_id}: the store lacks a table")

    return accepted(vector_id, given, {"tables": found, "meta": _meta(store)})


def _schema_surface(scratch: Path) -> Surface:
    return Surface(
        name=f"{GROUP}.schema",
        path=f"{GROUP}/schema.json",
        entry="library.library.index_scope",
        contract=STORE_CONTRACT,
        notes=(
            "The input is args.model: the model of a stub embedder. The corpus is an empty "
            "directory, and no store exists before the run.",
            "value.tables holds the statement that sqlite_master keeps for each of five "
            f"tables: {', '.join(_TABLES)}. It holds no statement of chunks_vec, and none "
            "of a table that a virtual table makes for itself.",
            "value.meta holds each row of meta as a pair of key and value, in the order of "
            f"the keys. The store also holds the row {_UPDATED_AT}. Its value is the time of "
            "the run, and no vector holds it.",
        ),
        vectors=tuple(_schema_vector(vector_id, model, scratch) for vector_id, model in MODELS),
    )


# --- a corpus and its runs ------------------------------------------------------

#: The second model of a vector that changes the model.
OTHER_MODEL: Final = "other-model"

#: The four kinds of step.
RUN: Final = "run"
WRITE: Final = "write"
REMOVE: Final = "remove"
MODEL_STEP: Final = "model"

type Step = dict[str, str]


def _run() -> Step:
    return {"do": RUN}


def _write(path: str, text: str) -> Step:
    return {"do": WRITE, "path": path, "text": text}


def _remove(path: str) -> Step:
    return {"do": REMOVE, "path": path}


def _model(name: str) -> Step:
    return {"do": MODEL_STEP, "name": name}


@dataclass(frozen=True)
class Scenario:
    """One corpus and what happens to it: the files at the start, then the steps."""

    id: str
    files: tuple[tuple[str, str], ...]
    steps: tuple[Step, ...]
    profile: Corpus = Corpus.VAULT


def _paragraph(number: int) -> str:
    """A paragraph of 501 code points. Two of them do not fit one chunk."""
    return "x" * 400 + "y" * number + "z" * (101 - number)


#: A text of one chunk more than one call of the embedder takes.
_MANY_CHUNKS: Final = "\n\n".join(_paragraph(number) for number in range(EMBED_MAX_BATCH + 1))

#: Five names whose order as paths differs from their order as texts. `B.md`
#: sorts before `a.md` by code point.
_FIVE: Final = (
    ("a/b.md", "The file in the directory a."),
    ("a-b/c.md", "The file in the directory a-b."),
    ("a b.md", "The file with a space in its name."),
    ("B.md", "The file with an upper-case name."),
    ("a.md", "The first paragraph of a.md.\n\nThe second paragraph of a.md."),
)

SCENARIOS: Final[tuple[Scenario, ...]] = (
    Scenario("five-names-two-runs", _FIVE, (_run(), _run())),
    Scenario(
        "write-remove-and-add",
        _FIVE[:4],
        (
            _run(),
            _write("a.md", "A new file."),
            _write("B.md", "The file with an upper-case name, with a new text."),
            _write("a b.md", "The file with a space in its name."),
            _remove("a-b/c.md"),
            _run(),
        ),
    ),
    Scenario(
        "remove-each-file-then-add",
        (("a.md", "The first file."), ("B.md", "The second file.")),
        (_run(), _remove("a.md"), _remove("B.md"), _run(), _write("a/b.md", "A new file."), _run()),
    ),
    Scenario(
        "model-change",
        (("a.md", "The first file."), ("B.md", "The second file.")),
        (_run(), _model(OTHER_MODEL), _run(), _run()),
    ),
    Scenario(
        "model-change-with-a-removed-file",
        (("a.md", "The first file."), ("B.md", "The second file.")),
        (_run(), _remove("B.md"), _model(OTHER_MODEL), _run()),
    ),
    Scenario(
        "file-of-a-batch-and-one-chunk",
        (("a.md", _MANY_CHUNKS), ("B.md", "The file before the long file.")),
        (_run(), _remove("a.md"), _run()),
    ),
    Scenario(
        "files-with-no-chunk",
        (("a.md", ""), ("B.md", " \n\n\t\n"), ("a b.md", "The one file with a chunk.")),
        (
            _run(),
            _run(),
            _write("a b.md", ""),
            _write("a.md", "The file has a text now."),
            _run(),
        ),
    ),
    Scenario(
        "equal-text-in-two-files",
        (("a.md", "The same text."), ("B.md", "The same text.")),
        (_run(), _remove("a.md"), _run()),
    ),
    Scenario(
        "crlf-file",
        (("a.md", "first\r\n\r\nsecond\r\n"),),
        (_run(), _write("a.md", "first\n\nsecond\n"), _run()),
    ),
    Scenario(
        "names-of-the-vault-profile",
        (
            ("a.md", "A note."),
            ("notes.txt", "A text file."),
            ("UPPER.MD", "A suffix in upper case."),
            ("a.b/c.txt", "A directory with a dot inside its name."),
            ("node_modules/pkg.md", "A directory that only the code profile skips."),
            ("script.py", "print('no prose')\n"),
            ("README", "A name with no suffix."),
            ("archive.md.bak", "A second suffix."),
            (".hidden.md", "A file whose name starts with a dot."),
            ("a/.hidden.md", "The same, in a directory."),
            (".git/config.md", "A directory whose name starts with a dot."),
            (".index/a.md", "The directory of an old index."),
            ("review-inbox/draft.md", "A draft that no person approved."),
            ("a/review-inbox/draft.md", "The same, in a directory."),
        ),
        (_run(),),
    ),
    Scenario(
        "names-of-the-code-profile",
        (
            ("src/app.py", "def handler():\n    return 'ok'\n"),
            ("src/client.ts", "export const call = () => fetch('/v1');\n"),
            ("README.md", "The service has one route.\n"),
            ("MAIN.RS", "fn main() {}\n"),
            ("review-inbox/draft.md", "A directory that only the vault profile skips."),
            ("notes.pdf", "A suffix of the vault profile only."),
            ("Makefile", "all:\n"),
            ("node_modules/pkg/index.js", "// generated\n"),
            ("src/node_modules/index.js", "// generated\n"),
            ("target/debug.rs", "// generated\n"),
            ("dist/bundle.js", "// generated\n"),
            ("build/out.js", "// generated\n"),
            ("__pycache__/app.py", "# generated\n"),
            (".git/config.toml", "# not source\n"),
            (".index/a.md", "The directory of an old index."),
        ),
        (_run(),),
        profile=Corpus.CODE,
    ),
)


def _stand_in_vector(text: str) -> list[float]:
    """The vector of a text. The notes of `library.index_scope` give the formula.

    The TEI stand-in of the process-level suite uses the same formula.
    """
    total = sum(ord(char) for char in text)

    return [((total * (place + 1)) % 1000) / 1000 - 0.5 for place in range(DIMS)]


def _check_names(scenario: Scenario) -> None:
    """Each name must give the same corpus on each file system.

    A file system can fold the case of a name, change the form of a
    character that is not ASCII, and drop a final dot.
    """
    paths = [path for path, _ in scenario.files]
    paths += [step["path"] for step in scenario.steps if "path" in step]
    entries: set[tuple[str, ...]] = set()
    for path in paths:
        parts = tuple(path.split("/"))
        entries.update(parts[:count] for count in range(1, len(parts) + 1))

    folded = {tuple(name.casefold() for name in entry) for entry in entries}
    if len(folded) != len(entries):
        raise ValueError(f"{scenario.id}: two names of one directory differ only in case")

    names = {name for entry in entries for name in entry}
    if not all(name.isascii() and name and not name.endswith(".") for name in names):
        raise ValueError(f"{scenario.id}: a name that a file system can change")


def _relative(path: object, scope: Path) -> str:
    return Path(str(path)).relative_to(scope).as_posix()


def _run_value(report: IndexReport, batches: list[int], store: Path, scope: Path) -> object:
    """What one run did: the counts of its report, and the rows of the store."""
    files = _select(store, "SELECT path, hash FROM files ORDER BY path")
    chunks = _select(store, "SELECT id, path, ord, text FROM chunks ORDER BY id")
    vectors = _select(store, "SELECT id, embedding FROM chunks_emb ORDER BY id")

    return {
        "report": {
            "indexed": len(report.indexed),
            "unchanged": report.unchanged,
            "removed": len(report.removed),
            "chunks": report.chunks,
            "errors": len(report.errors),
            "rebuilt": report.rebuilt,
        },
        "batches": batches,
        "meta": _meta(store),
        "files": [[_relative(path, scope), digest] for path, digest in files],
        "chunks": [
            [chunk_id, _relative(path, scope), place, text]
            for chunk_id, path, place, text in chunks
        ],
        "chunks_emb": [list(row) for row in vectors],
    }


def _put(scope: Path, path: str, text: str) -> None:
    target = scope / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(text.encode("utf-8"))


def _scenario_vector(scenario: Scenario, scratch: Path) -> Vector:
    _check_names(scenario)
    args = {"model": MODEL, "files": scenario.files, "steps": scenario.steps}
    given: dict[str, Json] = {"args": normalize(args)}
    params = {"profile": scenario.profile}
    scope = (scratch / scenario.id / _SCOPE_DIR).resolve()
    index_dir = scratch / scenario.id / _INDEX_DIR
    scope.mkdir(parents=True)
    for path, text in scenario.files:
        _put(scope, path, text)

    stub = Stub(MODEL, _stand_in_vector)
    profile = PROFILES[scenario.profile]
    runs: list[object] = []
    for step in scenario.steps:
        if step["do"] == WRITE:
            _put(scope, step["path"], step["text"])
        elif step["do"] == REMOVE:
            (scope / step["path"]).unlink()
        elif step["do"] == MODEL_STEP:
            stub.model = step["name"]
        elif step["do"] != RUN:
            raise ValueError(f"{scenario.id}: a step of no kind")
        else:
            stub.batches = []
            outcome = attempt(lambda: index_scope(scope, stub, index_dir, profile=profile))
            if isinstance(outcome, Raised):
                return raised(scenario.id, given, outcome.exc, params=params)

            runs.append(_run_value(outcome, stub.batches, index_dir / _STORE_FILE, scope))

    return accepted(scenario.id, given, {"runs": runs}, params=params)


def _index_surface(scratch: Path) -> Surface:
    return Surface(
        name=f"{GROUP}.index_scope",
        path=f"{GROUP}/index_scope.json",
        entry="library.library.index_scope",
        contract=STORE_CONTRACT,
        notes=(
            "The input is a corpus and what happens to it, as args. args.files is a list of "
            "pairs: the path of a file from the scope, then its text. The generator writes "
            "the UTF-8 bytes of the text. params.profile is the profile: vault or code.",
            "args.steps is a list of steps, in order. A step is an object, and its key do "
            f"names its kind. {WRITE} writes the text to the path. {REMOVE} removes the "
            f"file of the path. {MODEL_STEP} gives the embedder the model of the key name. "
            f"{RUN} calls the entry point. The index directory is outside the scope, and "
            "each run of a vector uses the same one.",
            "The embedder is a stub with the model args.model at the start. Let s be the "
            "sum of the code points of a text. Value i of the vector of that text is "
            f"((s * (i + 1)) % 1000) / 1000 - 0.5, for i from 0 to {DIMS - 1}.",
            f"value.runs holds one item for each step {RUN}, in order. report holds the "
            "counts of the report of that run. batches holds the count of texts of each "
            "call of the embedder in that run, in order.",
            "The other keys of an item hold rows of the store after that run. meta holds "
            f"each row of meta but {_UPDATED_AT}, as pairs in the order of the keys. files "
            "holds the path and the hash of each row, in the order of the paths as bytes. "
            "chunks holds id, path, ord and text of each row, in the order of the ids. "
            "chunks_emb holds id and embedding of each row, in the order of the ids.",
            "A path of a row is a path from the scope. The store holds the full path of the "
            "scope before it. No vector holds a time of a row.",
        ),
        vectors=tuple(_scenario_vector(scenario, scratch) for scenario in SCENARIOS),
    )


def surfaces() -> tuple[Surface, ...]:
    with tempfile.TemporaryDirectory(prefix="vectors-library-") as scratch_name, quiet_logs():
        scratch = Path(scratch_name)

        return (
            _chunk_surface(),
            _hash_surface(scratch / "hash"),
            _read_surface(scratch / "read"),
            _report_surface(),
            _tei_url_surface(),
            _embedding_surface(scratch / "embedding"),
            _schema_surface(scratch / "schema"),
            _index_surface(scratch / "index"),
        )
