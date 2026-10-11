"""The strict JSON reader and writer (`strictjson.py`).

Each table holds texts for one rule of the module docstring. A refused text
breaks the rule of its table. Most of them break no other rule. A comment
marks a text that also breaks a later rule of the sequence.
"""

from __future__ import annotations

import copy
import json
import math
import pickle
import time
from collections.abc import Callable

import pytest
from noticeboard.strictjson import NotStrictJson, Rule

from noticeboard import strictjson

MEBIBYTE = 1 << 20

#: A cap that each short text of these tests is under.
ROOMY = MEBIBYTE

#: The most levels of a strict text, and one level more.
LEVELS = strictjson.DEPTH_MAX
ONE_MORE = LEVELS + 1

#: One digit more than `int` reads from a text.
TOO_MANY_DIGITS = 4301


def refusal(data: bytes, cap: int = ROOMY) -> NotStrictJson:
    with pytest.raises(NotStrictJson) as caught:
        strictjson.loads(data, cap=cap)

    return caught.value


def rule_of(data: bytes) -> Rule:
    return refusal(data).rule


def nested(count: int, opener: str, inside: str, closer: str) -> bytes:
    """`count` times `opener`, then `inside`, then `count` times `closer`."""
    return f"{opener * count}{inside}{closer * count}".encode()


# --- the rule words --------------------------------------------------------------------


def test_the_words_are_the_eleven_words_of_the_rust_enum() -> None:
    assert [rule.value for rule in Rule] == [
        "too_large",
        "not_utf8",
        "byte_order_mark",
        "syntax",
        "constant",
        "trailing_data",
        "too_deep",
        "duplicate_key",
        "lone_surrogate",
        "integer_range",
        "float_range",
    ]


def test_a_rule_is_its_word_as_text() -> None:
    assert str(Rule.LONE_SURROGATE) == "lone_surrogate"
    assert f"{Rule.TOO_DEEP}" == "too_deep"
    assert Rule("syntax") is Rule.SYNTAX
    with pytest.raises(ValueError):
        Rule("Syntax")


# --- the refusal -----------------------------------------------------------------------


def test_a_refusal_is_a_value_error_with_the_word_as_its_message() -> None:
    refused = refusal(b"[1,]")

    assert isinstance(refused, ValueError)
    assert str(refused) == "syntax"
    assert refused.args == ("syntax",)
    assert refused.rule is Rule.SYNTAX


def test_a_refusal_holds_no_character_of_the_text_and_no_other_error() -> None:
    secret = "hunter2-hunter2"
    texts = (
        f'{{"token": "{secret}", "a": 1, "a": 2}}'.encode(),
        f'{{"token": "{secret}"'.encode(),
        f'["{secret}", "\\ud800"]'.encode(),
        f'["{secret}", NaN]'.encode(),
        f'["{secret}"] x'.encode(),
        f'["{secret}", 1e999]'.encode(),
        nested(ONE_MORE, "[", f'"{secret}"', "]"),
        secret.encode() + b"\xff",
        b"\xef\xbb\xbf" + secret.encode(),
    )
    for data in texts:
        refused = refusal(data)

        assert refused.__cause__ is None
        assert refused.__context__ is None
        assert secret not in repr(refused)
        assert secret not in str(refused)
        assert refused.args == (refused.rule.value,)


def test_the_rule_and_the_offset_of_a_refusal_do_not_change() -> None:
    refused = refusal(b"1 2")

    for name in ("rule", "offset"):
        with pytest.raises(AttributeError):
            setattr(refused, name, None)

    assert (refused.rule, refused.offset) == (Rule.TRAILING_DATA, 2)


def test_a_copy_of_a_refusal_has_its_rule_and_its_offset() -> None:
    refused = refusal(b"1 2")

    for again in (copy.copy(refused), pickle.loads(pickle.dumps(refused))):
        assert type(again) is NotStrictJson
        assert (again.rule, again.offset, str(again)) == (Rule.TRAILING_DATA, 2, "trailing_data")


# --- rule 1: the size ------------------------------------------------------------------


def test_the_reader_has_no_default_cap() -> None:
    loads: Callable[..., object] = strictjson.loads

    with pytest.raises(TypeError):
        loads(b"1")
    with pytest.raises(TypeError):
        loads(b"1", 4)

    assert loads(b"1", cap=4) == 1


def test_a_text_of_exactly_its_cap_is_under_the_cap() -> None:
    assert strictjson.loads(b"12345", cap=5) == 12345
    assert strictjson.loads(b"[1, 2, 3]", cap=9) == [1, 2, 3]


def test_a_text_over_its_cap_is_too_large_and_the_offset_is_the_cap() -> None:
    for data, cap in ((b"12345", 4), (b"1", 0), (b"[1, 2, 3]", 8)):
        refused = refusal(data, cap)

        assert (refused.rule, refused.offset) == (Rule.TOO_LARGE, cap)


def test_the_size_rule_is_first() -> None:
    assert refusal(b"\xff\xff\xff", 2).rule is Rule.TOO_LARGE
    assert refusal(b"nope", 1).rule is Rule.TOO_LARGE
    assert refusal(b"\xef\xbb\xbf1", 3).rule is Rule.TOO_LARGE


def test_the_empty_text_is_under_each_cap_and_is_no_value() -> None:
    assert refusal(b"", 0).rule is Rule.SYNTAX


