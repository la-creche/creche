"""The field helpers every reader goes through (`jsonfiles.py`)."""

from __future__ import annotations

from noticeboard import jsonfiles

#: An integer that JSON can write and that no float can hold.
PAST_EVERY_FLOAT = 10**400


def test_a_number_reads_as_a_float() -> None:
    assert jsonfiles.number({"spend_usd": 3}, "spend_usd") == 3.0
    assert jsonfiles.number({"spend_usd": 3.42}, "spend_usd") == 3.42


def test_a_field_that_is_not_a_number_reads_none() -> None:
    assert jsonfiles.number({"spend_usd": "3.42"}, "spend_usd") is None
    assert jsonfiles.number({"spend_usd": True}, "spend_usd") is None
    assert jsonfiles.number({}, "spend_usd") is None


def test_an_integer_past_every_float_reads_none() -> None:
    """`float` raises OverflowError on this integer."""
    assert jsonfiles.number({"spend_usd": PAST_EVERY_FLOAT}, "spend_usd") is None
    assert jsonfiles.number({"spend_usd": -PAST_EVERY_FLOAT}, "spend_usd") is None
