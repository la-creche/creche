"""The one reader of a time text (`rfc3339.py`).

The source of the grammar is the Rust type `creche_contracts::time::Timestamp`.
`ACCEPTED` and `REFUSED` hold each text of the two test tables of that type.
An accepted text has the count that the Rust table gives it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from noticeboard import rfc3339

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
MICROSECOND = timedelta(microseconds=1)

#: Each text that the reader takes, with its count of microseconds from
#: 1970-01-01T00:00:00Z.
ACCEPTED: tuple[tuple[str, int], ...] = (
    # --- one text for each form of the grammar ---
    ("2026-10-06T06:00:00Z", 1_791_266_400_000_000),
    ("2026-10-06T06:00:00+00:00", 1_791_266_400_000_000),
    ("2026-10-06T06:00:00-00:00", 1_791_266_400_000_000),
    ("2026-10-06T06:00:00+05:30", 1_791_246_600_000_000),
    ("2026-10-06T06:00:00-05:30", 1_791_286_200_000_000),
    ("2026-10-06T06:00:00+23:59", 1_791_180_060_000_000),
    ("2026-10-06T06:00:00-23:59", 1_791_352_740_000_000),
    ("2026-12-31T23:30:00-01:00", 1_798_763_400_000_000),
    ("2024-02-29T23:30:00-01:00", 1_709_253_000_000_000),
    ("2026-10-06t06:00:00Z", 1_791_266_400_000_000),
    ("2026-10-06T06:00:00.1Z", 1_791_266_400_100_000),
    ("2026-10-06T06:00:00.123Z", 1_791_266_400_123_000),
    ("2026-10-06T06:00:00.123456Z", 1_791_266_400_123_456),
    ("2026-10-06T06:00:00.1234567Z", 1_791_266_400_123_456),
    ("2026-10-06T06:00:00.1234+05:30", 1_791_246_600_123_400),
    ("2026-10-06T06:00:00z", 1_791_266_400_000_000),
    ("2024-02-29T00:00:00Z", 1_709_164_800_000_000),
    ("0001-01-01T00:00:00Z", -62_135_596_800_000_000),
    ("9999-12-31T23:59:59.999999Z", 253_402_300_799_999_999),
    ("1970-01-01T00:00:00Z", 0),
    ("1969-12-31T23:59:59.999999Z", -1),
    ("0001-01-01T00:01:00+00:01", -62_135_596_800_000_000),
    # --- midnight UTC in three forms ---
    ("2999-01-01T00:00:00Z", 32_472_144_000_000_000),
    ("2999-01-01T00:00:00z", 32_472_144_000_000_000),
    ("2999-01-01T02:00:00+02:00", 32_472_144_000_000_000),
    ("2026-10-06t06:00:00z", 1_791_266_400_000_000),
    # --- a fraction of more than six digits: the reader drops the others ---
    ("2026-10-06T06:00:00.123456789Z", 1_791_266_400_123_456),
    ("9999-12-31T23:59:59.999999999Z", 253_402_300_799_999_999),
    ("1969-12-31T23:59:59.9999999Z", -1),
    ("2026-10-06T06:00:00.5+05:30", 1_791_246_600_500_000),
    ("2026-10-06T06:00:00.5-05:30", 1_791_286_200_500_000),
    # --- the first and the last instant, each through the largest offset ---
    ("0001-01-01T00:00:00.000000+00:00", -62_135_596_800_000_000),
    ("0001-01-01T23:59:00+23:59", -62_135_596_800_000_000),
    ("9999-12-31T00:00:59.999999-23:59", 253_402_300_799_999_999),
    # --- the two sides of 1970 ---
    ("1969-12-31T23:59:59Z", -1_000_000),
    ("1970-01-01T00:00:00.000001Z", 1),
    ("1970-01-01T01:00:00+01:00", 0),
    # --- a leap day of a year that 400 divides, and the last second of a day ---
    ("2000-02-29T00:00:00Z", 951_782_400_000_000),
    ("2400-02-29T23:59:59Z", 13_574_649_599_000_000),
    ("2026-10-06T23:59:59Z", 1_791_331_199_000_000),
)

#: Each text that the reader refuses.
REFUSED: tuple[str, ...] = (
    # --- an offset in another form, or no offset ---
    "2026-10-06T06:00:00+0530",
    "2026-10-06T06:00:00+05",
    "2026-10-06T06:00:00+05:30:15",
    "2026-10-06T06:00:00+05:30:15.5",
    "2026-10-06T06:00:00+24:00",
    "2026-10-06T06:00:00+00:60",
    "2026-10-06T06:00:00+5",
    "2026-10-06T06:00:00+",
    "2026-10-06T06:00:00+00:00Z",
    "2026-10-06T06:00:00",
    # --- a part of a time, or another form of a date or of a time ---
    "2026-10-06",
    "2026-10-06Z",
    "2026-10-06+06:00",
    "2026-10-06T06",
    "2026-10-06T06:00",
    "2026-10-06T06:00Z",
    "2026-10-06 06:00:00Z",
    "2026-10-06\u00e906:00:00Z",
    "2026-10-06\U0001f60006:00:00Z",
    "2026-10-06TT06:00:00Z",
    "2026-10-0606:00:00Z",
    "20261006",
    "20261006T060000Z",
    "2026-10-06T060000Z",
    "20261006T06:00:00Z",
    "2026-10-06T0600+05",
    "2026-10-06T06:0000Z",
    "2026-W38-6",
    "2026-W38-6T06:00:00Z",
    "2026W386",
    # --- a fraction in another form ---
    "2026-10-06T06:00:00.12345678901234567890Z",
    "2026-10-06T06:00:00,5Z",
    "2026-10-06T06:00:00.5.5Z",
    "2026-10-06T06:00:00.\u0661Z",
    # --- a character that the grammar does not have ---
    "2026-10-06T06:00:00 Z",
    "2026-10-06T06:00:009Z",
    "2026-10-06T06:00:+05:30",
    "2026-10-06T06:00:00.123456abc+05:30",
    "2026-10-06T06000530",
    "2026-10-06T06:00:00ZZ",
    "2026-10-06T06:00:00UTC",
    " 2026-10-06T06:00:00Z",
    "2026-10-06T06:00:00Z ",
    "2026-10-06T06:00:00Z\n",
    "2026-9-19T06:00:00Z",
    "2026-10-06T6:00:00Z",
    "26-09-19T06:00:00Z",
    "\u0662\u0660\u0662\u0666-09-19T06:00:00Z",
    # --- a field outside its bounds ---
    "2026-02-29T00:00:00Z",
    "2026-13-01T00:00:00Z",
    "2026-09-31T00:00:00Z",
    "2026-09-00T00:00:00Z",
    "2026-10-06T25:00:00Z",
    "2026-10-06T23:60:00Z",
    "2026-10-06T23:59:60Z",
    "0000-01-01T00:00:00Z",
    "10000-01-01T00:00:00Z",
    # --- a text that is no time at all ---
    "Z",
    "last Tuesday",
    "1789797600",
    "2026-10-06 06:00:00 +0200",
    "2026-10-06T06:00:00  Z",
    "2026-10-06T06:00:00 ",
    "2026-10-06T06:00:00.12 Z",
    "2026-10-06T06:00:00Z\x00abc",
    "2026-10-06T0600003",
    "2026-10-06T060005301234.5+00:00",
    "2026-W38",
    "2026W38",
    "2026-W38-06:00",
    "2026W386060000",
    "2026-W53-1",
    "2025-W53-1",
    "2026-W38-8",
    "9999-W52-6",
    # --- an offset that moves the instant out of the years 0001 to 9999 ---
    "0001-01-01T00:00:00+00:01",
    "9999-12-31T23:59:59-00:01",
    # --- a space in place of the `T`, a text with no offset and hour 24 ---
    "2999-01-01 00:00:00Z",
    "2999-01-01T00:00:00",
    "2026-10-06T06:00:00.5",
    "2999-01-01T24:00:00Z",
    "2026-10-06T24:00:00Z",
    "2026-10-06T06:00:61Z",
    # --- a fraction of no digit and of 10 digits, and a number in it ---
    "2026-10-06T06:00:00.Z",
    "2026-10-06T06:00:00.1234567890Z",
    "2026-10-06T06:00:00.-5Z",
    "2026-10-06T06:00:00.+5Z",
    "2026-10-06T06:00:00.5e1Z",
    # --- the year 0000, also when the offset moves the instant to 0001 ---
    "0000-12-31T23:30:00-01:00",
    "0000-02-29T00:00:00Z",
    "0000-12-31T23:59:59.999999Z",
    # --- one microsecond before the first instant, and one second after the
    # last second, each through the largest offset ---
    "0001-01-01T23:58:59.999999+23:59",
    "9999-12-31T00:01:00-23:59",
    # --- an offset outside the form or outside its bounds ---
    "2026-10-06T06:00:00+05:3",
    "2026-10-06T06:00:00+05:300",
    "2026-10-06T06:00:00+5:30",
    "2026-10-06T06:00:00+-5:30",
    "2026-10-06T06:00:00+05:-3",
    "2026-10-06T06:00:00+23:60",
    "2026-10-06T06:00:00-24:00",
    # --- a sign, a digit and a letter that are not ASCII ---
    "2026-10-06T06:00:00\u221205:30",
    "2026-10-06T06:00:00+\u0660\u0665:30",
    "2026-10-06T06:00:0\u0660Z",
    "2026-10-06T06:00:00\uff3a",
    "2026\u201010-06T06:00:00Z",
    # --- white space at an end, a sign at the start and no text ---
    "2026-10-06T06:00:00Z\r\n",
    "\n2026-10-06T06:00:00Z",
    "2026-10-06T06:00:00Z\t",
    "+2026-10-06T06:00:00Z",
    "-2026-10-06T06:00:00Z",
    "2026-10-06T-6:00:00Z",
    "",
    # --- a date that the calendar does not have ---
    "1900-02-29T00:00:00Z",
    "2100-02-29T00:00:00Z",
    "2026-00-06T06:00:00Z",
    "2026-10-32T06:00:00Z",
    "2026-04-31T06:00:00Z",
    # --- a text that breaks more than one rule ---
    "0000-13-01T25:00:00+2400",
    "0000-13-01T25:00:00",
    "2026-13-01T25:00:00+24:00",
    "2026-10-06T25:00:00+24:00",
    "0000-01-01T00:00:00+24:00",
    "0000-13-01T00:00:00Z",
)


def micros_of(moment: datetime) -> int:
    """The microseconds from 1970-01-01T00:00:00Z to `moment`, as an integer."""
    return (moment - EPOCH) // MICROSECOND


@pytest.mark.parametrize(("text", "micros"), ACCEPTED)
def test_a_text_in_the_grammar_is_its_instant_in_utc(text: str, micros: int) -> None:
    moment = rfc3339.parse(text)

    assert moment is not None
    assert moment.tzinfo is UTC
    assert micros_of(moment) == micros


@pytest.mark.parametrize("text", REFUSED)
def test_a_text_outside_the_grammar_is_no_time(text: str) -> None:
    assert rfc3339.parse(text) is None


def test_no_text_is_in_a_table_two_times() -> None:
    texts = [*(text for text, _ in ACCEPTED), *REFUSED]

    assert len(set(texts)) == len(texts)


def test_hour_24_is_no_time() -> None:
    """`datetime.fromisoformat` reads hour 24 under one supported Python version only."""
    assert rfc3339.parse("2999-01-01T24:00:00Z") is None
    assert rfc3339.parse("2999-01-01T24:00:00+00:00") is None
    assert rfc3339.parse("2999-12-31T24:00:00-05:00") is None


def test_the_result_is_the_instant_and_not_the_time_of_the_text() -> None:
    moment = rfc3339.parse("2031-04-18T08:43:10.25+02:00")

    assert moment == datetime(2031, 4, 18, 6, 43, 10, 250_000, tzinfo=UTC)
    assert moment is not None
    assert moment.utcoffset() == timedelta(0)
    assert (moment.hour, moment.minute) == (6, 43)


def test_a_fraction_past_six_digits_is_cut_and_not_rounded() -> None:
    cut = rfc3339.parse("2031-04-18T06:43:10.999999999Z")

    assert cut == datetime(2031, 4, 18, 6, 43, 10, 999_999, tzinfo=UTC)
    assert rfc3339.parse("2031-04-18T06:43:10.0000009Z") == datetime(
        2031, 4, 18, 6, 43, 10, tzinfo=UTC
    )


def test_two_texts_of_one_instant_are_equal() -> None:
    midnight = rfc3339.parse("2999-01-01T00:00:00Z")

    assert midnight is not None
    assert rfc3339.parse("2999-01-01T02:00:00+02:00") == midnight
    assert rfc3339.parse("2998-12-31t19:00:00.000-05:00") == midnight
    assert rfc3339.parse("2999-01-01T00:00:00-00:00") == midnight


def test_a_digit_of_another_script_is_no_digit() -> None:
    """`int` and `\\d` read such a digit. The grammar has the ten ASCII digits."""
    for place in (0, 5, 8, 11, 14, 17, 20, 22, 25):
        text = "2026-10-06T06:00:00.5+05:30"
        other = f"{text[:place]}\u0665{text[place + 1 :]}"

        assert rfc3339.parse(other) is None, other