# --- rule 2: the encoding --------------------------------------------------------------

#: Bytes that are not UTF-8, with the offset of the first such byte.
NOT_UTF8: tuple[tuple[bytes, int], ...] = (
    (b"\xff", 0),
    (b"\x80", 0),
    (b'{"a": "\xff"}', 7),
    # The three bytes of the first half of a surrogate pair, and of a second half.
    (b'"\xed\xa0\x80"', 1),
    (b'"\xed\xbf\xbf"', 1),
    # A form with more bytes than its character needs.
    (b'"\xc0\xaf"', 1),
    (b'"\xe0\x80\x80"', 1),
    # A code point after the last one.
    (b'"\xf4\x90\x80\x80"', 1),
    # A character that the end of the text cuts.
    (b'"\xc3', 1),
    # UTF-16 and UTF-32 with a byte order mark.
    (b"\xff\xfe" + '{"a": 1}'.encode("utf-16-le"), 0),
    (b"\xfe\xff" + '{"a": 1}'.encode("utf-16-be"), 0),
    (b"\xff\xfe\x00\x00" + '{"a": 1}'.encode("utf-32-le"), 0),
    (b"\x00\x00\xfe\xff" + '{"a": 1}'.encode("utf-32-be"), 2),
    # The check reads each byte of the text before the grammar.
    (b"nope\xff", 4),
    # The check of the bytes comes before the check of the mark.
    (b"\xef\xbb\xbf\xff", 3),
)


def test_bytes_that_are_not_utf8_are_refused_at_the_first_such_byte() -> None:
    for data, offset in NOT_UTF8:
        refused = refusal(data)

        assert (refused.rule, refused.offset) == (Rule.NOT_UTF8, offset), data


def test_a_text_that_starts_with_the_mark_is_refused_at_offset_0() -> None:
    for text in ("\N{BOM}1", '\N{BOM}{"a": 1}', "\N{BOM}[]", "\N{BOM}"):
        refused = refusal(text.encode())

        assert (refused.rule, refused.offset) == (Rule.BYTE_ORDER_MARK, 0), text


def test_the_mark_in_a_string_is_a_character() -> None:
    assert strictjson.loads('"\N{BOM}"'.encode(), cap=ROOMY) == "\N{BOM}"
    assert strictjson.loads('["a\N{BOM}"]'.encode(), cap=ROOMY) == ["a\N{BOM}"]


def test_utf16_and_utf32_with_no_mark_are_no_json_text() -> None:
    """Each second byte is U+0000, which is UTF-8 and no white space."""
    for codec in ("utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"):
        assert rule_of('{"a": 1}'.encode(codec)) is Rule.SYNTAX, codec

    # The value is whole at the first U+0000.
    refused = refusal("7".encode("utf-16-le"))
    assert (refused.rule, refused.offset) == (Rule.TRAILING_DATA, 1)


# --- rule 3: the grammar ---------------------------------------------------------------

#: Each strict text, with its value.
ACCEPTED: tuple[tuple[str, object], ...] = (
    ("null", None),
    ("true", True),
    ("false", False),
    ("0", 0),
    ("7", 7),
    ("-7", -7),
    ("10", 10),
    ("0.5", 0.5),
    ("-0.0", -0.0),
    ("1e3", 1000.0),
    ("1E3", 1000.0),
    ("1e+3", 1000.0),
    ("1E-3", 0.001),
    ("0e0", 0.0),
    ("1.5e10", 15000000000.0),
    ("1e007", 10000000.0),
    ('""', ""),
    ('"a"', "a"),
    (r'"\"\\\/\b\f\n\r\t"', '"\\/\b\f\n\r\t'),
    (r'"\u0041\u00e9\u00E9\uabcd\uABCD\uAbCd"', "A\xe9\xe9\uabcd\uabcd\uabcd"),
    (r'"\u0000"', "\x00"),
    (r'"\ufffe\uffff"', "\ufffe\uffff"),
    ('"caf\xe9 \U0001f600"', "caf\xe9 \U0001f600"),
    # U+007F and a line separator need no escape.
    ('"\x7f\u2028\u2029"', "\x7f\u2028\u2029"),
    # A word of another rule is a text inside a string.
    ('"NaN Infinity // [1,] 01"', "NaN Infinity // [1,] 01"),
    ("[]", []),
    ("[ ]", []),
    ("[1]", [1]),
    ("[1,2]", [1, 2]),
    ("[ 1 , 2 ]", [1, 2]),
    ("[[],[]]", [[], []]),
    ('[null,true,false,0,-0.0,"",[],{}]', [None, True, False, 0, -0.0, "", [], {}]),
    ("{}", {}),
    ("{ }", {}),
    ('{"a":1}', {"a": 1}),
    ('{ "a" : 1 , "b" : 2 }', {"a": 1, "b": 2}),
    ('{"":1}', {"": 1}),
    ('{"a":[],"b":{}}', {"a": [], "b": {}}),
    # Each of the four characters of white space, before the value and after it.
    (" \t\n\r1 \t\n\r", 1),
    ('\n{\n\t"a" :\r\n[ 1 ,\n2 ]\n}\n', {"a": [1, 2]}),
)


