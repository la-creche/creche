"""The field helpers every reader goes through (`jsonfiles.py`)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from noticeboard_helpers import peak_memory_of

from noticeboard import jsonfiles

#: An integer that JSON can write and that no float can hold.
PAST_EVERY_FLOAT = 10**400

#: A file far past the cap of the reader, and the most memory its read may take.
FAR_PAST_THE_CAP = 64 * jsonfiles.MAX_DOC_BYTES
READ_MEMORY_MAX = 4 * jsonfiles.MAX_DOC_BYTES

#: Noon in UTC on one day, and each text of the one grammar that names it.
NOON = datetime(2026, 9, 19, 12, 0, 0, tzinfo=UTC)
NOON_TEXTS = (
    "2026-09-19T12:00:00Z",
    "2026-09-19T12:00:00z",
    "2026-09-19t12:00:00Z",
    "2026-09-19T12:00:00+00:00",
    "2026-09-19T12:00:00-00:00",
    "2026-09-19T14:00:00+02:00",
    "2026-09-19T06:30:00-05:30",
    "2026-09-19T12:00:00.000Z",
)

#: A text that is outside the one grammar. Each one names a time in another
#: form.
NOT_IN_THE_GRAMMAR = (
    "2026-09-19T12:00:00",
    "2026-09-19T12:00:00.5",
    "2026-09-19",
    "2026-09-19 12:00:00Z",
    "2026-09-19T12:00Z",
    "2026-09-19T12:00:00+0000",
    "2026-09-19T12:00:00+00",
    "20260919T120000Z",
    "2026-W38-6T12:00:00Z",
    "2026-09-19T12:00:00Z\n",
    " 2026-09-19T12:00:00Z",
    "2026-09-31T12:00:00Z",
    "2026-09-19T12:00:60Z",
    "0000-09-19T12:00:00Z",
    "9999-12-31T23:59:59-00:01",
    "noon",
)


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


def test_a_name_that_is_no_path_reports_and_does_not_raise(tmp_path: Path) -> None:
    """`open` raises ValueError for a name with a NUL, not OSError."""
    body, problem = jsonfiles.read_object(tmp_path / "validation\x00.json")

    assert body is None
    assert problem == "cannot read 'validation\\x00.json': the system takes no such path"


def test_a_file_past_the_cap_is_refused_and_not_read_whole(tmp_path: Path) -> None:
    """The size of a file must not set the memory of a page."""
    path = tmp_path / "status.json"
    with path.open("wb") as handle:
        handle.truncate(FAR_PAST_THE_CAP)

    (body, problem), peak = peak_memory_of(lambda: jsonfiles.read_object(path))

    assert body is None
    assert problem == f"status.json is over {jsonfiles.MAX_DOC_BYTES} bytes; refusing to parse it"
    assert peak < READ_MEMORY_MAX


def test_a_file_of_exactly_the_cap_is_parsed(tmp_path: Path) -> None:
    path = tmp_path / "status.json"
    text = b'{"kind": "attended"}'
    path.write_bytes(text + b" " * (jsonfiles.MAX_DOC_BYTES - len(text)))

    body, problem = jsonfiles.read_object(path)

    assert problem is None
    assert body == {"kind": "attended"}


@pytest.mark.parametrize("text", NOON_TEXTS)
def test_a_time_reads_as_its_instant_in_utc(text: str) -> None:
    stamp = jsonfiles.moment({"written_at": text}, "written_at")

    assert stamp == NOON
    assert stamp is not None
    assert stamp.tzinfo is UTC


def test_a_time_keeps_six_digits_of_its_fraction() -> None:
    stamp = jsonfiles.moment({"as_of": "2026-09-19T12:00:00.123456789Z"}, "as_of")

    assert stamp == NOON + timedelta(microseconds=123_456)


@pytest.mark.parametrize("text", NOT_IN_THE_GRAMMAR)
def test_a_text_outside_the_one_grammar_reads_none(text: str) -> None:
    assert jsonfiles.moment({"written_at": text}, "written_at") is None


def test_a_time_with_no_offset_has_no_age() -> None:
    stamp = jsonfiles.moment({"written_at": "2026-09-19T11:59:55"}, "written_at")

    assert stamp is None
    assert jsonfiles.age_s(stamp, NOON) is None


def test_a_field_that_is_no_text_reads_none_as_a_time() -> None:
    assert jsonfiles.moment({}, "written_at") is None
    assert jsonfiles.moment({"written_at": ""}, "written_at") is None
    assert jsonfiles.moment({"written_at": None}, "written_at") is None
    assert jsonfiles.moment({"written_at": 1_789_819_200}, "written_at") is None
    assert jsonfiles.moment({"written_at": ["2026-09-19T12:00:00Z"]}, "written_at") is None
