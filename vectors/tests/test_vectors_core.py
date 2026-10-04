"""The normalizer: one JSON form per Python value, and a marker for the rest.

A reader in another language decodes each marker object by its one key. A
test here holds each marker to the form that `vectors/README.md` states.
"""

from __future__ import annotations

import enum
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import BaseModel

from vectors.core import (
    BASE64_MARKER,
    DEPTH_MAX,
    ENTRIES_MARKER,
    FLOAT_MARKER,
    INT_MARKER,
    JSON_MARKER,
    MARKERS,
    UTF16_MARKER,
    Json,
    Surface,
    accepted,
    bounded,
    depth,
    normalize,
    refused,
    render,
    text_input,
)

LONE_SURROGATE = "a\ud800b"


class Color(enum.Enum):
    RED = "red"


@dataclass(frozen=True)
class Point:
    x: int
    color: Color


class Limits(BaseModel):
    rpm: int = 60


def _nested(levels: int) -> Json:
    """`levels` arrays, one inside the other."""
    value: Json = []
    for _ in range(levels - 1):
        value = [value]

    return value


def _keys(value: Json) -> list[str]:
    assert isinstance(value, dict)

    return list(value)


def test_a_plain_value_keeps_its_json_form() -> None:
    assert normalize(None) is None
    assert normalize(True) is True
    assert normalize(Color.RED) == "red"
    assert normalize(Point(1, Color.RED)) == {"x": 1, "color": "red"}
    assert normalize(Limits()) == {"rpm": 60}
    assert normalize((1, "a")) == [1, "a"]
    assert normalize({"b", "a"}) == ["a", "b"]
    assert normalize(frozenset({2, 1})) == [1, 2]


def test_a_time_keeps_the_offset_it_has() -> None:
    plus_two = timezone(timedelta(hours=2))

    assert normalize(datetime(2999, 1, 1, tzinfo=UTC)) == "2999-01-01T00:00:00+00:00"
    assert normalize(datetime(2999, 1, 1, 2, tzinfo=plus_two)) == "2999-01-01T02:00:00+02:00"
    assert normalize(datetime(2999, 1, 1)) == "2999-01-01T00:00:00"


def test_an_integer_past_64_bits_is_a_marker() -> None:
    assert normalize(2**64 - 1) == 2**64 - 1
    assert normalize(-(2**63)) == -(2**63)
    assert normalize(2**64) == {INT_MARKER: "18446744073709551616"}
    assert normalize(-(2**63) - 1) == {INT_MARKER: "-9223372036854775809"}


def test_a_float_that_is_not_finite_is_a_marker() -> None:
    assert normalize(1.5) == 1.5
    assert normalize(math.nan) == {FLOAT_MARKER: "NaN"}
    assert normalize(math.inf) == {FLOAT_MARKER: "Infinity"}
    assert normalize(-math.inf) == {FLOAT_MARKER: "-Infinity"}


def test_a_lone_surrogate_is_a_marker() -> None:
    assert normalize(LONE_SURROGATE) == {UTF16_MARKER: [0x61, 0xD800, 0x62]}
    assert normalize("\U0001f600") == "\U0001f600"


def test_bytes_are_a_marker() -> None:
    assert normalize(b"\xff\x00") == {BASE64_MARKER: "/wA="}
    assert normalize(bytearray(b"hi")) == {BASE64_MARKER: "aGk="}


def test_a_key_that_is_no_plain_string_is_a_marker() -> None:
    assert normalize({1: "a", "b": 2}) == {ENTRIES_MARKER: [[1, "a"], ["b", 2]]}
    assert normalize({LONE_SURROGATE: 1}) == {
        ENTRIES_MARKER: [[{UTF16_MARKER: [0x61, 0xD800, 0x62]}, 1]]
    }


@pytest.mark.parametrize("marker", sorted(MARKERS))
def test_a_value_that_looks_like_a_marker_is_refused(marker: str) -> None:
    with pytest.raises(ValueError, match="collides"):
        normalize({marker: "5"})


def test_a_value_with_no_json_form_is_refused() -> None:
    with pytest.raises(TypeError):
        normalize(object())


def test_depth_counts_arrays_and_objects() -> None:
    assert depth(1) == 0
    assert depth("text") == 0
    assert depth([]) == 1
    assert depth({"a": [1], "b": {"c": {"d": 1}}}) == 3
    assert depth(_nested(DEPTH_MAX)) == DEPTH_MAX


def test_a_field_past_the_depth_budget_is_a_marker() -> None:
    at_budget = _nested(DEPTH_MAX)
    past_budget = _nested(DEPTH_MAX + 1)

    assert bounded(at_budget) == at_budget

    marker = bounded(past_budget)

    assert isinstance(marker, dict)
    assert list(marker) == [JSON_MARKER]
    assert json.loads(str(marker[JSON_MARKER])) == past_budget


def test_every_field_of_a_vector_is_bounded() -> None:
    deep = _nested(DEPTH_MAX + 1)
    given = text_input("x")

    took = accepted("a", given, deep, params=deep).body
    refusal = refused("b", given, deep).body

    assert _keys(took["value"]) == [JSON_MARKER]
    assert _keys(took["params"]) == [JSON_MARKER]
    assert _keys(refusal["refusal"]) == [JSON_MARKER]


def test_an_input_past_the_depth_budget_stops_the_render() -> None:
    vector = accepted("a", {"args": _nested(DEPTH_MAX + 1)})
    surface = Surface("s", "s.json", "entry", "contract", (vector,))

    with pytest.raises(ValueError, match="nests too deep"):
        render(surface)


def test_an_input_text_with_a_lone_surrogate_is_refused() -> None:
    with pytest.raises(ValueError, match="lone surrogate"):
        text_input(LONE_SURROGATE)