def test_each_strict_text_gives_its_value() -> None:
    for text, value in ACCEPTED:
        got = strictjson.loads(text.encode(), cap=ROOMY)

        assert got == value, text
        assert repr(got) == repr(value), text


def test_a_strict_text_gives_the_value_of_the_standard_reader() -> None:
    for text, _ in ACCEPTED:
        assert repr(strictjson.loads(text.encode(), cap=ROOMY)) == repr(json.loads(text)), text


def test_the_order_of_the_keys_is_the_order_of_the_text() -> None:
    got = strictjson.loads(b'{"b": 1, "a": 2, "c": 3}', cap=ROOMY)

    assert isinstance(got, dict)
    assert list(got) == ["b", "a", "c"]


#: Each text that is no value of RFC 8259.
SYNTAX: tuple[bytes, ...] = (
    # No value.
    b"",
    b" ",
    b"\n\t \r",
    # A word that is no literal.
    b"nope",
    b"nul",
    b"tru",
    b"fals",
    b"Null",
    b"TRUE",
    b"None",
    b"nan",
    b"inf",
    b"-inf",
    b"undefined",
    # A text with a part that is absent.
    b"[",
    b"[1",
    b"[1,",
    b"[1,]",
    b"[,1]",
    b"[1,,2]",
    b"[1 2]",
    b"]",
    b"[1}",
    b"{",
    b'{"a"',
    b'{"a":',
    b'{"a":1',
    b'{"a":1,}',
    b'{"a"}',
    b'{"a" 1}',
    b'{"a":}',
    b'{"a":1 "b":2}',
    b'{"a":1]',
    b"}",
    # A key that is no string.
    b"{a:1}",
    b"{1:2}",
    b"{null:1}",
    b"{'a':1}",
    # A string with no end, and a string in other quotes.
    b'"',
    b'"abc',
    b"'a'",
    # A comment.
    b"/* c */ 1",
    b"// c\n1",
    b"[1, /* c */ 2]",
    # White space that is not one of the four characters.
    b"\x0c1",
    b"\x0b1",
    "\xa01".encode(),
    "\u20281".encode(),
    "[1,\u30002]".encode(),
    # A byte order mark after the first character.
    " \N{BOM}1".encode(),
    # A separator alone, and a character that starts no value.
    b":",
    b",",
    "\xe9".encode(),
    b"\x00",
    # A number that starts with no digit.
    b".5",
    b"-.5",
    b"-",
    b"--1",
    b"+1",
    # A digit that is not ASCII in the place of a value.
    "\u0661".encode(),
)


def test_a_text_that_is_no_value_breaks_the_grammar() -> None:
    for data in SYNTAX:
        refused = refusal(data)

        assert refused.rule is Rule.SYNTAX, data
        assert refused.offset is None


#: Each word that some JSON readers take as a number.
CONSTANT: tuple[bytes, ...] = (
    b"NaN",
    b"Infinity",
    b"-Infinity",
    b" NaN",
    b"[NaN]",
    b"[1,-Infinity]",
    b'{"a":Infinity}',
    b'{"a": -Infinity}',
    # In a text that also holds an integer of 20 digits or an exponent of
    # three digits.
    b"[NaN,18446744073709551615]",
    b'{"f":1e100,"x":Infinity}',
)


def test_a_word_for_a_number_that_is_not_finite_is_no_value() -> None:
    for data in CONSTANT:
        refused = refusal(data)

        assert refused.rule is Rule.CONSTANT, data
        assert refused.offset is None


#: A character after the value that is not white space, with its offset.
TRAILING_DATA: tuple[tuple[bytes, int], ...] = (
    (b"1 2", 2),
    (b"1x", 1),
    (b"1,", 1),
    (b"1]", 1),
    (b"0x10", 1),
    (b"1_000", 1),
    (b"truex", 4),
    (b"true1", 4),
    (b"falsee", 5),
    (b"nullnull", 4),
    (b'"a""b"', 3),
    (b'"a"x', 3),
    (b"[]]", 2),
    (b"[][]", 2),
    (b"{}}", 2),
    (b'{"a": 1}{"b": 2}', 8),
    (b"[1] 2", 4),
    (b"1 \n x", 4),
    (b"1 // c", 2),
    (b"1\x00", 1),
    (b"1\x0c", 1),
    ("1\u0661".encode(), 1),
    # The offset counts bytes: the string before it has seven.
    ('"\xe9\u4e2d" x'.encode(), 8),
)


def test_a_character_after_the_value_is_trailing_data_at_its_offset() -> None:
    for data, offset in TRAILING_DATA:
        refused = refusal(data)

        assert (refused.rule, refused.offset) == (Rule.TRAILING_DATA, offset), data


# --- the run of a number ---------------------------------------------------------------

#: Each run of number characters that is no number.
NO_NUMBER: tuple[str, ...] = (
    "01",
    "00",
    "-01",
    "1.",
    "0.",
    "1.5.2",
    "1e5e3",
    "1e5.3",
    "1+2",
    "1-2",
    "1e5-",
    "1e",
    "1E+",
    "1.e3",
    "1.0e",
    # A number in such a run is never out of range.
    "1e999e5",
    "1e999.",
    "18446744073709551616.",
    "18446744073709551616e",
    "-9223372036854775809-",
    "99999999999999999999999999999999999999990000000000000000000000000000000+1",
)


