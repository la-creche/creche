"""The TOML reader of a file kind: its limits, its four texts and its checks.

The reader takes the bytes of one file and gives a table or one refusal.
`_only_toml10` holds the six forms that TOML 1.1 added. The TOML reader of
the Python standard library refuses those forms up to Python 3.14 and reads
them from Python 3.15 on. So the tests of the function call it directly, and
each one holds under each Python version."""

from __future__ import annotations

import math
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from agent_family import tomlfile
from agent_family.tomlfile import DEPTH, FileKind, TomlRefusal, loads

NOT_TOML = tomlfile.NOT_TOML

#: The byte limit of each file kind.
LIMITS = {
    FileKind.FAMILY: 65536,
    FileKind.SERVER: 65536,
    FileKind.MANIFEST: 16384,
    FileKind.ROSTER: 1048576,
}

#: The name of the byte limit of a kind, for a write that must fail.
LIMIT_FIELD = "byte_limit"

#: One text of each of the six forms that TOML 1.1 added.
TOML11_FORMS = {
    "line-end-in-inline-table": "a = {b = 1,\n c = 2}\n",
    "comma-before-brace": "a = {b = 1,}\n",
    "escape-e": 'a = "\\e"\n',
    "escape-x": 'a = "\\x41"\n',
    "time-no-seconds": "a = 07:32\n",
    "date-time-no-seconds": "a = 1979-05-27T07:32Z\n",
}

#: The same six forms at other places of a text.
TOML11_FORMS_ELSEWHERE = {
    "line-end-after-comment": "a = { # note\n b = 1 }\n",
    "line-end-with-cr-lf": "a = {b = 1,\r\n c = 2}\r\n",
    "line-end-in-table-of-array": "a = [{b = 1,\n c = 2}]\n",
    "comma-before-brace-with-spaces": "a = {b = 1, \t }\n",
    "comma-before-brace-in-array": "a = [{b = 1,}]\n",
    "escape-e-multi-line": 'a = """\\e"""\n',
    "escape-x-multi-line": 'a = """\nx\\x41"""\n',
    "escape-e-in-key": '"\\e" = 1\n',
    "time-no-seconds-in-array": "a = [07:32]\n",
    "date-time-no-seconds-with-offset": "a = 1979-05-27T07:32+01:00\n",
    "local-date-time-no-seconds": "a = 1979-05-27T07:32\n",
    "date-time-no-seconds-with-space": "a = 1979-05-27 07:32\n",
    "time-no-seconds-then-comment": "a = 07:32#c\n",
    "date-time-no-seconds-then-comment": "a = 1979-05-27T07:32#c\n",
    "after-a-string-of-four-quotes": 'a = """x""""\nb = 07:32\n',
    "after-a-string-of-five-quotes": "a = '''x'''''\nb = {c = 1,}\n",
}

#: The characters of the six forms where TOML 1.0 permits them.
TOML10_TEXTS = {
    "in-a-basic-string": 'a = "{b = 1,} 07:32 1979-05-27T07:32Z"\n',
    "in-a-literal-string": "a = '\\e \\x41 {b = 1,} 07:32'\n",
    "in-a-multi-line-basic-string": 'a = """\n{b = 1,\n} 07:32\n"""\n',
    "in-a-multi-line-literal-string": "a = '''\n\\e \\x41 {b = 1,\n}\n'''\n",
    "in-a-comment": "a = 1 # {b = 1,} \\e \\x41 07:32 1979-05-27T07:32Z\n",
    "in-a-quoted-key": '"{b = 1,} 07:32" = 1\n',
    "line-end-in-array-of-inline-table": "a = {b = [\n1,\n2,\n]}\n",
    "string-in-inline-table": 'a = {b = "x,", c = "{"}\n',
    "times-with-seconds": "a = 07:32:00\nb = 1979-05-27T07:32:00-08:00\nc = 00:32:00.999\n",
    "date-time-with-space": "a = 1979-05-27 07:32:00Z\n",
    "trailing-comma-of-array": "a = [1, 2, ]\n",
    "escaped-backslash-then-e": 'a = "\\\\e \\\\x41"\n',
    "escaped-quote": 'a = "\\"{b = 1,}"\n',
    "string-of-four-quotes": 'a = """x""""\nb = "07:32"\n',
    "string-of-five-quotes": 'a = """x"""""\nb = "{c = 1,}"\n',
    "literal-of-five-quotes": "a = '''x'''''\nb = '07:32'\n",
}


