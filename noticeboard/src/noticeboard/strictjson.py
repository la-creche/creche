"""Strict JSON: a reader and a writer for a JSON text from another process.

`loads` gives the value of a text, or it raises `NotStrictJson`. `dumps` gives
the text of a value, or it raises `NotStrictJson`. The writer holds the rules
of the reader, so `loads` reads each text of `dumps`.

A text is strict when each of these eight rules holds:

1. Size. The text is not longer than the cap of its surface. The caller gives
   the cap. This module has no default.
2. Encoding. The bytes are UTF-8, and the first character is not a byte order
   mark. Thus UTF-16 and UTF-32 are not strict.
3. Grammar. The text is one value of RFC 8259 between optional white space.
   White space is a space, a tab, a line feed or a carriage return. `NaN`,
   `Infinity` and `-Infinity` are no values. Nothing follows the white space
   after the value.
4. Strings. A character below U+0020 is an escape. The escape of the first
   half of a surrogate pair has the escape of a second half directly after
   it, and a second half is in that place only.
5. Keys. One object holds each key one time. Two keys are equal when their
   code points are equal after the decode of each escape.
6. Depth. `DEPTH_MAX` arrays and objects are open at one point, or less.
7. Integers. A number with no fraction and no exponent is in the range from
   `INTEGER_MIN` to `INTEGER_MAX`.
8. Floats. Each other number is finite as a float of 64 bits. `1e-999` is 0.

A value of each kind is a strict text. A caller that needs an object checks
the kind of the value itself.

A number is the longest run of digits and of the characters `+`, `-`, `.`,
`e` and `E`. A run that is no number of RFC 8259 breaks rule 3, for example
`01`, `1.` and `1.5.2`. A number in such a run is thus never out of range.

The source of the rules is the Rust module `creche_contracts::json`, in
`rust/crates/creche-contracts/src/json.rs`. Each word of `Rule` is a word of
the enum `Rule` there. The vector surface `runtime.strict_json` records the
reader of the noticeboard copy. A test of the Rust module gives each input of
that surface to the Rust reader.

A text can break more than one rule. The refusal then names the fault that
this reader finds first, in this sequence:

1. The size.
2. The encoding: UTF-8 first, then the mark.
3. The read of the standard decoder, from the start of the text: the grammar,
   a word that is no value, the range of a number and a key two times. The
   decoder gives the keys of an object when the object ends. A text that
   nests deeper than the decoder can read breaks rule 6 here.
4. A character after the value.
5. The depth.
6. A lone surrogate.

The Rust reader names the fault at the lowest byte offset. For a text with
two faults, the two readers can thus give two words. The vector surface holds
only texts that break one rule.

The standard decoder reads each text. No loop of this module reads each
character of a long text.

This file is a copy. Each package that needs these rules holds a file with
the same bytes, and `bin/tests/test_strictjson_copies.py` fails when two
copies differ. Change each copy in one commit. The file needs the standard
library only.
"""

from __future__ import annotations

import enum
import json
import math
import re
from array import array
from itertools import accumulate
from typing import Final, NoReturn

#: The deepest nesting of arrays and objects in a strict text.
DEPTH_MAX: Final = 64

#: The least integer and the largest integer of a strict text: the least
#: value of 64 bits with a sign, and the largest value of 64 bits with none.
INTEGER_MIN: Final = -(2**63)
INTEGER_MAX: Final = 2**64 - 1


class Rule(enum.StrEnum):
    """The rule that a refused text breaks. The set is closed.

    The value of a member is the word of the rule in a log line and in a
    notice. Each word is a word of the Rust enum `Rule`.
    """

    TOO_LARGE = "too_large"
    NOT_UTF8 = "not_utf8"
    BYTE_ORDER_MARK = "byte_order_mark"
    SYNTAX = "syntax"
    CONSTANT = "constant"
    TRAILING_DATA = "trailing_data"
    TOO_DEEP = "too_deep"
    DUPLICATE_KEY = "duplicate_key"
    LONE_SURROGATE = "lone_surrogate"
    INTEGER_RANGE = "integer_range"
    FLOAT_RANGE = "float_range"


