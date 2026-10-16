"""The field helpers every reader goes through (`jsonfiles.py`)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from noticeboard.jsonfiles import Surface
from noticeboard_helpers import Notices, peak_memory_of

from noticeboard import jsonfiles, notices

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


#: The sentence of each text that is not strict JSON, after the name of the text.
NOT_STRICT = "is not JSON: the text breaks a rule of strict JSON"

#: One text for each rule that a reader of `json.loads` does not hold, with
#: the word of the rule.
NOT_STRICT_TEXTS: tuple[tuple[bytes, str], ...] = (
    (b'{"spend": NaN}', "constant"),
    (b'{"spend": Infinity}', "constant"),
    (b'\xef\xbb\xbf{"kind": "attended"}', "byte_order_mark"),
    (b"\xff\xfe" + '{"kind": "attended"}'.encode("utf-16-le"), "not_utf8"),
    ('{"kind": "attended"}'.encode("utf-16-be"), "syntax"),
    ('{"kind": "attended"}'.encode("utf-32-le"), "syntax"),
    (rb'{"kind": "\ud800"}', "lone_surrogate"),
    (b'{"kind": "attended", "kind": "autonomous"}', "duplicate_key"),
    (b'{"epoch": 123456789012345678901234567890}', "integer_range"),
    (b'{"spend": 1e999}', "float_range"),
    (b'{"x":' + b"[" * 65 + b"]" * 65 + b"}", "too_deep"),
    (b'{"kind": "attended",}', "syntax"),
    (b'{"kind": "attended"} x', "trailing_data"),
    (b'{"a": "\xff"}', "not_utf8"),
)


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> Notices:
    return Notices().install(monkeypatch)


@pytest.mark.parametrize(("data", "rule"), NOT_STRICT_TEXTS)
def test_a_text_that_is_not_strict_json_reads_as_a_problem(
    data: bytes, rule: str, recorded: Notices
) -> None:
    body, problem = jsonfiles.parse_object(
        data, "status.json", surface=Surface.STATUS_DOCUMENT, family="chat"
    )

    assert body is None
    assert problem == f"status.json {NOT_STRICT}"
    assert recorded.calls == [("status.document", rule, "chat", True)]


def test_a_strict_text_records_nothing(recorded: Notices) -> None:
    body, problem = jsonfiles.parse_object(
        b'{"kind": "attended"}', "status.json", surface=Surface.STATUS_DOCUMENT
    )

    assert (body, problem) == ({"kind": "attended"}, None)
    assert recorded.calls == []


def test_a_strict_value_that_is_no_object_records_nothing(recorded: Notices) -> None:
    body, problem = jsonfiles.parse_object(
        b"[1, 2]", "status.json", surface=Surface.STATUS_DOCUMENT
    )

    assert body is None
    assert problem == "status.json is list, not a JSON object"
    assert recorded.calls == []


def test_the_cap_of_the_reader_is_a_parameter(recorded: Notices) -> None:
    data = b'{"x": "' + b"a" * 100 + b'"}'

    assert jsonfiles.parse_object(data, "x", surface=Surface.AUDIT_LINE)[0] is not None
    body, problem = jsonfiles.parse_object(data, "x", 100, surface=Surface.AUDIT_LINE)

    assert body is None
    assert problem == f"x {NOT_STRICT}"
    assert recorded.calls == [("audit.line", "too_large", None, True)]


def test_the_default_cap_is_the_cap_of_a_document(recorded: Notices) -> None:
    text = b'{"x": ""}'
    at_cap = text[:-2] + b"a" * (jsonfiles.MAX_DOC_BYTES - len(text)) + text[-2:]

    assert len(at_cap) == jsonfiles.MAX_DOC_BYTES
    assert jsonfiles.parse_object(at_cap, "x", surface=Surface.STATUS_DOCUMENT)[1] is None
    assert jsonfiles.parse_object(at_cap + b" ", "x", surface=Surface.STATUS_DOCUMENT)[0] is None
    assert recorded.calls == [("status.document", "too_large", None, True)]


def test_a_refused_text_gives_one_log_line_and_no_byte_of_it_in_the_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "hunter2-hunter2"
    path = tmp_path / "notices" / "noticeboard.json"
    monkeypatch.setattr(notices, "_settings", None)
    notices.configure("noticeboard", path, clock=lambda: 1_760_000_000.0)

    with caplog.at_level(logging.INFO):
        body, problem = jsonfiles.parse_object(
            f'{{"token": "{secret}", "spend": NaN}}'.encode(),
            "status.json",
            surface=Surface.STATUS_DOCUMENT,
            family="chat",
        )

    assert body is None
    assert problem is not None
    assert secret not in problem
    assert caplog.messages == ["json_not_strict noticeboard status.document constant"]
    assert secret.encode() not in path.read_bytes()
    assert b'"push": "status.document:constant:1760000000"' in path.read_bytes()


def test_read_object_gives_the_surface_and_the_family_to_the_notice(
    tmp_path: Path, recorded: Notices
) -> None:
    path = tmp_path / "status.json"
    path.write_bytes(b'{"kind": NaN}')

    body, problem = jsonfiles.read_object(path, surface=Surface.STATUS_OUTCOME, family="code")

    assert body is None
    assert problem == f"status.json {NOT_STRICT}"
    assert recorded.calls == [("status.outcome", "constant", "code", True)]


def test_a_file_past_the_cap_records_no_notice(tmp_path: Path, recorded: Notices) -> None:
    path = tmp_path / "status.json"
    path.write_bytes(b" " * (jsonfiles.MAX_DOC_BYTES + 1))

    body, problem = jsonfiles.read_object(path, surface=Surface.STATUS_DOCUMENT)

    assert body is None
    assert problem == f"status.json is over {jsonfiles.MAX_DOC_BYTES} bytes; refusing to parse it"
    assert recorded.calls == []


def test_the_surface_words_are_the_words_of_the_notices_of_the_noticeboard() -> None:
    assert [one.value for one in Surface] == [
        "status.document",
        "status.report",
        "status.outcome",
        "audit.line",
        "session.answer",
        "session.journal_line",
    ]


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
    body, problem = jsonfiles.read_object(
        tmp_path / "validation\x00.json", surface=Surface.STATUS_REPORT
    )

    assert body is None
    assert problem == "cannot read 'validation\\x00.json': the system takes no such path"


def test_a_file_past_the_cap_is_refused_and_not_read_whole(tmp_path: Path) -> None:
    """The size of a file must not set the memory of a page."""
    path = tmp_path / "status.json"
    with path.open("wb") as handle:
        handle.truncate(FAR_PAST_THE_CAP)

    (body, problem), peak = peak_memory_of(
        lambda: jsonfiles.read_object(path, surface=Surface.STATUS_DOCUMENT)
    )

    assert body is None
    assert problem == f"status.json is over {jsonfiles.MAX_DOC_BYTES} bytes; refusing to parse it"
    assert peak < READ_MEMORY_MAX


def test_a_file_of_exactly_the_cap_is_parsed(tmp_path: Path) -> None:
    path = tmp_path / "status.json"
    text = b'{"kind": "attended"}'
    path.write_bytes(text + b" " * (jsonfiles.MAX_DOC_BYTES - len(text)))

    body, problem = jsonfiles.read_object(path, surface=Surface.STATUS_DOCUMENT)

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
