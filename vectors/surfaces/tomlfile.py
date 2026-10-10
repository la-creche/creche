"""The syntax of a TOML file (the group `tomlfile`).

One surface, `tomlfile.syntax`: the text of one file to the table that a
reader gives, or to a refusal. Four file kinds are TOML texts: the family
file, the server file, the component manifest and the roster. Each reader of
such a file takes the same texts, and the Rust reader of those kinds walks
this surface.

The entry point is `tomllib.loads` of the Python standard library. No
product code reads TOML yet (`vectors/AGENTS.md`, "Known gaps").

Four kinds of text are in no list here. `tomllib` does not give the answer
of a reader of a file kind for them, or its answer changes with the Python
version (`vectors/AGENTS.md`, rule 7). A plain Rust test holds each kind:

1. A form that TOML 1.1 added. A reader of a file kind takes TOML 1.0.
2. An integer outside 64 bits.
3. A float whose text is outside the range of a float, for example `1e999`.
4. A text with more than 8 levels.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Final, cast

from vectors.core import (
    ACCEPTED,
    REFUSED,
    Json,
    Raised,
    Surface,
    Vector,
    accepted,
    attempt,
    raised,
    refused,
    text_input,
)

GROUP: Final = "tomlfile"

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


#: Texts that a reader takes.
TAKEN: Final[tuple[Text, ...]] = (
    Text("empty", ""),
    Text("line-end-only", "\n"),
    Text("comment-only", "# a comment\n"),
    Text("no-final-line-end", "a = 1"),
    Text("line-ends-cr-lf", "a = 1\r\nb = 2\r\n"),
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
    # --- a date-time, which a vector holds by its kind ---
    Text("offset-date-time", "a = 1979-05-27T07:32:00Z\nb = 1979-05-27 07:32:00.5-07:00\n"),
    Text("local-date-time", "a = 1979-05-27T07:32:00\n"),
    Text("local-date", "a = 1979-05-27\n"),
    Text("local-time", "a = 07:32:00\nb = 23:59:59.999999\n"),
    # --- the levels ---
    Text("levels-8-of-tables", "[a.b.c.d.e.f.g]\nh = 1\n"),
    Text("levels-8-of-lists", "a = [[[[[[[1]]]]]]]\n"),
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
)


def kind_of(moment: date | time) -> str:
    """The kind of one date-time of a TOML text."""
    if isinstance(moment, datetime):
        return LOCAL_DATE_TIME if moment.tzinfo is None else OFFSET_DATE_TIME

    return LOCAL_DATE if isinstance(moment, date) else LOCAL_TIME


def plain(value: object) -> object:
    """A value of `tomllib`, with each date-time as its kind.

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


def vector_of(text: Text, wanted: str) -> Vector:
    """The vector of one text. `wanted` is the result that the list of the text names.

    Another result stops the build: a list then no longer says what a reader
    does with its texts.
    """
    given: dict[str, Json] = text_input(text.text)
    outcome = attempt(lambda: tomllib.loads(text.text))
    if isinstance(outcome, Raised):
        if not isinstance(outcome.exc, tomllib.TOMLDecodeError):
            return raised(text.id, given, outcome.exc)

        vector = refused(text.id, given)
    else:
        vector = accepted(text.id, given, plain(outcome))

    if vector.body["result"] != wanted:
        raise ValueError(f"{GROUP}: the entry point gives the text {text.id} another result")

    return vector


def surfaces() -> tuple[Surface, ...]:
    tail = "syntax"

    return (
        Surface(
            name=f"{GROUP}.{tail}",
            path=f"{GROUP}/{tail}.json",
            entry="tomllib.loads",
            contract="TOML 1.0.0",
            notes=(
                "The input is the text of one file. The entry point takes that text. A reader "
                "that takes bytes gets the UTF-8 bytes of the text.",
                "The entry point is the TOML reader of the Python standard library. No product "
                "code reads TOML yet.",
                "An accepted vector is a text that the entry point reads. value is the table "
                "that it gives.",
                "A refused vector is a text for which the entry point raises TOMLDecodeError. No "
                "vector holds the message.",
                "A date-time in value is an object with one key and the value null. The key is "
                f"the kind of the date-time: {OFFSET_DATE_TIME}, {LOCAL_DATE_TIME}, {LOCAL_DATE} "
                f"or {LOCAL_TIME}. TOML has no null, so no table of a text has that form. No "
                "vector holds a part of the value of a date-time.",
                "A float that is not finite is a $float marker. The sign of a zero is a part of "
                "a float: compare 0.0 and -0.0 as two values.",
                "No vector holds a form that TOML 1.1 added, an integer outside 64 bits, a float "
                "whose text is outside the range of a float or a text with more than 8 levels. "
                "A reader of a file kind refuses each one.",
            ),
            vectors=(
                *(vector_of(text, ACCEPTED) for text in TAKEN),
                *(vector_of(text, REFUSED) for text in NOT_TAKEN),
            ),
        ),
    )
