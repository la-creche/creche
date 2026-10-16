"""The inputs and the committed file of the group `tomlfile`.

No test here builds the vectors. A test reads the lists of
`surfaces/tomlfile.py`, calls one of its functions, or reads the committed
file.
"""

from __future__ import annotations

import json
import math
import tomllib
from typing import cast

import pytest
from agent_family.tomlfile import BYTE_ORDER_MARK, NOT_TOML, NOT_UTF8, TOO_LARGE

from vectors import generate
from vectors.core import ACCEPTED, REFUSED, Json, depth, expand
from vectors.surfaces import tomlfile

#: The vector file of the surface `tomlfile.syntax`.
SYNTAX_FILE = "tomlfile/syntax.json"

#: The range of an integer that a reader of a file kind takes: 64 bits with
#: a sign.
INTEGER_MIN = -(2**63)
INTEGER_MAX = 2**63 - 1

#: The most levels of a text that a reader of a file kind takes. The top
#: table has level 1.
LEVEL_MAX = 8

#: The words of a TOML float that is not finite.
NOT_FINITE_WORDS = ("inf", "nan")

#: A key two times, in the five forms that the surface must hold.
KEY_TWO_TIMES = (
    "key-two-times",
    "table-two-times",
    "dotted-key-over-a-value",
    "key-two-times-in-an-inline-table",
    "table-over-a-scalar",
)

#: The entry point of the surface: the TOML reader of a file kind.
ENTRY = "agent_family.tomlfile.loads"

#: The four texts of a refusal, for the kind that the surface reads.
REFUSALS = frozenset(
    (
        TOO_LARGE.format(limit=tomlfile.KIND.byte_limit),
        NOT_UTF8,
        BYTE_ORDER_MARK,
        NOT_TOML,
    )
)

#: The six forms that TOML 1.1 added, by the end of their ids. The surface
#: holds each one at the top level and inside a list of inline tables.
TOML11_FORMS = {
    "toml11-line-end-in-an-inline-table": "toml11-line-end-in-a-list-of-inline-tables",
    "toml11-comma-before-the-end-of-an-inline-table": "toml11-comma-in-a-list-of-inline-tables",
    "toml11-escape-e": "toml11-escape-e-in-a-list-of-inline-tables",
    "toml11-escape-x": "toml11-escape-x-in-a-list-of-inline-tables",
    "toml11-time-with-no-seconds": "toml11-time-in-a-list-of-inline-tables",
    "toml11-date-time-with-no-seconds": "toml11-date-time-in-a-list-of-inline-tables",
}

#: The four forms of a level: a list, an inline table, a table header and a
#: dotted key. The surface takes 8 levels of each and refuses 9.
LEVEL_FORMS = ("lists", "inline-tables", "tables", "a-dotted-key")

#: The three integers just outside 64 bits with a sign.
INTEGERS_OUTSIDE = ("integer-2-to-the-63", "integer-below-the-least", "integer-2-to-the-64")

#: The first five numbers below 1000 from the seed of the committed file.
FIRST_NUMBERS = [694, 509, 962, 41, 282]

#: The first number of the seed 0, below 2^32: the high half of the
#: increment of the linear step.
FIRST_OF_ZERO = 335903614


class Given:
    """A fixed sequence of numbers in the place of `tomlfile.Numbers`."""

    def __init__(self, *numbers: int) -> None:
        self._left = list(numbers)

    def below(self, bound: int) -> int:
        number = self._left.pop(0)
        assert number < bound
        return number


def _committed() -> list[dict[str, Json]]:
    document = cast("dict[str, Json]", json.loads(generate.committed()[SYNTAX_FILE]))
    return cast("list[dict[str, Json]]", document["vectors"])


#: One text for each kind of a date-time.
MOMENTS = (
    ("a = 1979-05-27T07:32:00Z\n", tomlfile.OFFSET_DATE_TIME),
    ("a = 1979-05-27T07:32:00-07:00\n", tomlfile.OFFSET_DATE_TIME),
    ("a = 1979-05-27T07:32:00\n", tomlfile.LOCAL_DATE_TIME),
    ("a = 1979-05-27\n", tomlfile.LOCAL_DATE),
    ("a = 07:32:00\n", tomlfile.LOCAL_TIME),
)