def test_a_run_that_is_no_number_breaks_the_grammar() -> None:
    for run in NO_NUMBER:
        assert rule_of(run.encode()) is Rule.SYNTAX, run
        assert rule_of(f" {run} ".encode()) is Rule.SYNTAX, run


def test_a_run_that_is_no_number_breaks_the_grammar_inside_a_value() -> None:
    for run in NO_NUMBER:
        assert rule_of(f"[{run}]".encode()) is Rule.SYNTAX, run
        assert rule_of(f'{{"a": {run}}}'.encode()) is Rule.SYNTAX, run
        assert rule_of(f"[1, {run}, 2]".encode()) is Rule.SYNTAX, run


def test_a_literal_has_no_run() -> None:
    """`true` ends with a character that a number can hold."""
    assert rule_of(b"truee") is Rule.TRAILING_DATA
    assert rule_of(b"true1") is Rule.TRAILING_DATA
    assert rule_of(b"false-") is Rule.TRAILING_DATA
    assert rule_of(b"[truee]") is Rule.SYNTAX


def test_a_number_out_of_range_before_another_fault_keeps_its_rule() -> None:
    """The number is whole: a character that no number holds comes after it."""
    assert rule_of(b"1e999 x") is Rule.FLOAT_RANGE
    assert rule_of(b"1e999x") is Rule.FLOAT_RANGE
    assert rule_of(b"18446744073709551616x") is Rule.INTEGER_RANGE
    assert rule_of(b"[18446744073709551616,") is Rule.INTEGER_RANGE
    assert rule_of(b"[1e999, truee]") is Rule.FLOAT_RANGE
    assert rule_of(b"[1e999, 1e999e5]") is Rule.FLOAT_RANGE
    assert rule_of(b'[1e999, "1e999"e]') is Rule.FLOAT_RANGE
    assert rule_of(b"[1e999, NaN]") is Rule.FLOAT_RANGE
    assert rule_of(b'{"a": 1e999, "a": 2}') is Rule.FLOAT_RANGE
    assert rule_of(b"[1e999}") is Rule.FLOAT_RANGE
    assert rule_of(b'{"a":1e999,"a":"x"}') is Rule.FLOAT_RANGE
    assert rule_of(b"[1e999,18446744073709551616]") is Rule.FLOAT_RANGE


# --- rule 4: the strings ---------------------------------------------------------------

#: A string that breaks rule 4 with no surrogate.
BROKEN_STRING: tuple[bytes, ...] = (
    # A control character with no escape.
    b'"a\tb"',
    b'"a\nb"',
    b'"a\rb"',
    b'"a\x00b"',
    b'"a\x1fb"',
    # An escape outside the list.
    rb'"\a"',
    rb'"\'"',
    rb'"\0"',
    rb'"\v"',
    rb'"\x41"',
    rb'"\U0041"',
    # A `\u` escape with less than four hex digits.
    rb'"\u"',
    rb'"\u12"',
    rb'"\u12G4"',
    rb'"\u+123"',
    rb'"\u 123"',
    # An escape that the end of the text cuts.
    b'"\\',
    b'"\\u123',
)


def test_a_string_that_breaks_the_escape_rules_breaks_the_grammar() -> None:
    for data in BROKEN_STRING:
        assert rule_of(data) is Rule.SYNTAX, data


#: Each form of a lone surrogate.
LONE_SURROGATE: tuple[bytes, ...] = (
    # A first half alone, in the two letter cases.
    rb'"\ud800"',
    rb'"\uD800"',
    rb'"\udbff"',
    rb'"\uDBFF"',
    # A second half alone.
    rb'"\udc00"',
    rb'"\uDC00"',
    rb'"\udfff"',
    # A first half, then a character that is no second half.
    rb'"\ud800\u0041"',
    rb'"\ud800a"',
    rb'"\ud800\n"',
    rb'"\ud800 \udc00"',
    # A first half, an escaped backslash and the text `udc00`.
    rb'"\ud800\\udc00"',
    # The two halves in the wrong order, and one half two times.
    rb'"\udc00\ud800"',
    rb'"\udc00\udc00"',
    rb'"\ud83d\ud83d\ude00"',
    rb'"a\ud83d\ude00\ude00"',
    # In a key, in an item and deep in a value.
    rb'{"\ud800":1}',
    rb'["\ud800"]',
    rb'{"a":1,"b":"\udc00"}',
    rb'{"a":1,"b":{"c":["\ud800"]}}',
    # After a string that holds a quote and a bracket.
    rb'["\"]", "\ud800"]',
)


def test_each_form_of_a_lone_surrogate_is_refused() -> None:
    for data in LONE_SURROGATE:
        refused = refusal(data)

        assert refused.rule is Rule.LONE_SURROGATE, data
        assert refused.offset is None


def test_a_whole_surrogate_pair_is_one_character() -> None:
    pairs = (
        (rb'"\ud83d\ude00"', "\U0001f600"),
        (rb'"\uD83D\uDE00"', "\U0001f600"),
        (rb'"\ud800\udc00"', "\U00010000"),
        (rb'"\udbff\udfff"', "\U0010ffff"),
        (rb'"a\ud83d\ude00b\ud83d\ude00"', "a\U0001f600b\U0001f600"),
        # The code points directly below and directly above the surrogates.
        (rb'"\ud7ff\ue000"', "\ud7ff\ue000"),
    )
    for data, value in pairs:
        assert strictjson.loads(data, cap=ROOMY) == value, data