class NotStrictJson(ValueError):
    """A text or a value that is not strict JSON.

    The message is the word of the rule. The error holds no character of the
    text and no other error, so a log line can print it for a text that holds
    a secret.

    `offset` is a count of bytes from the start of the text, or `None`.
    `loads` gives a count for four rules, and the Rust reader gives the same
    count for a text that breaks one rule:

    - `TOO_LARGE`: the cap.
    - `NOT_UTF8`: the first byte that is not UTF-8.
    - `BYTE_ORDER_MARK`: 0.
    - `TRAILING_DATA`: the first byte after the value that is not white
      space.

    For each other rule the offset is `None`, because the standard decoder
    gives this module no such place. Each refusal of `dumps` has the offset
    `None`.
    """

    def __init__(self, rule: Rule, offset: int | None) -> None:
        super().__init__(rule.value)
        self._rule: Final = rule
        self._offset: Final = offset

    @property
    def rule(self) -> Rule:
        return self._rule

    @property
    def offset(self) -> int | None:
        return self._offset

    def __reduce__(self) -> tuple[type[NotStrictJson], tuple[Rule, int | None]]:
        """The two arguments again, so a copy of the error has its rule and its offset."""
        return type(self), (self._rule, self._offset)


_BYTE_ORDER_MARK: Final = "\N{BYTE ORDER MARK}"

_WHITE_SPACE: Final = re.compile(r"[ \t\n\r]*")

#: Each character that the run of a number can hold.
_NUMBER_CHARS: Final = "0123456789+-.eE"

#: Two arguments for `bytes.translate`. The call makes each digit the digit
#: 0 and each `E` an `e`, and it deletes each `+`.
_AS_ZERO_AND_E: Final = bytes.maketrans(b"123456789E", b"000000000e")
_PLUS: Final = b"+"

#: After that call, each of these two texts shows a number that can be
#: outside a range. The first one is a run of 19 digits: an integer of 18
#: digits is in the range of rule 7. The second one is an exponent of three
#: digits with no minus sign: a float with a shorter exponent and with less
#: than 19 digits before it is finite.
_WIDE_NUMBER_MARKS: Final = (b"0" * 19, b"e000")

#: The most characters of an integer in the range of rule 7.
_INTEGER_CHARS_MAX: Final = len(str(INTEGER_MIN))

_BACKSLASH: Final = b"\\"
_QUOTE: Final = b'"'

#: The two escapes that hide a quote from a split of the text at each quote.
_ESCAPED_BACKSLASH: Final = b"\\\\"
_ESCAPED_QUOTE: Final = b'\\"'

_OPENERS: Final = (b"[", b"{")

#: The argument for `bytes.translate` that deletes each byte that is no
#: bracket and no quote.
_NOT_A_MARK: Final = bytes(byte for byte in range(256) if byte not in b'[{]}"')

#: A table for `bytes.translate` that makes a bracket that opens the byte
#: 0x01 and a bracket that closes the byte 0xFF. As a byte with a sign, those
#: are 1 and -1.
_BRACKET_AS_STEP: Final = bytes.maketrans(b"[{]}", b"\x01\x01\xff\xff")

#: The first characters of the escape of each half of a surrogate pair, in
#: the two letter cases of the digit D.
_SURROGATE_ESCAPES: Final = (b"\\ud", b"\\uD")


def _refuse_constant(word: str) -> NoReturn:
    raise NotStrictJson(Rule.CONSTANT, None)