def _scalars(value: object) -> list[object]:
    """Each value of a parsed text that is no table and no list."""
    if isinstance(value, dict):
        return [
            found for item in cast("dict[str, object]", value).values() for found in _scalars(item)
        ]

    if isinstance(value, list):
        return [found for item in cast("list[object]", value) for found in _scalars(item)]

    return [value]


def test_the_committed_file_holds_each_input_of_the_lists() -> None:
    vectors = _committed()
    listed = [(text.id, ACCEPTED) for text in tomlfile.TAKEN]
    listed += [(text.id, REFUSED) for text in tomlfile.NOT_TAKEN]
    listed += [(item.id, wanted) for item, wanted in tomlfile.LIMITS]
    changed = vectors[len(listed) :]

    assert [(vector["id"], vector["result"]) for vector in vectors[: len(listed)]] == listed
    assert [vector["id"] for vector in changed] == [
        text.id for text in tomlfile.changed_texts(tomlfile.SEED, tomlfile.CHANGED_COUNT)
    ]


def test_the_committed_file_holds_500_changed_texts() -> None:
    ids = [vector["id"] for vector in _committed() if str(vector["id"]).startswith("changed-")]

    assert tomlfile.CHANGED_COUNT == 500
    assert ids == [f"changed-{index:03d}" for index in range(500)]


def test_the_entry_point_is_the_reader_of_a_file_kind() -> None:
    document = cast("dict[str, Json]", json.loads(generate.committed()[SYNTAX_FILE]))

    assert document["entry"] == ENTRY
    assert tomlfile.KIND.byte_limit == max(kind.byte_limit for kind in type(tomlfile.KIND))


def test_a_refused_vector_holds_one_of_the_four_texts() -> None:
    refusals = {str(vector["refusal"]) for vector in _committed() if vector["result"] == REFUSED}

    assert refusals == REFUSALS


def test_the_surface_holds_each_form_of_toml_11_in_two_places() -> None:
    refused = {text.id for text in tomlfile.NOT_TAKEN}

    assert refused >= set(TOML11_FORMS) | set(TOML11_FORMS.values())


def test_the_surface_holds_8_and_9_levels_of_each_form() -> None:
    taken = {text.id for text in tomlfile.TAKEN}
    refused = {text.id for text in tomlfile.NOT_TAKEN}

    for form in LEVEL_FORMS:
        assert f"levels-8-of-{form}" in taken
        assert f"levels-9-of-{form}" in refused


def test_the_surface_holds_the_integers_just_outside_64_bits() -> None:
    refused = {text.id: text.text for text in tomlfile.NOT_TAKEN}
    numbers = {int(refused[name].removeprefix("a = ")) for name in INTEGERS_OUTSIDE}

    assert numbers == {2**63, -(2**63) - 1, 2**64}


def test_the_limits_hold_the_byte_limit_of_the_kind() -> None:
    limit = tomlfile.KIND.byte_limit
    lengths = {
        item.id: len(expand(item.parts).encode("utf-8"))
        for item, _ in tomlfile.LIMITS
        if isinstance(item, tomlfile.Parts)
    }

    assert lengths == {"bytes-at-the-limit": limit, "bytes-one-over-the-limit": limit + 1}


def test_each_raw_input_is_no_utf8_text() -> None:
    raws = [item for item, _ in tomlfile.LIMITS if isinstance(item, tomlfile.Raw)]

    assert raws
    for item in raws:
        with pytest.raises(UnicodeDecodeError):
            item.data.decode("utf-8")


def test_the_numbers_are_one_fixed_linear_step() -> None:
    numbers = tomlfile.Numbers(tomlfile.SEED)
    zero = tomlfile.Numbers(0)

    assert [numbers.below(1000) for _ in FIRST_NUMBERS] == FIRST_NUMBERS
    assert zero.below(2**32) == FIRST_OF_ZERO


def test_a_larger_count_starts_with_the_changed_texts_of_a_smaller_one() -> None:
    small = tomlfile.changed_texts(tomlfile.SEED, 10)
    large = tomlfile.changed_texts(tomlfile.SEED, 50)

    assert large[:10] == small
    assert len({text.id for text in large}) == len(large)