def test_the_text_of_a_surrogate_escape_after_a_backslash_is_no_surrogate() -> None:
    assert strictjson.loads(rb'"\\ud800"', cap=ROOMY) == "\\ud800"
    assert strictjson.loads(rb'"\\\\ud800 \\uDC00"', cap=ROOMY) == "\\\\ud800 \\uDC00"
    assert strictjson.loads(rb'{"\\udfff": "\\ud83d"}', cap=ROOMY) == {"\\udfff": "\\ud83d"}


# --- rule 5: the keys ------------------------------------------------------------------

#: An object that holds one key two times.
DUPLICATE_KEY: tuple[bytes, ...] = (
    rb'{"a":1,"a":2}',
    rb'{"a":1,"a":1}',
    rb'{"a":1,"b":2,"a":3}',
    rb'{"":1,"":2}',
    # An escape and its character are one key.
    rb'{"a":1,"\u0061":2}',
    rb'{"\u0041":1,"\u0041":2}',
    rb'{"a/b":1,"a\/b":2}',
    rb'{"\n":1,"\u000a":2}',
    rb'{"\u00e9":1,"\u00E9":2}',
    '{"\U0001f600":1,"\\ud83d\\ude00":2}'.encode(),
    # An inner object.
    rb'{"x":{"a":1,"a":2}}',
    rb'[{"a":1,"a":1}]',
    rb'[{}, {"b": [{"k": null, "k": null}]}]',
    # In a text that also holds an integer of 20 digits, and in a text with
    # a string of 19 digits.
    rb'{"a":1,"a":2,"n":18446744073709551615}',
    rb'{"t":"1234567890123456789","a":1,"a":2}',
)


def test_an_object_with_a_key_two_times_is_refused() -> None:
    for data in DUPLICATE_KEY:
        refused = refusal(data)

        assert refused.rule is Rule.DUPLICATE_KEY, data
        assert refused.offset is None


def test_keys_that_differ_in_a_code_point_are_two_keys() -> None:
    texts = (
        '{"a":1,"A":2}',
        '{"a":1,"a ":2}',
        '{"1":1,"1.0":2,"01":3,"true":4}',
        '{"":1,"\\u0000":2}',
        # U+00E9 is not `e` with U+0301: the reader normalizes no key.
        '{"\xe9":1,"e\u0301":2}',
        '{"\\ud83d\\ude00":1,"\\ud83d\\ude01":2}',
        # One key in two objects is one time in each object.
        '{"a":{"a":{"a":1}}}',
        '[{"a":1},{"a":2}]',
        '{"a":{"b":1},"b":{"a":1}}',
    )
    for text in texts:
        assert strictjson.loads(text.encode(), cap=ROOMY) == json.loads(text), text


# --- rule 6: the depth -----------------------------------------------------------------


def test_the_depth_limit_is_64_levels() -> None:
    assert strictjson.DEPTH_MAX == 64


