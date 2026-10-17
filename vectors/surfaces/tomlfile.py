"""The syntax of a TOML file (the group `tomlfile`).

One surface, `tomlfile.syntax`: the bytes of one file to the table that the
reader of a file kind gives, or to its refusal. Four file kinds are TOML
texts: the family file, the server file, the component manifest and the
roster. Each reader of such a file takes the same texts, and the Rust reader
of those kinds walks this surface.

The entry point is `agent_family.tomlfile.loads` with the file kind
`ROSTER`, the kind with the largest byte limit. A refused vector holds the
text of the refusal: one of the four fixed texts of the reader.

The surface holds four groups of inputs, in this order:

1. `TAKEN`: written texts that the reader takes.
2. `NOT_TAKEN`: written texts that the reader refuses.
3. `LIMITS`: the byte limit of the kind and the encoding, each as bytes
   that are written short.
4. The changed texts: `changed_texts(SEED, CHANGED_COUNT)`. Each one is a
   text of `TAKEN` with one change.

A change that a person makes to `TAKEN` moves each changed text, because the
changed texts start from that list.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import Enum
from typing import Final, cast

from agent_family import tomlfile
from agent_family.tomlfile import FileKind, TomlRefusal

from vectors.core import (
    ACCEPTED,
    REFUSED,
    Json,
    Raised,
    Surface,
    Vector,
    accepted,
    attempt,
    bytes_input,
    expand,
    refused,
    repeat_input,
    text_input,
)

GROUP: Final = "tomlfile"

#: The file kind that the entry point reads. It has the largest byte limit.
KIND: Final = FileKind.ROSTER

#: The four kinds of a date-time in a TOML text. A vector holds the kind of
#: a date-time and no part of its value.
OFFSET_DATE_TIME: Final = "offset date-time"
LOCAL_DATE_TIME: Final = "local date-time"
LOCAL_DATE: Final = "local date"
LOCAL_TIME: Final = "local time"


@dataclass(frozen=True)
class Text:
    """One text of a file: an id and the text."""

    id: str
    text: str


@dataclass(frozen=True)
class Parts:
    """One long text of a file, written short: each part is a text and a count.

    The text is each part, repeated its count of times, joined in order."""

    id: str
    parts: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class Raw:
    """Bytes of a file that are no UTF-8 text."""

    id: str
    data: bytes


#: One input of the surface.
type Input = Text | Parts | Raw


#: Texts that a reader takes.
TAKEN: Final[tuple[Text, ...]] = (
    Text("empty", ""),
    Text("line-end-only", "\n"),
    Text("comment-only", "# a comment\n"),
    Text("no-final-line-end", "a = 1"),
    Text("line-ends-cr-lf", "a = 1\r\nb = 2\r\n"),
    Text("cr-lf-in-a-multi-line-basic-string", 'a = """one\r\ntwo"""\r\n'),
    Text("cr-lf-in-a-multi-line-literal-string", "a = '''one\r\ntwo'''\r\n"),
    Text("cr-lf-after-the-opening-quotes", 'a = """\r\none"""\r\n'),
    Text("keys-not-in-sorted-order", "zeta = 1\nalpha = 2\nmiddle = 3\n"),
    Text("key-forms", 'bare-key_1 = 1\n"quoted key" = 2\n\'literal.key\' = 3\n"" = 4\n1 = 5\n'),
    Text("dotted-keys", "a.b.c = 1\na.b.d = 2\na.e = 3\n"),
    Text(
        "tables",
        '[server]\nname = "one"\n\n[server.limits]\ncalls = 5\n\n[other]\nflag = true\n',
    ),
    Text(
        "list-of-tables",
        '[[rows]]\nname = "a"\n\n[[rows]]\nname = "b"\n\n[[rows.fences]]\ntool = "t"\n',
    ),
    Text("dotted-key-in-a-new-item-of-a-list", "[[a]]\n\n[[a.b]]\n\n[[a]]\nb.c = 1\n"),
    Text("inline-table", "a = {b = 1, c = {d = 2}, e = []}\n"),
    Text("list-of-each-kind", 'a = [1, "two", 3.0, true, [4], {five = 5}]\n'),
    Text("list-on-more-lines", "a = [\n  1, # a comment\n  2,\n]\n"),
    Text("booleans", "a = true\nb = false\n"),
    # --- a text ---
    Text("basic-string", 'a = "one \\"two\\" \\\\ \\t \\u00e9 \\U0001F600"\n'),
    Text("literal-string", "a = 'one \\n \"two\"'\n"),
    Text("multi-line-basic-string", 'a = """\nfirst\nsecond \\\n     third "" end"""\n'),
    Text("multi-line-literal-string", "a = '''\nfirst\\n\nsecond '' end'''\n"),
    Text("empty-strings", "a = \"\"\nb = ''\nc = \"\"\"\"\"\"\nd = ''''''\n"),
    Text("text-outside-ascii", 'a = "caf\u00e9 \U0001f600"\n'),
    # --- the signs of the forms that TOML 1.1 added, where they start no form ---
    Text(
        "toml11-signs-in-a-basic-string",
        'a = "{b = 1,} 07:32 1979-05-27T07:32Z \\\\e \\\\x41"\n',
    ),
    Text(
        "toml11-signs-in-a-literal-string",
        "a = '{b = 1,} 07:32 1979-05-27T07:32Z \\e \\x41'\n",
    ),
    Text("toml11-signs-in-a-multi-line-basic-string", 'a = """{b = 1,\n} 07:32"""\n'),
    Text(
        "toml11-signs-in-a-multi-line-literal-string",
        "a = '''{b = 1,\n} 07:32 \\e \\x41'''\n",
    ),
    Text("toml11-signs-in-a-quoted-key", '"{b = 1,} 07:32" = 1\n'),
    Text(
        "toml11-signs-in-a-comment",
        "a = 1 # {b = 1,} 07:32 1979-05-27T07:32Z \\e \\x41\n# {c = 2,\n",
    ),
    Text(
        "toml11-signs-in-a-string-of-a-list-of-inline-tables",
        "a = [{b = \"{c = 1,} 07:32 \\\\e\"}, {d = '\\x41 1979-05-27T07:32Z'}]\n",
    ),
    Text(
        "toml11-signs-in-a-comment-of-a-list-of-inline-tables",
        "a = [\n  {b = 1}, # {c = 1,} 07:32 \\e \\x41\n  {d = 2},\n]\n",
    ),
    # --- an integer ---
    Text("integers", "a = 0\nb = +7\nc = -7\nd = 1_000\n"),
    Text("integers-of-each-base", "a = 0xDEAD_beef\nb = 0o17\nc = 0b101\n"),
    Text("integer-largest", "a = 9223372036854775807\n"),
    Text("integer-least", "a = -9223372036854775808\n"),
    # --- a float ---
    Text("floats", "a = 1.5\nb = -2e3\nc = 6.02E+23\nd = 1_0.2_5\n"),
    Text("float-inf", "a = inf\nb = +inf\nc = -inf\n"),
    Text("float-nan", "a = nan\nb = +nan\nc = -nan\n"),
    Text("float-zeros", "a = 0.0\nb = -0.0\nc = +0.0\n"),
    Text("float-and-integer", "a = 1\nb = 1.0\n"),
    Text("float-largest", "a = 1.7976931348623157e308\nb = -1.7976931348623157e308\n"),
    Text("float-that-rounds-to-zero", "a = 1e-999\nb = -1e-999\n"),
    # --- a date-time, which a vector holds by its kind ---
    Text("offset-date-time", "a = 1979-05-27T07:32:00Z\nb = 1979-05-27 07:32:00.5-07:00\n"),
    Text("local-date-time", "a = 1979-05-27T07:32:00\n"),
    Text("local-date", "a = 1979-05-27\n"),
    Text("local-time", "a = 07:32:00\nb = 23:59:59.999999\n"),
    # --- the levels: 8 is the limit ---
    Text("levels-8-of-tables", "[a.b.c.d.e.f.g]\nh = 1\n"),
    Text("levels-8-of-lists", "a = [[[[[[[1]]]]]]]\n"),
    Text("levels-8-of-inline-tables", "a = {b = {c = {d = {e = {f = {g = {h = 1}}}}}}}\n"),
    Text("levels-8-of-a-dotted-key", "a.b.c.d.e.f.g.h = 1\n"),
)

#: Texts that a reader refuses.
NOT_TAKEN: Final[tuple[Text, ...]] = (
    # --- a key two times, in each of five forms ---
    Text("key-two-times", "a = 1\na = 2\n"),
    Text("table-two-times", "[a]\nb = 1\n\n[a]\nc = 2\n"),
    Text("dotted-key-over-a-value", "a = 1\na.b = 2\n"),
    Text("key-two-times-in-an-inline-table", "a = {b = 1, b = 2}\n"),
    Text("table-over-a-scalar", "a = 1\n\n[a]\nb = 2\n"),
    Text("key-two-times-in-two-forms", 'a = 1\n"a" = 2\n'),
    Text("inline-table-with-a-later-key", "a = {b = 1}\na.c = 2\n"),
    Text("list-with-a-later-table", "a = []\n\n[[a]]\nb = 1\n"),
    # --- a dotted key and a list of tables ---
    Text("dotted-key-over-a-list-of-tables", "[[a.b]]\n\n[a]\nb.c = 1\n"),
    Text("dotted-key-through-a-list-of-tables", "[[a.b]]\n\n[a]\nb.c.d = 1\n"),
    Text(
        "dotted-key-through-a-list-below-a-list",
        "[[a]]\n\n[[a.b.c]]\n\n[a.b]\nc.d.e = 1\n",
    ),
    # --- the encoding ---
    Text("byte-order-mark", "\ufeffa = 1\n"),
    Text("byte-order-mark-only", "\ufeff"),
    Text("lone-surrogate-escape", 'a = "\\ud800"\n'),
    # --- what a YAML text can hold ---
    Text("yaml-mapping", "a: 1\n"),
    Text("yaml-anchor", "a = &anchor 1\n"),
    Text("yaml-alias", "a = *anchor\n"),
    Text("yaml-tag", "a = !!str 1\n"),
    Text("yaml-anchor-and-alias", "a: &anchor 1\nb: *anchor\n"),
    Text("null-word", "b = null\n"),
    Text("tilde", "b = ~\n"),
    Text("yes-word", "b = yes\n"),
    Text("integer-with-a-zero-first", "i = 010\n"),
    # --- a date-time that the time types of Python do not hold ---
    Text("second-60", "a = 23:59:60\n"),
    Text("second-60-in-a-date-time", "a = 1979-05-27T23:59:60Z\n"),
    Text("year-0000", "a = 0000-01-01\n"),
    Text("year-0000-in-a-date-time", "a = 0000-12-31T23:59:59Z\n"),
    # --- a text that is no TOML ---
    Text("no-value", "a =\n"),
    Text("no-key", "= 1\n"),
    Text("word-with-no-quotes", "a = word\n"),
    Text("two-values-on-one-line", "a = 1 b = 2\n"),
    Text("string-with-no-end", 'a = "one\n'),
    Text("line-end-in-a-basic-string", 'a = "one\ntwo"\n'),
    Text("escape-with-no-meaning", 'a = "\\q"\n'),
    Text("control-character-in-a-string", 'a = "one\x01two"\n'),
    Text("table-header-with-no-end", "[a\nb = 1\n"),
    Text("list-with-no-end", "a = [1, 2\n"),
    Text("inline-table-with-no-end", "a = {b = 1\n"),
    Text("date-that-no-month-has", "a = 1979-02-30\n"),
    # --- a CR that is no part of a CR LF ---
    Text("cr-before-a-cr-lf", "a = 1\r\r\n"),
    Text("cr-with-no-lf", "a = 1\rb = 2\n"),
    Text("cr-with-no-lf-in-a-multi-line-string", 'a = """one\rtwo"""\n'),
    # --- the six forms that TOML 1.1 added, at the top level ---
    Text("toml11-line-end-in-an-inline-table", "a = {b = 1,\n c = 2}\n"),
    Text("toml11-comma-before-the-end-of-an-inline-table", "a = {b = 1,}\n"),
    Text("toml11-escape-e", 'a = "\\e"\n'),
    Text("toml11-escape-x", 'a = "\\x41"\n'),
    Text("toml11-time-with-no-seconds", "a = 07:32\n"),
    Text("toml11-date-time-with-no-seconds", "a = 1979-05-27T07:32Z\n"),
    # --- the same six forms, inside a list of inline tables ---
    Text("toml11-line-end-in-a-list-of-inline-tables", "a = [{b = 1,\n c = 2}]\n"),
    Text("toml11-comma-in-a-list-of-inline-tables", "a = [{b = 1,}]\n"),
    Text("toml11-escape-e-in-a-list-of-inline-tables", 'a = [{b = "\\e"}]\n'),
    Text("toml11-escape-x-in-a-list-of-inline-tables", 'a = [{b = "\\x41"}]\n'),
    Text("toml11-time-in-a-list-of-inline-tables", "a = [{b = 07:32}]\n"),
    Text("toml11-date-time-in-a-list-of-inline-tables", "a = [{b = 1979-05-27T07:32Z}]\n"),
    # --- an integer outside 64 bits with a sign ---
    Text("integer-2-to-the-63", "a = 9223372036854775808\n"),
    Text("integer-below-the-least", "a = -9223372036854775809\n"),
    Text("integer-2-to-the-64", "a = 18446744073709551616\n"),
    # --- a float whose text is outside the range of a float ---
    Text("float-outside-the-range", "a = 1e999\n"),
    Text("float-outside-the-range-negative", "a = -1e999\n"),
    # --- the levels: 9 is over the limit ---
    Text("levels-9-of-tables", "[a.b.c.d.e.f.g.h]\ni = 1\n"),
    Text("levels-9-of-lists", "a = [[[[[[[[1]]]]]]]]\n"),
    Text("levels-9-of-inline-tables", "a = {b = {c = {d = {e = {f = {g = {h = {i = 1}}}}}}}}\n"),
    Text("levels-9-of-a-dotted-key", "a.b.c.d.e.f.g.h.i = 1\n"),
)

#: The text of a file of exactly the byte limit of the kind: one key, then
#: one comment that fills the rest.
_AT_THE_LIMIT_START: Final = "a = 1\n# "

#: The limits of the reader, each with the result that the reader gives.
LIMITS: Final[tuple[tuple[Input, str], ...]] = (
    (
        Parts(
            "bytes-at-the-limit",
            ((_AT_THE_LIMIT_START, 1), ("x", KIND.byte_limit - len(_AT_THE_LIMIT_START))),
        ),
        ACCEPTED,
    ),
    (
        Parts(
            "bytes-one-over-the-limit",
            ((_AT_THE_LIMIT_START, 1), ("x", KIND.byte_limit + 1 - len(_AT_THE_LIMIT_START))),
        ),
        REFUSED,
    ),
    (Raw("byte-that-is-no-utf8", b'a = "\xff"\n'), REFUSED),
    (Raw("bytes-of-a-surrogate", b'a = "\xed\xa0\x80"\n'), REFUSED),
    (Raw("overlong-form-of-a-slash", b'a = "\xc0\xaf"\n'), REFUSED),
    (Raw("byte-order-mark-and-no-utf8", b'\xef\xbb\xbfa = "\xff"\n'), REFUSED),
)

# --- the changed texts ---------------------------------------------------------

#: The seed of the changed texts in the committed file.
SEED: Final = 20261016

#: The count of the changed texts in the committed file.
CHANGED_COUNT: Final = 500

#: The characters that an insert takes: the signs of TOML, the two line end
#: characters, a space, a tab, three digits and eight letters.
ALPHABET: Final = "=.,[]{}\"'#\\:-+_ \t\n\r019aeinxzTZ"

#: The constants of the linear step of `Numbers`.
_MULTIPLIER: Final = 6364136223846793005
_INCREMENT: Final = 1442695040888963407
_MASK: Final = 2**64 - 1
_HIGH_BITS: Final = 32


class Numbers:
    """A fixed sequence of numbers: one 64-bit linear step for each number.

    The module `random` promises the same output under two Python versions
    only for `random()`, so the changed texts use this sequence."""

    def __init__(self, seed: int) -> None:
        self._state = seed & _MASK

    def below(self, bound: int) -> int:
        """The next number, from 0 to `bound` - 1."""
        self._state = (self._state * _MULTIPLIER + _INCREMENT) & _MASK
        return (self._state >> _HIGH_BITS) % bound


class Change(Enum):
    """The one change that makes a changed text."""

    DELETE = "delete one character"
    INSERT = "insert one character of the alphabet"
    SWAP = "swap two characters"
    COPY_LINE = "copy one line to another place"


#: The changes, in the order that a number selects.
CHANGES: Final = tuple(Change)

#: The least length of a text for a swap of two characters.
_SWAP_LENGTH: Final = 2


def changed(text: str, change: Change, numbers: Numbers) -> str:
    """`text` with one change. A text that is too short for the change gets
    an insert in its place: a delete needs one character, and a swap needs
    two. A swap of two equal characters makes no change, so it gets an
    insert in its place too."""
    if change is Change.COPY_LINE:
        lines = text.split("\n")
        line = lines[numbers.below(len(lines))]
        lines.insert(numbers.below(len(lines) + 1), line)
        return "\n".join(lines)

    if change is Change.DELETE and text:
        at = numbers.below(len(text))
        return text[:at] + text[at + 1 :]

    if change is Change.SWAP and len(text) >= _SWAP_LENGTH:
        first = numbers.below(len(text))
        second = (first + 1 + numbers.below(len(text) - 1)) % len(text)
        if text[first] != text[second]:
            signs = list(text)
            signs[first], signs[second] = signs[second], signs[first]
            return "".join(signs)

    at = numbers.below(len(text) + 1)
    sign = ALPHABET[numbers.below(len(ALPHABET))]
    return text[:at] + sign + text[at:]


def changed_texts(seed: int, count: int) -> tuple[Text, ...]:
    """`count` changed texts from `seed`. Each one is a text of `TAKEN` with
    one change. The first texts of a larger count are the texts of a smaller
    count, with the same ids."""
    numbers = Numbers(seed)
    texts: list[Text] = []
    for index in range(count):
        base = TAKEN[numbers.below(len(TAKEN))]
        change = CHANGES[numbers.below(len(CHANGES))]
        texts.append(Text(f"changed-{index:03d}", changed(base.text, change, numbers)))

    return tuple(texts)


# --- the vectors ---------------------------------------------------------------


def kind_of(moment: date | time) -> str:
    """The kind of one date-time of a TOML text."""
    if isinstance(moment, datetime):
        return LOCAL_DATE_TIME if moment.tzinfo is None else OFFSET_DATE_TIME

    return LOCAL_DATE if isinstance(moment, date) else LOCAL_TIME


def plain(value: object) -> object:
    """A value of the reader, with each date-time as its kind.

    A date-time is an object with one key and the value `null`. TOML has no
    null, so no table of a text has that form.
    """
    if isinstance(value, dict):
        table = cast("dict[str, object]", value)
        return {key: plain(item) for key, item in table.items()}

    if isinstance(value, list):
        return [plain(item) for item in cast("list[object]", value)]

    if isinstance(value, (date, time)):
        return {kind_of(value): None}

    return value


def given_of(item: Input) -> dict[str, Json]:
    """The input form of one input."""
    if isinstance(item, Text):
        return text_input(item.text)

    if isinstance(item, Parts):
        return repeat_input(item.parts)

    return bytes_input(item.data)


def bytes_of(item: Input) -> bytes:
    """The bytes that the entry point gets for one input."""
    if isinstance(item, Text):
        return item.text.encode("utf-8")

    if isinstance(item, Parts):
        return expand(item.parts).encode("utf-8")

    return item.data


def read(data: bytes) -> object:
    """The call of the entry point."""
    return tomlfile.loads(data, KIND)


def vector_of(item: Input, wanted: str | None, reader: Callable[[bytes], object] = read) -> Vector:
    """The vector of one input. `wanted` is the result that the list of the
    input names, or `None` for a changed text.

    Another result stops the build: a list then no longer says what a reader
    does with its texts. An exception other than `TomlRefusal` stops the
    build too, because no committed vector has the result `raised`
    (`vectors/AGENTS.md`, rule 5).
    """
    given = given_of(item)
    data = bytes_of(item)
    outcome = attempt(lambda: reader(data))
    if isinstance(outcome, Raised):
        if not isinstance(outcome.exc, TomlRefusal):
            name = type(outcome.exc).__name__
            raise ValueError(f"{GROUP}: the entry point raises {name} for the text {item.id}")

        vector = refused(item.id, given, str(outcome.exc))
    else:
        vector = accepted(item.id, given, plain(outcome))

    if wanted is not None and vector.body["result"] != wanted:
        raise ValueError(f"{GROUP}: the entry point gives the text {item.id} another result")

    return vector


def surfaces() -> tuple[Surface, ...]:
    tail = "syntax"

    return (
        Surface(
            name=f"{GROUP}.{tail}",
            path=f"{GROUP}/{tail}.json",
            entry="agent_family.tomlfile.loads",
            contract="TOML 1.0.0",
            notes=(
                "The input is the bytes of one file. A text input stands for its UTF-8 bytes. A "
                "base64 input holds bytes that are no UTF-8 text. A repeat input is a long text.",
                "The entry point is the TOML reader of a file kind, with the kind ROSTER. Its "
                f"byte limit is {KIND.byte_limit}, the largest limit of the four kinds.",
                "An accepted vector is a text that the entry point reads. value is the table "
                "that it gives.",
                "A refused vector holds the text of the refusal in refusal. It is one of four "
                "fixed texts, and it names no line, no column and no character of the file.",
                "A date-time in value is an object with one key and the value null. The key is "
                f"the kind of the date-time: {OFFSET_DATE_TIME}, {LOCAL_DATE_TIME}, {LOCAL_DATE} "
                f"or {LOCAL_TIME}. TOML has no null, so no table of a text has that form. No "
                "vector holds a part of the value of a date-time.",
                "A CR LF is one line end. In value, a CR LF inside a multi-line string is one "
                "LF. A CR with no LF after it is in no accepted text.",
                "A float that is not finite is a $float marker. The sign of a zero is a part of "
                "a float: compare 0.0 and -0.0 as two values.",
                f"The vectors changed-000 to changed-{CHANGED_COUNT - 1} are changed texts. Each "
                "one is an accepted text that a person wrote, with one change: a character less, "
                "one more character, two characters swapped, or one line copied to another "
                "place. A fixed seed makes them.",
            ),
            vectors=(
                *(vector_of(text, ACCEPTED) for text in TAKEN),
                *(vector_of(text, REFUSED) for text in NOT_TAKEN),
                *(vector_of(item, wanted) for item, wanted in LIMITS),
                *(vector_of(text, None) for text in changed_texts(SEED, CHANGED_COUNT)),
            ),
        ),
    )