def _refusal(data: bytes, kind: FileKind = FileKind.FAMILY) -> str:
    with pytest.raises(TomlRefusal) as caught:
        loads(data, kind)

    return str(caught.value)


def _deep_array(levels: int) -> bytes:
    """A text whose deepest array has the level `levels`."""
    arrays = levels - 1
    return ("a = " + "[" * arrays + "1" + "]" * arrays + "\n").encode()


def _deep_inline_table(levels: int) -> bytes:
    tables = levels - 1
    return ("a = " + "{b = " * tables + "1" + "}" * tables + "\n").encode()


def _deep_header(levels: int) -> bytes:
    parts = ".".join(f"k{index}" for index in range(levels - 1))
    return f"[{parts}]\nx = 1\n".encode()


def _deep_dotted_key(levels: int) -> bytes:
    parts = ".".join(f"k{index}" for index in range(levels))
    return f"{parts} = 1\n".encode()


def _sized(size: int) -> bytes:
    """A good text of exactly `size` bytes."""
    frame = len(b'a = ""\n')
    return b'a = "' + b"x" * (size - frame) + b'"\n'


# --- the limits ---------------------------------------------------------------


def test_each_kind_has_its_byte_limit() -> None:
    assert {kind: kind.byte_limit for kind in FileKind} == LIMITS


@pytest.mark.parametrize("kind", list(FileKind), ids=lambda kind: kind.name)
def test_a_caller_cannot_change_the_byte_limit_of_a_kind(kind: FileKind) -> None:
    with pytest.raises(AttributeError):
        setattr(kind, LIMIT_FIELD, 10**9)

    assert kind.byte_limit == LIMITS[kind]
    assert loads(_sized(LIMITS[kind]), kind) == {"a": "x" * (LIMITS[kind] - 7)}


def test_the_level_limit_is_8() -> None:
    assert DEPTH == 8


@pytest.mark.parametrize("kind", list(FileKind), ids=lambda kind: kind.name)
def test_a_file_at_the_byte_limit_passes(kind: FileKind) -> None:
    assert loads(_sized(LIMITS[kind]), kind) == {"a": "x" * (LIMITS[kind] - 7)}


@pytest.mark.parametrize("kind", list(FileKind), ids=lambda kind: kind.name)
def test_one_byte_more_than_the_limit_is_refused(kind: FileKind) -> None:
    message = _refusal(_sized(LIMITS[kind] + 1), kind)

    assert message == f"the file has more than {LIMITS[kind]} bytes"


def test_the_size_is_checked_before_the_encoding() -> None:
    message = _refusal(_sized(65536) + b"\xff")

    assert message == "the file has more than 65536 bytes"


@pytest.mark.parametrize(
    "data",
    [b'a = "\xff"\n', b'a = "\xed\xa0\x80"\n', b'a = "\xc0\xaf"\n', b'a = "\xe2\x82"\n'],
    ids=["byte-ff", "surrogate", "overlong", "cut"],
)
def test_a_byte_that_is_no_utf8_is_refused(data: bytes) -> None:
    assert _refusal(data) == "the file is not UTF-8 text"


def test_a_byte_order_mark_is_refused() -> None:
    assert _refusal(b"\xef\xbb\xbfa = 1\n") == "the file starts with a byte order mark"


def test_the_encoding_is_checked_before_the_byte_order_mark() -> None:
    assert _refusal(b"\xef\xbb\xbfa = '\xff'\n") == "the file is not UTF-8 text"


