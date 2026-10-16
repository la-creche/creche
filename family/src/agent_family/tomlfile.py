"""The reader of a TOML file of one kind: the byte limits, the level limit,
the TOML 1.0 check and the four texts of a refusal.

`loads` takes the bytes of one file. It gives the top table, or it raises
`TomlRefusal` with one of four fixed texts. A text holds no line, no column
and no character of the file.

`loads` makes these checks, in this sequence:

1. The size. A file has the bytes of its kind at most.
2. The encoding. The bytes are UTF-8 in the strict form.
3. The start. The text starts with no byte order mark.
4. The parse of `tomllib`. Each `ValueError` and each `RecursionError` of
   the parser is a refusal. The parser reads a CR LF as one LF, also inside
   a multi-line string.
5. The TOML 1.0 check. `_only_toml10` refuses the six forms that TOML 1.1
   added. The `tomllib` of Python 3.15 reads those forms, so the check does
   not rely on the version of the parser.
6. The numbers. Each integer is in the range of 64 bits with a sign. A
   float whose text holds a digit has a finite value: `1e999` is a refusal,
   and `inf` and `nan` are not. The parse marks such a float, and this
   check refuses the mark.
7. The levels. The top table has level 1. A table or an array inside a
   table or an array of level n has level n + 1. No table and no array has
   a level above `DEPTH`. The walk uses a stack and no recursion.

The checks 4 to 7 give one text. The parser stops a deep text at a depth
that changes with the version of Python, so a syntax fault and a level fault
share that text.

No limit counts the values. A TOML text has no alias, so the byte limit
bounds the count, and the grammar of each file kind holds its own caps.

In the result of `loads`, the keys of the top table and of each table below
it are sorted by code point. A reader in another language that sorts the
same way thus gives the faults of one text in the same sequence.

The byte limits, the level limit and the four texts are copies. The Rust
reader of the same file kinds is their source, and a test holds each copy
equal to it."""

from __future__ import annotations

import math
import re
import tomllib
from enum import Enum
from typing import Any, Final, cast

#: The most levels of a text, for each file kind.
DEPTH: Final = 8

#: The four texts of a refusal. `TOO_LARGE` gets the limit of the kind.
TOO_LARGE: Final = "the file has more than {limit} bytes"
NOT_UTF8: Final = "the file is not UTF-8 text"
BYTE_ORDER_MARK: Final = "the file starts with a byte order mark"
NOT_TOML: Final = f"the text is not TOML 1.0, or it nests deeper than {DEPTH} levels"

#: The range of an integer: 64 bits with a sign.
_INTEGER_MIN: Final = -(2**63)
_INTEGER_MAX: Final = 2**63 - 1

_BYTE_ORDER_MARK: Final = "\ufeff"
_DIGIT: Final = re.compile("[0-9]")


class FileKind(Enum):
    """A kind of file that is a TOML text, with its byte limit.

    The set is closed. A caller cannot give `loads` another limit, and a
    caller cannot change the limit of a member. The value of a member is a
    word and a limit: the word keeps two members with one limit apart."""

    FAMILY = ("family", 65536)
    SERVER = ("server", 65536)
    MANIFEST = ("manifest", 16384)
    ROSTER = ("roster", 1048576)

    @property
    def byte_limit(self) -> int:
        """The most bytes of a file of this kind."""
        return self.value[1]


class TomlRefusal(ValueError):
    """Why `loads` refuses a file. The text is one of the four texts."""


class _Unbounded:
    """A float whose text holds a digit and whose value is not finite."""


_UNBOUNDED: Final = _Unbounded()


