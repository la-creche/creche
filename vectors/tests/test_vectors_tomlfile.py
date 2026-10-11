"""The inputs and the committed file of the group `tomlfile`.

No test here builds the vectors. A test reads the two lists of
`surfaces/tomlfile.py`, or the committed file.
"""

from __future__ import annotations

import json
import math
import tomllib
from typing import cast

import pytest

from vectors import generate
from vectors.core import ACCEPTED, REFUSED, Json, depth
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


def test_the_committed_file_holds_each_text_of_the_two_lists() -> None:
    document = cast("dict[str, Json]", json.loads(generate.committed()[SYNTAX_FILE]))
    vectors = cast("list[dict[str, Json]]", document["vectors"])
    listed = [(text.id, ACCEPTED) for text in tomlfile.TAKEN]
    listed += [(text.id, REFUSED) for text in tomlfile.NOT_TAKEN]

    assert [(vector["id"], vector["result"]) for vector in vectors] == listed


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


def test_no_listed_text_has_a_number_that_the_surface_leaves_out() -> None:
    """`tomllib` reads an integer of each size and a float text outside the
    range of a float. A reader of a file kind refuses both. Such a text in a
    list would be a vector on which the two differ."""
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
