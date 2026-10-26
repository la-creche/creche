"""`attendance` read as `view-ro`, over its two read-only view routes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from noticeboard.jsonfiles import MAX_DOC_BYTES
from noticeboard.sessions import (
    MAX_ANSWER_BYTES,
    MAX_ENTRIES,
    MAX_SESSIONS,
    SESSION_ID_MAX,
    EntryKind,
    SessionReader,
    is_session,
)
from noticeboard_helpers import (
    SESSION,
    FakeAttendance,
    Notices,
    deep_object,
    said,
    tool_entry,
    transcript_doc,
    view_row,
    write_token,
)

CHAT = "chat"
LIST_PATH = f"/families/{CHAT}/sessions"
ONE_PATH = f"{LIST_PATH}/{SESSION}"

#: U+2028. Built with `chr` so the source file holds no invisible character.
LINE_SEP = chr(0x2028)

TRIGGER = {"kind": "dispatch", "name": "scrum-lead", "fired_at": "2026-10-25T12:00:00Z"}


def reader(tmp_path: Path, fake: FakeAttendance) -> SessionReader:
    return SessionReader(transport=fake, token_file=write_token(tmp_path))


def test_the_session_list_reads_the_view_route(tmp_path: Path) -> None:
    fake = FakeAttendance()
    fake.answer(
        LIST_PATH,
        {
            "sessions": [
                view_row(),
                view_row("s-2", title=None, job_id="job-1", trigger=TRIGGER),
                view_row("s-3", delegation="vault-oracle"),
            ],
            "next_cursor": "c-2",
        },
    )

    answer = reader(tmp_path, fake).sessions(CHAT)

    assert fake.calls == [(LIST_PATH, {})]
    assert answer.problem == ""
    assert [one.session for one in answer.rows] == [SESSION, "s-2", "s-3"]
    assert [one.family for one in answer.rows] == [CHAT] * 3
    assert answer.rows[0].title == "Boiler service date"
    assert answer.rows[1].title == ""
    assert [one.origin for one in answer.rows] == ["", "trigger dispatch scrum-lead", "delegation"]
    assert answer.rows[1].job_id == "job-1"
    assert answer.next_cursor == "c-2"


def test_a_cursor_goes_to_the_query(tmp_path: Path) -> None:
    fake = FakeAttendance()
    fake.answer(LIST_PATH, {"sessions": [], "next_cursor": None})

    answer = reader(tmp_path, fake).sessions(CHAT, cursor="c-2")

    assert fake.calls == [(LIST_PATH, {"cursor": "c-2"})]
    assert answer.next_cursor == ""


def test_the_transcript_reads_one_page_of_entries(tmp_path: Path) -> None:
    fake = FakeAttendance()
    entries = [
        said(0, "user", "when is the boiler due?"),
        tool_entry(1),
        said(2, "thought", "the answer is in the result"),
        said(3, "agent", "In March."),
    ]
    fake.answer(ONE_PATH, transcript_doc(entries, next_start=4, truncated=True))

    page = reader(tmp_path, fake).transcript(CHAT, SESSION)

    assert fake.calls == [(ONE_PATH, {})]
    assert page.problem == ""
    assert [one.kind for one in page.entries] == [
        EntryKind.USER,
        EntryKind.TOOL_CALL,
        EntryKind.THOUGHT,
        EntryKind.AGENT,
    ]
    assert page.entries[0].text == "when is the boiler due?"
    assert page.entries[1].title == "kagi__kagi_search_fetch"
    assert page.entries[1].status == "completed"
    assert page.entries[1].text == "The boiler is due in March."
    assert page.next_start == 4
    assert page.truncated


def test_a_later_page_of_the_transcript_names_its_start(tmp_path: Path) -> None:
    fake = FakeAttendance()
    fake.answer(ONE_PATH, transcript_doc([said(4, "agent", "Yes.")]))

    page = reader(tmp_path, fake).transcript(CHAT, SESSION, start=4)

    assert fake.calls == [(ONE_PATH, {"start": "4"})]
    assert page.next_start is None
    assert [one.position for one in page.entries] == [4]


def test_an_entry_of_another_kind_is_a_problem_and_hides_no_other(tmp_path: Path) -> None:
    fake = FakeAttendance()
    entries = [said(0, "user", "hi"), said(1, "plan", "x"), said(2, "agent", "hello")]
    fake.answer(ONE_PATH, transcript_doc(entries))

    page = reader(tmp_path, fake).transcript(CHAT, SESSION)

    assert [one.text for one in page.entries] == ["hi", "hello"]
    assert page.unread == ("an entry of the transcript has the kind 'plan'",)
    assert page.problem == ""


def test_the_list_and_the_transcript_are_capped(tmp_path: Path) -> None:
    fake = FakeAttendance()
    fake.answer(LIST_PATH, {"sessions": [view_row(f"s-{n}") for n in range(MAX_SESSIONS + 5)]})
    fake.answer(ONE_PATH, transcript_doc([said(n, "user", "x") for n in range(MAX_ENTRIES + 5)]))

    listed = reader(tmp_path, fake).sessions(CHAT)
    page = reader(tmp_path, fake).transcript(CHAT, SESSION)

    assert len(listed.rows) == MAX_SESSIONS
    assert len(page.entries) == MAX_ENTRIES


@pytest.mark.parametrize(
    "value",
    ["a", "0", "A.b_c-d", SESSION, "auto-01J9ZQ5V7Y8X4W3T2S1R0QPNMK", "a..b", "a" * SESSION_ID_MAX],
)
def test_a_session_id_is_taken(value: str) -> None:
    assert is_session(value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        ".",
        "..",
        ".hidden",
        "-x",
        "_x",
        "a/b",
        "a\\b",
        "a b",
        "a:b",
        "a%2Fb",
        "a\tb",
        "ä",
        "a" + LINE_SEP + "b",
        "a" * (SESSION_ID_MAX + 1),
        "\na",
        "a\n",
        "a\r\n",
    ],
)
def test_a_text_that_is_no_session_id_is_refused(value: str) -> None:
    assert not is_session(value)


def test_the_token_never_reaches_the_query(tmp_path: Path) -> None:
    """Invariant 13. The bearer is its own argument, so no query can hold it."""
    fake = FakeAttendance()
    fake.answer(LIST_PATH, {"sessions": []})

    reader(tmp_path, fake).sessions(CHAT)

    _, params = fake.calls[0]
    assert "v" * 40 not in json.dumps(params)
    assert fake.bearers == ["v" * 40]


def test_a_missing_token_file_reports_and_makes_no_call(tmp_path: Path) -> None:
    fake = FakeAttendance()
    answer = SessionReader(transport=fake, token_file=tmp_path / "gone.token").sessions(CHAT)

    assert "cannot read the noticeboard-ro token" in answer.problem
    assert fake.calls == []


def test_an_empty_token_file_is_refused(tmp_path: Path) -> None:
    fake = FakeAttendance()
    answer = SessionReader(transport=fake, token_file=write_token(tmp_path, "")).sessions(CHAT)

    assert "empty" in answer.problem
    assert fake.calls == []


def test_a_token_file_that_is_not_text_reports_and_makes_no_call(tmp_path: Path) -> None:
    """The decoder raises UnicodeDecodeError for these bytes, not OSError."""
    fake = FakeAttendance()
    token_file = write_token(tmp_path)
    token_file.write_bytes(b"\xff\xfe" + b"v" * 40)

    answer = SessionReader(transport=fake, token_file=token_file).sessions(CHAT)

    assert "token" in answer.problem
    assert fake.calls == []


def test_a_refusal_becomes_a_sentence_not_an_exception(tmp_path: Path) -> None:
    fake = FakeAttendance()
    fake.answer(
        ONE_PATH,
        {"error": {"code": "sandbox_unavailable", "message": "family chat facade gave no answer"}},
        status=503,
    )

    page = reader(tmp_path, fake).transcript(CHAT, SESSION)

    assert page.entries == ()
    assert "sandbox_unavailable" in page.problem
    assert "503" in page.problem


def test_a_dead_attendance_becomes_a_report(tmp_path: Path) -> None:
    fake = FakeAttendance()
    fake.fault = "cannot reach attendance: ConnectError: no socket"

    answer = reader(tmp_path, fake).sessions(CHAT)

    assert answer.rows == ()
    assert "cannot reach attendance" in answer.problem


def test_a_malformed_body_becomes_a_report(tmp_path: Path) -> None:
    fake = FakeAttendance()
    fake.answer(LIST_PATH, b"{ not json")

    answer = reader(tmp_path, fake).sessions(CHAT)

    assert answer.rows == ()
    assert "not JSON" in answer.problem


#: The problem of a text that is not strict JSON, after the name of the text.
NOT_STRICT = "is not JSON: the text breaks a rule of strict JSON"


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> Notices:
    return Notices().install(monkeypatch)


def test_a_body_that_nests_too_deep_becomes_a_report(tmp_path: Path) -> None:
    fake = FakeAttendance()
    fake.answer(LIST_PATH, deep_object())
    fake.answer(ONE_PATH, deep_object())

    listed = reader(tmp_path, fake).sessions(CHAT)
    page = reader(tmp_path, fake).transcript(CHAT, SESSION)

    assert listed.rows == ()
    assert listed.problem == f"attendance's answer {NOT_STRICT}"
    assert page.entries == ()
    assert page.problem == f"attendance's answer {NOT_STRICT}"


def test_an_answer_that_is_not_strict_records_a_notice_of_the_family(
    tmp_path: Path, recorded: Notices
) -> None:
    fake = FakeAttendance()
    fake.answer(LIST_PATH, b'{"sessions": [], "next_cursor": NaN}')
    fake.answer(ONE_PATH, b'{"session_id": "a", "session_id": "b"}')

    reader(tmp_path, fake).sessions(CHAT)
    reader(tmp_path, fake).transcript(CHAT, SESSION)

    assert recorded.calls == [
        ("session.answer", "constant", CHAT, True),
        ("session.answer", "duplicate_key", CHAT, True),
    ]


def test_a_refusal_that_is_not_strict_records_a_notice(tmp_path: Path, recorded: Notices) -> None:
    fake = FakeAttendance()
    fake.answer(ONE_PATH, b'{"error": {"code": "forbidden", "retry": NaN}}', status=403)

    page = reader(tmp_path, fake).transcript(CHAT, SESSION)

    assert page.problem == "attendance answered 403 with no error code"
    assert recorded.calls == [("session.answer", "constant", CHAT, True)]


def test_a_refusal_past_the_cap_records_a_notice_of_its_size(
    tmp_path: Path, recorded: Notices
) -> None:
    fake = FakeAttendance()
    body = b'{"error": {"code": "forbidden"}}'.ljust(MAX_ANSWER_BYTES + 1)
    fake.answer(LIST_PATH, body, status=403)

    listed = reader(tmp_path, fake).sessions(CHAT)

    assert listed.problem == "attendance answered 403 with no error code"
    assert recorded.calls == [("session.answer", "too_large", CHAT, True)]


def test_an_answer_past_the_cap_of_a_document_still_reads(tmp_path: Path) -> None:
    """An answer has the cap of the session reader, and not the cap of a file."""
    fake = FakeAttendance()
    padding = "x" * (MAX_DOC_BYTES + 1)
    fake.answer(LIST_PATH, {"sessions": [], "next_cursor": padding})

    listed = reader(tmp_path, fake).sessions(CHAT)

    assert MAX_DOC_BYTES < len(padding) < MAX_ANSWER_BYTES
    assert listed.problem == ""
    assert listed.next_cursor == padding[:500]