def _members(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """The object of the members that the standard decoder read. Rule 5."""
    table = dict(pairs)
    if len(table) != len(pairs):
        raise NotStrictJson(Rule.DUPLICATE_KEY, None)

    return table


class _Numbers:
    """The hooks of the standard decoder for a text with a wide number.

    A hook for a number cannot refuse the text at once. The decoder takes the
    longest number that it can, so the hook does not see the next character.
    That character can make the run a text that is no number, and the fault
    is then `SYNTAX` and not a range. The hook thus keeps the range fault,
    and the next step of the read settles it.
    """

    def __init__(self) -> None:
        #: The first range fault of the read, with the token of its number.
        self.held: tuple[Rule, str] | None = None

    def settle(self) -> None:
        """Refuses the text when a number before this place is out of range."""
        if self.held is not None:
            raise NotStrictJson(self.held[0], None)

    def constant(self, word: str) -> NoReturn:
        self.settle()
        _refuse_constant(word)

    def integer(self, token: str) -> int:
        self.settle()
        if len(token) <= _INTEGER_CHARS_MAX:
            value = int(token)
            if INTEGER_MIN <= value <= INTEGER_MAX:
                return value

        self.held = (Rule.INTEGER_RANGE, token)

        return 0

    def real(self, token: str) -> float:
        self.settle()
        value = float(token)
        if not math.isfinite(value):
            self.held = (Rule.FLOAT_RANGE, token)

        return value

    def members(self, pairs: list[tuple[str, object]]) -> dict[str, object]:
        self.settle()

        return _members(pairs)

    def run_goes_on(self, text: str, end: int) -> bool:
        """Whether the number of the held fault ends at `end` in a longer run."""
        if self.held is None or not _in_number(text, end):
            return False

        before = text[:end]
        run = before[len(before.rstrip(_NUMBER_CHARS)) :]

        return run == self.held[1]


def _in_number(text: str, at: int) -> bool:
    """Whether the character at `at` can be in the run of a number."""
    return at < len(text) and text[at] in _NUMBER_CHARS


def _has_wide_number(data: bytes) -> bool:
    """Whether a number of the text can be outside the range of rule 7 or 8."""
    flat = data.translate(_AS_ZERO_AND_E, _PLUS)

    return any(mark in flat for mark in _WIDE_NUMBER_MARKS)


def _after_white_space(text: str, at: int) -> int:
    found = _WHITE_SPACE.match(text, at)

    return at if found is None else found.end()


def _byte_offset(text: str, index: int) -> int:
    return len(text[:index].encode("utf-8"))


def _too_deep(data: bytes) -> bool:
    """Whether a text that the standard decoder took breaks rule 6."""
    marks = data.translate(None, _NOT_A_MARK)
    if sum(marks.count(opener) for opener in _OPENERS) <= DEPTH_MAX:
        return False

    if _BACKSLASH in data:
        # Without these two escapes, each quote starts or ends a string.
        bare = data.replace(_ESCAPED_BACKSLASH, b"").replace(_ESCAPED_QUOTE, b"")
        marks = bare.translate(None, _NOT_A_MARK)

    # The text starts outside a string, so each second part is outside one.
    outside = b"".join(marks.split(_QUOTE)[::2])
    steps = array("b", outside.translate(_BRACKET_AS_STEP))

    return max(accumulate(steps), default=0) > DEPTH_MAX


def _has_lone_surrogate(data: bytes, value: object) -> bool:
    """Whether a text that the standard decoder took breaks rule 4.

    `value` is the value of the text. It nests `DEPTH_MAX` levels at most.
    """
    if _BACKSLASH not in data:
        return False

    if not any(escape in data for escape in _SURROGATE_ESCAPES):
        return False

    # The decoder keeps a lone surrogate in a string, and UTF-8 cannot hold
    # one. The text of the standard encoder holds each string of the value.
    try:
        json.dumps(value, check_circular=False, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        return True

    return False


# CONTRACT-QUESTION: `rust/AGENTS.md`, "JSON", gives the Rust check one
# sequence: the fault at the lowest byte offset names the rule, and each rule
# has an offset. It does not say what a copy of the reader in another
# language names for a text that breaks two rules. This copy names the fault
# that it finds first, in the sequence of the module docstring, and it has
# an offset for four rules. The vector surface `runtime.strict_json` holds
# the two readers to one result and one word for each text that breaks one
# rule. A copy with the sequence and each offset of the Rust check costs a
# reader that takes each character in Python code. Such a reader is slower
# than the cost limit that the tests of this module hold.
def _read(text: str, data: bytes) -> object:
    """The value of `text`, which is the text of the bytes `data`.

    The two rules of the encoding hold for the text. The function holds each
    later rule.
    """
    numbers: _Numbers | None = None
    if _has_wide_number(data):
        numbers = _Numbers()
        decoder = json.JSONDecoder(
            parse_constant=numbers.constant,
            parse_int=numbers.integer,
            parse_float=numbers.real,
            object_pairs_hook=numbers.members,
        )
    else:
        # Most texts hold no wide number. The decoder then reads each number
        # with no call of this module.
        decoder = json.JSONDecoder(parse_constant=_refuse_constant, object_pairs_hook=_members)

    value: object = None
    end = 0
    stopped: int | None = None
    deep = False
    try:
        value, end = decoder.raw_decode(text, _after_white_space(text, 0))
    except json.JSONDecodeError as error:
        stopped = error.pos
    except RecursionError:
        deep = True

    # Each refusal below is outside the `except` blocks. The error of the
    # decoder holds the text, and a refusal must not hold that error.
    if deep:
        raise NotStrictJson(Rule.TOO_DEEP, None)

    if stopped is not None:
        if numbers is not None and not numbers.run_goes_on(text, stopped):
            numbers.settle()

        raise NotStrictJson(Rule.SYNTAX, None)

    if type(value) in (int, float) and _in_number(text, end):
        raise NotStrictJson(Rule.SYNTAX, None)

    if numbers is not None:
        numbers.settle()

    after = _after_white_space(text, end)
    if after != len(text):
        raise NotStrictJson(Rule.TRAILING_DATA, _byte_offset(text, after))

    if _too_deep(data):
        raise NotStrictJson(Rule.TOO_DEEP, None)

    if _has_lone_surrogate(data, value):
        raise NotStrictJson(Rule.LONE_SURROGATE, None)

    return value


def loads(data: bytes, *, cap: int) -> object:
    """The value of the strict text `data`.

    `cap` is the most bytes that a text of the surface has. A text of exactly
    that count is under the cap.

    Raises `NotStrictJson` with the rule that the text breaks.
    """
    if len(data) > cap:
        raise NotStrictJson(Rule.TOO_LARGE, cap)

    text = ""
    bad_byte: int | None = None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        bad_byte = error.start

    # The refusal is outside the `except` block. The error of the codec holds
    # the bytes, and a refusal must not hold that error.
    if bad_byte is not None:
        raise NotStrictJson(Rule.NOT_UTF8, bad_byte)

    if text.startswith(_BYTE_ORDER_MARK):
        raise NotStrictJson(Rule.BYTE_ORDER_MARK, 0)

    return _read(text, data)


def _broken_rule(text: str) -> Rule | None:
    """The rule that a text of `json.dumps` breaks. `None` for a strict text."""
    try:
        data = text.encode("utf-8")
    except UnicodeEncodeError:
        # With no ASCII escapes, the text holds a lone surrogate as it is.
        return Rule.LONE_SURROGATE

    try:
        _read(text, data)
    except NotStrictJson as refusal:
        return refusal.rule

    return None


def dumps(
    value: object,
    *,
    indent: int | str | None = None,
    sort_keys: bool = False,
    separators: tuple[str, str] | None = None,
    ensure_ascii: bool = True,
) -> str:
    """The strict text of `value`: the text of `json.dumps` with the same arguments.

    The function gives a text only when the text is strict. It never writes
    `null` in the place of a number.

    Raises `NotStrictJson` for a value whose text breaks a rule. Three
    examples are a float that is not finite, a string with a lone surrogate
    and an integer outside the range. A value that `json.dumps` has no text
    for gets the `TypeError` of `json.dumps`.
    """
    text = ""
    rule: Rule | None = None
    try:
        # With no check for a circle, a value that holds itself ends in a
        # `RecursionError`, as a value that nests too deep does.
        text = json.dumps(
            value,
            check_circular=False,
            indent=indent,
            sort_keys=sort_keys,
            separators=separators,
            ensure_ascii=ensure_ascii,
        )
    except RecursionError:
        rule = Rule.TOO_DEEP
    except ValueError:
        # With these arguments, `json.dumps` raises this type for one value:
        # an integer with more digits than `int` gives as text.
        rule = Rule.INTEGER_RANGE

    if rule is None:
        rule = _broken_rule(text)

    if rule is not None:
        raise NotStrictJson(rule, None)

    return text