def test_each_change_makes_one_change() -> None:
    text = "ab\ncd"
    hash_sign = tomlfile.ALPHABET.index("#")
    changes = [
        (tomlfile.Change.DELETE, Given(1), "a\ncd"),
        (tomlfile.Change.INSERT, Given(2, hash_sign), "ab#\ncd"),
        (tomlfile.Change.SWAP, Given(0, 2), "cb\nad"),
        (tomlfile.Change.SWAP, Given(3, 0), "ab\ndc"),
        (tomlfile.Change.SWAP, Given(4, 0), "db\nca"),
        (tomlfile.Change.COPY_LINE, Given(1, 0), "cd\nab\ncd"),
        (tomlfile.Change.COPY_LINE, Given(0, 2), "ab\ncd\nab"),
    ]

    for change, numbers, wanted in changes:
        assert tomlfile.changed(text, change, cast("tomlfile.Numbers", numbers)) == wanted


def test_a_text_too_short_for_its_change_gets_an_insert() -> None:
    first = tomlfile.ALPHABET[0]

    for change in (tomlfile.Change.DELETE, tomlfile.Change.SWAP):
        numbers = cast("tomlfile.Numbers", Given(0, 0))
        assert tomlfile.changed("", change, numbers) == first

    numbers = cast("tomlfile.Numbers", Given(1, 0))
    assert tomlfile.changed("a", tomlfile.Change.SWAP, numbers) == f"a{first}"


def test_an_exception_that_is_no_refusal_stops_the_build() -> None:
    def broken(data: bytes) -> object:
        raise KeyError(len(data))

    text = tomlfile.Text("broken", "a = 1\n")

    with pytest.raises(ValueError, match="raises KeyError for the text broken"):
        tomlfile.vector_of(text, None, broken)


def test_the_surface_holds_a_key_two_times_in_each_form() -> None:
    refused = {text.id for text in tomlfile.NOT_TAKEN}

    assert refused >= set(KEY_TWO_TIMES)


@pytest.mark.parametrize(("text", "kind"), MOMENTS)
def test_a_date_time_is_its_kind_and_null(text: str, kind: str) -> None:
    assert tomlfile.plain(tomllib.loads(text)) == {"a": {kind: None}}


def test_a_date_time_in_a_list_and_in_a_table_is_its_kind() -> None:
    parsed = tomllib.loads("a = [1979-05-27, {b = 07:32:00}]\nc = 1\n")

    assert tomlfile.plain(parsed) == {
        "a": [{tomlfile.LOCAL_DATE: None}, {"b": {tomlfile.LOCAL_TIME: None}}],
        "c": 1,
    }


def test_a_text_with_another_result_than_its_list_stops_the_build() -> None:
    taken = tomlfile.Text("taken", "a = 1\n")
    not_taken = tomlfile.Text("not-taken", "a = \n")

    assert tomlfile.vector_of(taken, ACCEPTED).body["result"] == ACCEPTED
    assert tomlfile.vector_of(not_taken, REFUSED).body["result"] == REFUSED

    with pytest.raises(ValueError, match="taken another result"):
        tomlfile.vector_of(taken, REFUSED)

    with pytest.raises(ValueError, match="not-taken another result"):
        tomlfile.vector_of(not_taken, ACCEPTED)


def test_no_taken_text_has_a_number_outside_the_range() -> None:
    """`tomllib` reads an integer of each size and a float text outside the
    range of a float. A reader of a file kind refuses both, so `NOT_TAKEN`
    holds such texts and `TAKEN` holds none."""
    for text in tomlfile.TAKEN:
        parsed = tomllib.loads(text.text)
        scalars = _scalars(parsed)
        integers = [item for item in scalars if isinstance(item, int)]
        floats = [item for item in scalars if isinstance(item, float)]
        says_not_finite = any(word in text.text for word in NOT_FINITE_WORDS)

        assert all(INTEGER_MIN <= item <= INTEGER_MAX for item in integers), text.id
        assert says_not_finite or all(math.isfinite(item) for item in floats), text.id


def test_the_deepest_listed_text_has_8_levels() -> None:
    """`tomllib` reads a text of each depth, and a reader of a file kind
    refuses a text of 9 levels. A date-time is one object more in a vector.
    No listed text holds one at level 8, so the depth of a value is the count
    of its levels."""
    deepest = max(
        depth(cast("Json", tomlfile.plain(tomllib.loads(text.text)))) for text in tomlfile.TAKEN
    )

    assert deepest == LEVEL_MAX