def test_a_duplicate_key_is_refused() -> None:
    assert _refusal(b"a = 1\na = 2\n") == NOT_TOML


def test_the_text_of_message_4() -> None:
    assert NOT_TOML == "the text is not TOML 1.0, or it nests deeper than 8 levels"


@pytest.mark.parametrize(
    "text",
    [
        "a = 9223372036854775808\n",
        "a = -9223372036854775809\n",
        "a = 0xFFFFFFFFFFFFFFFF\n",
        "a = [1, 18446744073709551616]\n",
        "a = {b = {c = 9223372036854775808}}\n",
        "[[a]]\nb = 9223372036854775808\n",
    ],
    ids=["2-63", "minus-2-63-minus-1", "hex", "in-array", "in-inline-table", "in-array-of-tables"],
)
def test_an_integer_outside_64_bits_is_refused(text: str) -> None:
    assert _refusal(text.encode()) == NOT_TOML


def test_the_integers_at_the_ends_of_64_bits_pass() -> None:
    table = loads(b"a = 9223372036854775807\nb = -9223372036854775808\n", FileKind.FAMILY)

    assert table == {"a": 2**63 - 1, "b": -(2**63)}


@pytest.mark.parametrize(
    "text",
    ["a = 1e999\n", "a = -1e999\n", "a = 1_000e400\n", "a = [1e999]\n", "a = {b = 9e999}\n"],
    ids=["1e999", "minus", "underscore", "in-array", "in-inline-table"],
)
def test_a_float_text_with_digits_and_no_finite_value_is_refused(text: str) -> None:
    assert _refusal(text.encode()) == NOT_TOML


def test_the_float_words_of_toml_pass() -> None:
    table = loads(b"a = inf\nb = -inf\nc = +inf\nd = nan\ne = -nan\nf = +nan\n", FileKind.FAMILY)

    assert table["a"] == table["c"] == math.inf
    assert table["b"] == -math.inf
    assert all(math.isnan(table[key]) for key in ("d", "e", "f"))


def test_a_float_text_that_is_too_small_reads_as_zero() -> None:
    assert loads(b"a = 1e-999\n", FileKind.FAMILY) == {"a": 0.0}


@pytest.mark.parametrize(
    "deep",
    [_deep_array, _deep_inline_table, _deep_header, _deep_dotted_key],
    ids=["array", "inline-table", "header", "dotted-key"],
)
def test_8_levels_pass_and_9_levels_are_refused(deep: Callable[[int], bytes]) -> None:
    loads(deep(DEPTH), FileKind.FAMILY)
    assert _refusal(deep(DEPTH + 1)) == NOT_TOML


def test_an_array_of_tables_counts_two_levels() -> None:
    six = ".".join(f"k{index}" for index in range(6))
    seven = ".".join(f"k{index}" for index in range(7))

    loads(f"[[{six}]]\nx = 1\n".encode(), FileKind.FAMILY)
    assert _refusal(f"[[{seven}]]\nx = 1\n".encode()) == NOT_TOML


def test_an_array_of_30000_levels_is_refused() -> None:
    assert _refusal(_deep_array(30_001)) == NOT_TOML


def test_a_header_of_30000_parts_is_refused() -> None:
    assert _refusal(_deep_header(30_001), FileKind.ROSTER) == NOT_TOML


@pytest.mark.parametrize("name", list(TOML11_FORMS))
def test_loads_refuses_each_form_of_toml11(name: str) -> None:
    assert _refusal(TOML11_FORMS[name].encode()) == NOT_TOML