def loads(data: bytes, kind: FileKind) -> dict[str, Any]:
    """The top table of the bytes of one file of `kind`.

    Raises `TomlRefusal` for the first check that fails."""
    if len(data) > kind.byte_limit:
        raise TomlRefusal(TOO_LARGE.format(limit=kind.byte_limit))

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise TomlRefusal(NOT_UTF8) from None

    if text.startswith(_BYTE_ORDER_MARK):
        raise TomlRefusal(BYTE_ORDER_MARK)

    try:
        table = tomllib.loads(text, parse_float=_float_of)
    except (ValueError, RecursionError):
        raise TomlRefusal(NOT_TOML) from None

    if not _only_toml10(text) or not _ordered(table):
        raise TomlRefusal(NOT_TOML)

    return table


def _float_of(text: str) -> float | _Unbounded:
    """The value of a float text, or `_UNBOUNDED` for a text with a digit
    and a value that is not finite. `_ordered` refuses that marker."""
    value = float(text)
    if math.isfinite(value) or _DIGIT.search(text) is None:
        return value

    return _UNBOUNDED


def _key_of(entry: tuple[str, Any]) -> str:
    return entry[0]


def _ordered(top: dict[str, Any]) -> bool:
    """Whether each number is in range and no table or array has a level
    above `DEPTH`. Puts the keys of each table in code point order, in
    place.

    Each table or array that the walk has open waits on a stack with its
    level, so a deep value uses no recursion."""
    waiting: list[tuple[dict[str, Any] | list[Any], int]] = [(top, 1)]
    while waiting:
        container, level = waiting.pop()
        if isinstance(container, dict):
            entries = sorted(container.items(), key=_key_of)
            container.clear()
            container.update(entries)
            values: list[Any] = list(container.values())
        else:
            values = container

        for value in values:
            if not isinstance(value, dict | list):
                if not _in_range(value):
                    return False

                continue

            if level >= DEPTH:
                return False

            waiting.append((cast("dict[str, Any] | list[Any]", value), level + 1))

    return True


def _in_range(value: object) -> bool:
    """Whether a value that is no table and no array passes check 6."""
    if isinstance(value, int):
        return _INTEGER_MIN <= value <= _INTEGER_MAX

    return value is not _UNBOUNDED


# --- the TOML 1.0 check --------------------------------------------------------


class _State(Enum):
    """Where the pass of `_only_toml10` is in the text."""

    PLAIN = "plain"
    COMMENT = "comment"
    BASIC = "basic string"
    MULTI_BASIC = "multi-line basic string"
    LITERAL = "literal string"
    MULTI_LITERAL = "multi-line literal string"


#: The two strings that a quote sign starts: one sign, or three.
_OPENS: Final = {
    '"': (_State.BASIC, _State.MULTI_BASIC),
    "'": (_State.LITERAL, _State.MULTI_LITERAL),
}

#: The quote sign of each kind of string.
_QUOTE: Final = {
    _State.BASIC: '"',
    _State.MULTI_BASIC: '"',
    _State.LITERAL: "'",
    _State.MULTI_LITERAL: "'",
}

#: The two kinds of string that hold an escape.
_ESCAPING: Final = frozenset((_State.BASIC, _State.MULTI_BASIC))

#: The two kinds of string that end at one quote sign.
_ONE_LINE: Final = frozenset((_State.BASIC, _State.LITERAL))

#: The next sign that a string can act on: its quote sign, and in a basic
#: string also the backslash of an escape.
_STOPS: Final = {
    _State.BASIC: re.compile(r'["\\]'),
    _State.MULTI_BASIC: re.compile(r'["\\]'),
    _State.LITERAL: re.compile("'"),
    _State.MULTI_LITERAL: re.compile("'"),
}

#: The two escapes that TOML 1.1 added to a basic string: `\e` and `\x`.
_NEW_ESCAPES: Final = frozenset("ex")

#: Three quote signs start a multi-line string. In such a string, a run of
#: three quote signs or more ends it: the last three signs close it, and the
#: signs before them are text.
_TRIPLE: Final = 3

#: The sign that stands for each sign of a string and of a comment in the
#: plain text of the pass. It is no digit, no colon, no sign of a number and
#: no comma, so it starts no form.
_HIDDEN: Final = "_"