def test_a_text_of_64_levels_is_strict() -> None:
    texts = (
        nested(LEVELS, "[", "", "]"),
        nested(LEVELS, "[", "1", "]"),
        nested(LEVELS, '{"a":', "1", "}"),
        nested(LEVELS // 2, '[{"a":', "null", "}]"),
        nested(LEVELS - 1, "[", '{"a": "[[[[{{{{"}', "]"),
    )
    for data in texts:
        assert strictjson.loads(data, cap=ROOMY) == json.loads(data), data


def test_a_text_of_65_levels_is_too_deep() -> None:
    texts = (
        nested(ONE_MORE, "[", "", "]"),
        nested(ONE_MORE, "[", "1", "]"),
        nested(ONE_MORE, '{"a":', "1", "}"),
        nested(LEVELS, '{"a":', "[]", "}"),
        nested(LEVELS, "[", "{}", "]"),
        nested(LEVELS, "[", '{"a": "x"}', "]"),
    )
    for data in texts:
        refused = refusal(data)

        assert refused.rule is Rule.TOO_DEEP, data
        assert refused.offset is None


def test_a_deeper_text_is_too_deep() -> None:
    """The standard reader reads some of these depths, and it refuses some."""
    for levels in (66, 200, 1_000, 5_000, 20_000, 400_000):
        assert rule_of(nested(levels, "[", "", "]")) is Rule.TOO_DEEP, levels

    assert rule_of(nested(5_000, '{"a":', "1", "}")) is Rule.TOO_DEEP


def test_the_depth_counts_only_what_is_open_at_one_point() -> None:
    siblings = b"[" + b"[]," * 999 + b"[]]"
    deep = nested(LEVELS - 1, "[", "", "]")
    deep_siblings = b"[" + (deep + b",") * 99 + deep + b"]"

    assert strictjson.loads(siblings, cap=ROOMY) == [[]] * 1000
    assert strictjson.loads(deep_siblings, cap=ROOMY) == json.loads(deep_siblings)


def test_a_bracket_in_a_string_is_no_level() -> None:
    opened = "[" * 200 + "{" * 200
    texts = (
        f'["{opened}"]',
        f'{{"{opened}": "{opened}"}}',
        # A quote and a backslash before the brackets, each as an escape.
        f'["\\"{opened}", "\\\\", "{opened}\\\\\\""]',
        f'["a\\\\\\\\", "{opened}"]',
        f'["\\u005b{opened}\\n"]',
    )
    for text in texts:
        assert strictjson.loads(text.encode(), cap=ROOMY) == json.loads(text), text


def test_a_string_with_brackets_does_not_hide_a_level() -> None:
    closed = "]" * 200 + "}" * 200
    texts = (
        nested(ONE_MORE, "[", f'"{closed}"', "]"),
        nested(LEVELS, f'["{closed}",', "[]", "]"),
        # A quote and a backslash before the brackets, each as an escape.
        nested(LEVELS, "[", f'{{"\\"{closed}": "\\\\"}}', "]"),
        nested(LEVELS, f'["\\\\", "\\"{closed}",', "[]", "]"),
    )
    for data in texts:
        assert json.loads(data) is not None
        assert rule_of(data) is Rule.TOO_DEEP, data


# --- rule 7: the integers --------------------------------------------------------------


def test_each_integer_in_the_range_is_strict() -> None:
    for text in (
        "0",
        "9007199254740993",
        "9223372036854775807",
        "9223372036854775808",
        "18446744073709551615",
        "-9223372036854775808",
        "-9223372036854775807",
        "123456789012345678",
        "1234567890123456789",
    ):
        got = strictjson.loads(text.encode(), cap=ROOMY)

        assert type(got) is int
        assert got == int(text)
        assert strictjson.loads(f'{{"a": [{text}]}}'.encode(), cap=ROOMY) == {"a": [int(text)]}


def test_the_range_has_the_limits_of_64_bits() -> None:
    assert strictjson.INTEGER_MIN == -9223372036854775808
    assert strictjson.INTEGER_MAX == 18446744073709551615


def test_minus_zero_is_the_integer_zero() -> None:
    got = strictjson.loads(b"-0", cap=ROOMY)

    assert type(got) is int
    assert got == 0


#: An integer outside the range.
INTEGER_RANGE: tuple[bytes, ...] = (
    b"18446744073709551616",
    b"-9223372036854775809",
    b"-18446744073709551615",
    b"99999999999999999999",
    b"123456789012345678901234567890",
    b"-123456789012345678901234567890",
    b" 18446744073709551616",
    b"[1,18446744073709551616]",
    b'{"a":-9223372036854775809}',
    b'{"a": [0, {"b": 340282366920938463463374607431768211456}]}',
)


def test_an_integer_outside_the_range_is_refused() -> None:
    for data in INTEGER_RANGE:
        refused = refusal(data)

        assert refused.rule is Rule.INTEGER_RANGE, data
        assert refused.offset is None


def test_an_integer_with_more_digits_than_int_reads_is_out_of_range() -> None:
    digits = b"9" * TOO_MANY_DIGITS

    assert rule_of(digits) is Rule.INTEGER_RANGE
    assert rule_of(b"-" + digits) is Rule.INTEGER_RANGE
    assert rule_of(b'{"a": ' + digits + b"}") is Rule.INTEGER_RANGE
    assert rule_of(b"[" + b"9" * 500_000 + b"]") is Rule.INTEGER_RANGE


def test_a_long_run_of_digits_in_a_string_is_a_text() -> None:
    digits = "9" * TOO_MANY_DIGITS
    text = f'{{"{digits}": "{digits}", "e+{digits}": [1, 2.5, 18446744073709551615]}}'

    assert strictjson.loads(text.encode(), cap=ROOMY) == json.loads(text)


# --- rule 8: the floats ----------------------------------------------------------------


def test_each_finite_float_is_strict() -> None:
    for text in (
        "1e308",
        "1.7976931348623157e308",
        "-1.7976931348623157E+308",
        # A text above the largest float that rounds to that float.
        "1.7976931348623158e308",
        # An integer past 64 bits with a fraction is a float.
        "18446744073709551616.0",
        "1e19",
        "123456789012345678901234567890e-10",
        "0.1e+100",
    ):
        got = strictjson.loads(text.encode(), cap=ROOMY)

        assert type(got) is float
        assert got == float(text)
        assert math.isfinite(got)


def test_a_float_below_the_least_float_is_zero() -> None:
    assert repr(strictjson.loads(b"1e-999", cap=ROOMY)) == "0.0"
    assert repr(strictjson.loads(b"-1e-999", cap=ROOMY)) == "-0.0"
    assert repr(strictjson.loads(b"[1E-400]", cap=ROOMY)) == "[0.0]"


#: A float that is not finite.
FLOAT_RANGE: tuple[bytes, ...] = (
    b"1e999",
    b"-1e999",
    b"1E+999",
    b"1e309",
    b"2e308",
    b"1e0000000000000000309",
    # A text above the largest float that rounds to an infinity.
    b"1.7976931348623159e308",
    b"-1.7976931348623159e308",
    b"[1.0,1e400]",
    b'{"a":-1E+999}',
    b'{"a": [0.5, {"b": 1e+1000}]}',
)


def test_a_float_that_is_not_finite_is_refused() -> None:
    for data in FLOAT_RANGE:
        refused = refusal(data)

        assert refused.rule is Rule.FLOAT_RANGE, data
        assert refused.offset is None


def test_a_float_with_many_digits_before_its_fraction_is_refused() -> None:
    assert rule_of(b"1" + b"0" * 309 + b".0") is Rule.FLOAT_RANGE
    assert strictjson.loads(b"1" + b"0" * 308 + b".0", cap=ROOMY) == 1e308


def test_a_long_float_token_counts_each_digit_of_its_exponent() -> None:
    zeros = "0" * 66_000
    past_the_range = f"0.{zeros}1e655360"
    rounds_to_zero = f"1{zeros}e-655360"
    one = f"0.{'0' * 700_000}1e700001"

    assert rule_of(past_the_range.encode()) is Rule.FLOAT_RANGE
    assert repr(strictjson.loads(rounds_to_zero.encode(), cap=ROOMY)) == "0.0"
    assert repr(strictjson.loads(one.encode(), cap=ROOMY)) == "1.0"


# --- a text with two faults ------------------------------------------------------------


def test_a_text_with_two_faults_gets_the_word_of_the_first_fault_in_the_sequence() -> None:
    rows: tuple[tuple[bytes, Rule], ...] = (
        # The encoding is before each rule of the grammar.
        (b"[NaN\xff", Rule.NOT_UTF8),
        (b"\xef\xbb\xbf[1,]", Rule.BYTE_ORDER_MARK),
        # The read of the decoder is before a character after the value.
        (b"[NaN] x", Rule.CONSTANT),
        (b'{"a": 1, "a": 2} x', Rule.DUPLICATE_KEY),
        # The decoder gives the keys of an object at the end of the object.
        (b'{"a": 1, "a": NaN}', Rule.CONSTANT),
        (b'{"a": 1, "a": }', Rule.SYNTAX),
        # A character after the value is before the depth.
        (nested(ONE_MORE, "[", "", "]") + b" x", Rule.TRAILING_DATA),
        # The depth is before a lone surrogate.
        (nested(ONE_MORE, "[", r'"\ud800"', "]"), Rule.TOO_DEEP),
        # A lone surrogate is last.
        (rb'"\ud800" x', Rule.TRAILING_DATA),
        (rb'{"\ud800": 1, "\ud800": 2}', Rule.DUPLICATE_KEY),
        (rb'["\ud800", 1e999]', Rule.FLOAT_RANGE),
    )
    for data, rule in rows:
        assert rule_of(data) is rule, data


# --- the writer ------------------------------------------------------------------------

#: Each value that the writer takes.
WRITTEN: tuple[object, ...] = (
    None,
    True,
    False,
    0,
    -7,
    strictjson.INTEGER_MIN,
    strictjson.INTEGER_MAX,
    0.5,
    -0.0,
    1e308,
    5e-324,
    "",
    "a",
    'quote " backslash \\ slash / tab \t line \n',
    "caf\xe9 \U0001f600 \u2028",
    "\\ud800",
    [],
    [1, [2, [3]]],
    {},
    {"b": 1, "a": {"d": [None, True], "c": "x"}},
    {"written_at": "2999-01-01T00:00:00Z", "rows": [{"count": 3, "spend": 1.25}]},
    (1, 2),
    {2: "a", 1: "b"},
)


def test_the_writer_gives_the_text_of_the_standard_writer() -> None:
    for value in WRITTEN:
        assert strictjson.dumps(value) == json.dumps(value)
        assert strictjson.dumps(value, indent=1, sort_keys=True) == json.dumps(
            value, indent=1, sort_keys=True
        )
        assert strictjson.dumps(value, indent=2) == json.dumps(value, indent=2)
        assert strictjson.dumps(value, indent="\t") == json.dumps(value, indent="\t")
        assert strictjson.dumps(value, separators=(",", ":"), ensure_ascii=False) == json.dumps(
            value, separators=(",", ":"), ensure_ascii=False
        )
        assert strictjson.dumps(value, sort_keys=True, ensure_ascii=False) == json.dumps(
            value, sort_keys=True, ensure_ascii=False
        )


def test_the_reader_reads_each_text_of_the_writer() -> None:
    for value in WRITTEN:
        for text in (
            strictjson.dumps(value),
            strictjson.dumps(value, indent=1, sort_keys=True),
            strictjson.dumps(value, separators=(",", ":"), ensure_ascii=False),
        ):
            assert repr(strictjson.loads(text.encode(), cap=ROOMY)) == repr(json.loads(text))


def test_the_writer_gives_a_key_that_is_no_text_the_text_of_the_standard_writer() -> None:
    value = {1: "a", 2.5: "b", False: "c", None: "d"}

    assert strictjson.dumps(value) == '{"1": "a", "2.5": "b", "false": "c", "null": "d"}'
    assert strictjson.dumps(value) == json.dumps(value)


def test_the_arguments_of_the_writer_have_names() -> None:
    dumps: Callable[..., str] = strictjson.dumps

    with pytest.raises(TypeError):
        dumps({}, 1)

    assert dumps({}, indent=1) == "{}"


def deep_list(levels: int) -> object:
    value: object = []
    for _ in range(levels - 1):
        value = [value]

    return value


def test_the_writer_writes_64_levels() -> None:
    value = deep_list(LEVELS)

    assert strictjson.dumps(value) == "[" * LEVELS + "]" * LEVELS


def circle() -> object:
    value: list[object] = []
    value.append(value)

    return value


#: A value whose text breaks a rule, with the rule.
NOT_WRITTEN: tuple[tuple[Callable[[], object], Rule], ...] = (
    (lambda: math.nan, Rule.CONSTANT),
    (lambda: math.inf, Rule.CONSTANT),
    (lambda: -math.inf, Rule.CONSTANT),
    (lambda: {"spend": [1.5, math.nan]}, Rule.CONSTANT),
    (lambda: strictjson.INTEGER_MAX + 1, Rule.INTEGER_RANGE),
    (lambda: strictjson.INTEGER_MIN - 1, Rule.INTEGER_RANGE),
    (lambda: [{"count": 10**30}], Rule.INTEGER_RANGE),
    (lambda: 10**TOO_MANY_DIGITS, Rule.INTEGER_RANGE),
    (lambda: {"a": [-(10**5000)]}, Rule.INTEGER_RANGE),
    (lambda: "\ud800", Rule.LONE_SURROGATE),
    (lambda: ["a", "\udfff"], Rule.LONE_SURROGATE),
    (lambda: {"\udc00": 1}, Rule.LONE_SURROGATE),
    (lambda: "\ud83d \ude00", Rule.LONE_SURROGATE),
    (lambda: deep_list(ONE_MORE), Rule.TOO_DEEP),
    (lambda: deep_list(100_000), Rule.TOO_DEEP),
    (circle, Rule.TOO_DEEP),
)


def test_the_writer_refuses_a_value_whose_text_is_not_strict() -> None:
    styles: tuple[Callable[[object], str], ...] = (
        strictjson.dumps,
        lambda value: strictjson.dumps(value, indent=1, sort_keys=True),
        lambda value: strictjson.dumps(value, separators=(",", ":"), ensure_ascii=False),
    )
    for make, rule in NOT_WRITTEN:
        for write in styles:
            with pytest.raises(NotStrictJson) as caught:
                write(make())

            assert caught.value.rule is rule
            assert caught.value.offset is None
            assert caught.value.__cause__ is None
            assert caught.value.__context__ is None


def test_the_writer_refuses_two_keys_that_have_one_text() -> None:
    """The standard writer writes the key 1 and the key "1" as one text."""
    for value in ({1: "a", "1": "b"}, {None: "a", "null": "b"}, [{2.5: 1, "2.5": 2}]):
        assert json.loads(json.dumps(value)) is not None
        with pytest.raises(NotStrictJson) as caught:
            strictjson.dumps(value)

        assert caught.value.rule is Rule.DUPLICATE_KEY
        assert caught.value.offset is None


def test_the_writer_never_writes_null_for_a_float_that_is_not_finite() -> None:
    """The standard writer gives the word `NaN` for this value."""
    assert json.dumps([math.nan]) == "[NaN]"
    with pytest.raises(NotStrictJson):
        strictjson.dumps([math.nan])


def test_the_writer_refuses_separators_that_make_no_json_text() -> None:
    with pytest.raises(NotStrictJson) as caught:
        strictjson.dumps({"a": [1, 2]}, separators=("; ", " = "))

    assert caught.value.rule is Rule.SYNTAX


def test_a_value_with_no_json_text_keeps_the_error_of_the_standard_writer() -> None:
    for value in ({1, 2}, b"bytes", {(1, 2): 3}, object()):
        with pytest.raises(TypeError):
            strictjson.dumps(value)


# --- the cost --------------------------------------------------------------------------

#: The most that the strict reader can cost, as a multiple of the standard reader.
COST_MAX = 10

RECORD = (
    '{"id": "01J9ZQ3V5K8M2N4P6R7S8T9V0W", "state": "ready", "count": 42, "spend": 3.25, '
    '"ok": true, "tags": ["a", "b"], "note": null, "text": "line one\\nline two \\u00e9"}'
)

#: The item of each text of the cost test. The last three need more work of
#: the strict reader than a common text: a string that is long, an escape of
#: a surrogate pair, and an integer of 20 digits.
UNITS = (
    RECORD,
    "1234567",
    '{"a":1}',
    '"' + "x" * 1000 + '"',
    '"\\ud83d\\ude00 \\n"',
    "18446744073709551615",
)


def filled(unit: str) -> bytes:
    """A strict text of exactly one mebibyte: an array of `unit`, then spaces."""
    count = (MEBIBYTE - 2) // (len(unit.encode()) + 1)
    data = f"[{','.join([unit] * count)}]".encode()

    return data + b" " * (MEBIBYTE - len(data))


def cpu_seconds(call: Callable[[], object]) -> float:
    """The least processor time of five runs of `call`."""
    runs: list[float] = []
    for _ in range(5):
        start = time.process_time()
        call()
        runs.append(time.process_time() - start)

    return min(runs)


@pytest.mark.parametrize("unit", UNITS)
def test_a_strict_text_of_one_mebibyte_costs_ten_standard_reads_at_most(unit: str) -> None:
    data = filled(unit)
    assert len(data) == MEBIBYTE
    assert strictjson.loads(data, cap=MEBIBYTE) == json.loads(data)

    standard = cpu_seconds(lambda: json.loads(data))
    strict = cpu_seconds(lambda: strictjson.loads(data, cap=MEBIBYTE))

    assert strict <= COST_MAX * standard, f"{strict:.4f} s against {standard:.4f} s"