@pytest.mark.parametrize("name", list(TOML11_FORMS))
def test_loads_refuses_each_form_that_a_toml11_parser_reads(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The parser of Python 3.15 reads TOML 1.1. A stand-in for it shows
    that `loads` gives each text that the parser read to the check."""
    read: list[str] = []

    def parse(text: str, parse_float: Callable[[str], Any]) -> dict[str, Any]:
        read.append(text)
        return {"a": 1}

    monkeypatch.setattr(tomlfile, "tomllib", SimpleNamespace(loads=parse))

    assert _refusal(TOML11_FORMS[name].encode()) == NOT_TOML
    assert read == [TOML11_FORMS[name]]


def test_the_escape_of_a_lone_surrogate_is_refused() -> None:
    assert _refusal(b'a = "\\ud800"\n') == NOT_TOML


@pytest.mark.parametrize(
    "text",
    ["a = 1990-12-31T23:59:60Z\n", "a = 0000-01-01\n"],
    ids=["second-60", "year-0"],
)
def test_a_moment_that_python_cannot_hold_is_refused(text: str) -> None:
    assert _refusal(text.encode()) == NOT_TOML


def test_a_dotted_key_through_an_array_of_tables_is_refused() -> None:
    assert _refusal(b"[[a.b]]\n[a]\nb.c.d = 1\n") == NOT_TOML


@pytest.mark.parametrize(
    "data",
    [b"a = ", b"[", b"a = {", b'a = "\\', b"a = '''", b"= 1", b"a = 1 b = 2", b"a = \r1"],
    ids=[
        "no-value",
        "open-header",
        "open-table",
        "open-escape",
        "open-literal",
        "no-key",
        "two-pairs",
        "lone-cr",
    ],
)
def test_a_text_that_is_no_toml_is_refused(data: bytes) -> None:
    assert _refusal(data) == NOT_TOML


# --- what loads gives ---------------------------------------------------------


def test_each_table_has_its_keys_in_code_point_order() -> None:
    text = 'b = 1\nZ = 2\n"é" = 3\na = {d = 1, c = [{y = 1, x = 2}]}\n[[t]]\nn = 1\nm = 2\n'

    table = loads(text.encode(), FileKind.FAMILY)

    assert list(table) == ["Z", "a", "b", "t", "é"]
    assert list(table["a"]) == ["c", "d"]
    assert list(table["a"]["c"][0]) == ["x", "y"]
    assert list(table["t"][0]) == ["m", "n"]


def test_a_cr_lf_in_a_multi_line_string_reads_as_one_line_feed() -> None:
    assert loads(b'a = """x\r\ny"""\r\n', FileKind.FAMILY) == {"a": "x\ny"}


def test_an_empty_text_is_an_empty_table() -> None:
    assert loads(b"", FileKind.FAMILY) == {}


def test_a_refusal_is_a_value_error() -> None:
    assert issubclass(TomlRefusal, ValueError)


def test_a_refusal_names_no_text_of_the_file() -> None:
    secret = "s3cr3t-value"

    message = _refusal(f'a = "{secret}"\na = 1\n'.encode())

    assert secret not in message
    assert "line" not in message


# --- the check of the six forms of TOML 1.1 -----------------------------------


@pytest.mark.parametrize("name", list(TOML11_FORMS))
def test_each_form_of_toml11_fails_the_check(name: str) -> None:
    assert tomlfile._only_toml10(TOML11_FORMS[name]) is False


@pytest.mark.parametrize("name", list(TOML11_FORMS_ELSEWHERE))
def test_each_form_at_another_place_fails_the_check(name: str) -> None:
    assert tomlfile._only_toml10(TOML11_FORMS_ELSEWHERE[name]) is False


@pytest.mark.parametrize("name", list(TOML10_TEXTS))
def test_the_characters_where_toml10_permits_them_pass_the_check(name: str) -> None:
    assert tomlfile._only_toml10(TOML10_TEXTS[name]) is True


@pytest.mark.parametrize("name", list(TOML10_TEXTS))
def test_loads_reads_each_text_of_toml10(name: str) -> None:
    loads(TOML10_TEXTS[name].encode(), FileKind.FAMILY)


@pytest.mark.parametrize(
    "text",
    ["", "{", "}", "]", "[[", '"', '"\\', "'''", '"""""', "#", "\\", "a = {\n"[:5], "{,}"],
)
def test_the_check_never_raises(text: str) -> None:
    assert tomlfile._only_toml10(text) in (True, False)