#: A time with no seconds, alone or in a date-time: two digits, a colon and
#: two digits, with no digit, colon or sign of an offset before them, and no
#: digit or colon after them.
_NO_SECONDS: Final = re.compile(r"(?<![0-9:+-])[0-9]{2}:[0-9]{2}(?![0-9:])")


def _only_toml10(text: str) -> bool:
    """Whether the text holds none of the six forms that TOML 1.1 added.

    The forms:

    a. A line end in an inline table. A line end in a string, or in an
       array that the inline table holds, does not count.
    b. A comma as the last sign before the `}` of an inline table. Spaces
       and tabs between the two do not count.
    c. The escape `\\e` in a basic string or a multi-line basic string.
    d. The escape `\\x` in such a string.
    e. A time with no seconds, for example `07:32`.
    f. A date-time with no seconds, for example `1979-05-27T07:32Z`.

    A sign inside a string or a comment starts no form a, b, e or f. A
    literal string holds no escape.

    The pass reads the text one time. It keeps a state and a stack of the
    brackets that are open. Outside a string and a comment, the pass opens
    a comment at `#` and a string at a quote sign. It pushes each `{` and
    `[`, and it pops at each `}` and `]`. The pass also keeps a plain copy
    of the text, with `_HIDDEN` in place of each sign of a string and of a
    comment. The forms e and f are a match of `_NO_SECONDS` in that copy.

    The function never raises. Its answer for a text that is no TOML has no
    meaning: `loads` calls it only for a text that `tomllib` read."""
    state = _State.PLAIN
    brackets: list[str] = []
    plain: list[str] = []
    last = ""
    index = 0
    while index < len(text):
        sign = text[index]
        if state is _State.COMMENT and sign == "\n":
            state = _State.PLAIN

        if state is _State.PLAIN:
            if sign == "\n" and brackets[-1:] == ["{"]:
                return False

            if sign == "}" and last == ",":
                return False

            state, width = _plain_step(text, index, brackets)
            shown = sign if state is _State.PLAIN else _HIDDEN * width
        else:
            if state in _ESCAPING and sign == "\\" and text[index + 1 : index + 2] in _NEW_ESCAPES:
                return False

            state, width = _inner_step(text, index, state)
            shown = _HIDDEN * width

        plain.append(shown)
        if shown not in (" ", "\t"):
            last = shown[-1]

        index += width

    return _NO_SECONDS.search("".join(plain)) is None


def _plain_step(text: str, index: int, brackets: list[str]) -> tuple[_State, int]:
    """The state after the sign at `index` in the plain state, and the count
    of signs that the step reads."""
    sign = text[index]
    if sign == "#":
        return _State.COMMENT, 1

    if sign in _OPENS:
        single, multi = _OPENS[sign]
        if text.startswith(sign * _TRIPLE, index):
            return multi, _TRIPLE

        return single, 1

    if sign in "{[":
        brackets.append(sign)
    elif sign in "}]" and brackets:
        brackets.pop()

    return _State.PLAIN, 1


def _inner_step(text: str, index: int, state: _State) -> tuple[_State, int]:
    """The state after the signs from `index` in a string or a comment, and
    the count of signs that the step reads. A comment ends before its line
    end, so the plain state reads that line end."""
    if state is _State.COMMENT:
        end = text.find("\n", index)
        return state, (len(text) if end < 0 else end) - index

    stop = _STOPS[state].search(text, index)
    if stop is None:
        return state, len(text) - index

    if stop.start() > index:
        return state, stop.start() - index

    if text[index] == "\\":
        return state, min(2, len(text) - index)

    if state in _ONE_LINE:
        return _State.PLAIN, 1

    quote = _QUOTE[state]
    end = index
    while end < len(text) and text[end] == quote:
        end += 1

    run = end - index
    return (_State.PLAIN if run >= _TRIPLE else state), run
