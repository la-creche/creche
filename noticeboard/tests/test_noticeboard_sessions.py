"""`attendance` read as `view-ro` (contract 02)."""

from __future__ import annotations

import json
from pathlib import Path

from noticeboard.sessions import (
    MAX_STREAM_LINES,
    SessionReader,
)
from noticeboard_helpers import (
    FakeAttendance,
    journal_line,
    ndjson,
    session_doc,
    turn_doc,
    write_token,
)

LIST_PATH = "/v1/sessions"
CHAT = "chat"
OWUI = "owui-3f2a9c41-77b0-4a1e-9a4c-1d0e5f8b2c33"
DETAIL_PATH = f"/v1/sessions/{CHAT}/{OWUI}"
EVENTS_PATH = f"{DETAIL_PATH}/events"

#: U+2028. Built with `chr` so the source file holds no invisible character.
LINE_SEP = chr(0x2028)


def reader(tmp_path: Path, fake: FakeAttendance) -> SessionReader:
    return SessionReader(transport=fake, token_file=write_token(tmp_path))


def test_the_session_list_is_read(tmp_path: Path) -> None:
    fake = FakeAttendance()
    fake.answer(LIST_PATH, {"sessions": [session_doc()], "next_cursor": None})

    answer = reader(tmp_path, fake).sessions(family=CHAT)

    assert answer.problem == ""
    assert [one.title for one in answer.rows] == ["Boiler service date"]
    assert answer.next_cursor == ""


def test_the_token_never_reaches_the_query(tmp_path: Path) -> None:
    """Invariant 13. The bearer is its own argument, so no query can hold it."""
    fake = FakeAttendance()
    fake.answer(LIST_PATH, {"sessions": []})

    reader(tmp_path, fake).sessions(family=CHAT)

    _, params = fake.calls[0]
    assert "v" * 40 not in json.dumps(params)
    assert fake.bearers == ["v" * 40]


def test_a_missing_token_file_reports_and_makes_no_call(tmp_path: Path) -> None:
    fake = FakeAttendance()
    answer = SessionReader(transport=fake, token_file=tmp_path / "gone.token").sessions()

    assert "cannot read the noticeboard-ro token" in answer.problem
    assert fake.calls == []


def test_an_empty_token_file_is_refused(tmp_path: Path) -> None:
    fake = FakeAttendance()
    answer = SessionReader(transport=fake, token_file=write_token(tmp_path, "")).sessions()

    assert "empty" in answer.problem
    assert fake.calls == []


def test_the_door_comes_from_the_session_id_prefix(tmp_path: Path) -> None:
    fake = FakeAttendance()
    fake.answer(
        LIST_PATH,
        {
            "sessions": [
                session_doc(session=OWUI),
                session_doc(session="tui-01JBQ7ZZ9D6M0Q4RXT2J8HYVBK"),
                session_doc(session="job-01JBQ80M4F7S2YQ1VZK6W3TDEN"),
                session_doc(session="auto-01JBQ81P5G8T3ZR2WAL7X4UEFP"),
            ]
        },
    )

    answer = reader(tmp_path, fake).sessions()

    assert [one.door for one in answer.rows] == ["owui", "tui", "delegate", "trigger"]


def test_an_unknown_prefix_falls_back_to_the_lease_holder(tmp_path: Path) -> None:
    lease = {
        "holder": "tui",
        "door_instance": "tui-a",
        "since": "2026-09-19T11:59:00Z",
        "expires_at": "2026-09-19T12:00:00Z",
        "turn": None,
    }
    fake = FakeAttendance()
    fake.answer(LIST_PATH, {"sessions": [session_doc(session="legacy-1", writer=lease)]})

    answer = reader(tmp_path, fake).sessions()

    assert answer.rows[0].door == "tui"


def test_a_refusal_becomes_a_sentence_not_an_exception(tmp_path: Path) -> None:
    fake = FakeAttendance()
    fake.answer(
        LIST_PATH,
        {"error": {"code": "forbidden", "message": "this token may not read chat"}},
        status=403,
    )

    answer = reader(tmp_path, fake).sessions()

    assert answer.rows == ()
    assert "forbidden" in answer.problem
    assert "403" in answer.problem


def test_a_dead_attendance_becomes_a_report(tmp_path: Path) -> None:
    fake = FakeAttendance()
    fake.fault = "cannot reach attendance: ConnectError: no socket"

    answer = reader(tmp_path, fake).sessions()

    assert answer.rows == ()
    assert "cannot reach attendance" in answer.problem


def test_a_malformed_body_becomes_a_report(tmp_path: Path) -> None:
    fake = FakeAttendance()
    fake.answer(LIST_PATH, b"{ not json")

    answer = reader(tmp_path, fake).sessions()

    assert answer.rows == ()
    assert "not JSON" in answer.problem


def test_the_turns_carry_their_state_and_usage(tmp_path: Path) -> None:
    fake = FakeAttendance()
    body = session_doc()
    body["turns"] = [turn_doc(), turn_doc(turn="01K5J9QWB2M4N6Q8S0V2W4Y6A8", state="running")]
    fake.answer(DETAIL_PATH, body)

    answer = reader(tmp_path, fake).detail(CHAT, OWUI)

    assert answer.session is not None
    assert [one.state for one in answer.turns] == ["settled", "running"]
    assert answer.turns[0].usage.cost_usd == 0.014
    assert answer.turns[0].usage.tokens == 4308
    assert answer.turns[1].running


def test_a_turn_without_usage_reads_zero_not_a_crash(tmp_path: Path) -> None:
    fake = FakeAttendance()
    body = session_doc()
    body["turns"] = [turn_doc(usage=None)]
    fake.answer(DETAIL_PATH, body)

    answer = reader(tmp_path, fake).detail(CHAT, OWUI)

    assert answer.turns[0].usage.tokens == 0
    assert answer.turns[0].usage.cost_usd is None


def test_the_event_stream_is_split_on_line_feed_alone(tmp_path: Path) -> None:
    """Contract 02 §8: U+2028 is legal inside a JSON string."""
    fake = FakeAttendance()
    fake.answer(
        EVENTS_PATH,
        json.dumps(
            journal_line(1, "note", {"text": f"one{LINE_SEP}two"}), ensure_ascii=False
        ).encode("utf-8")
        + b"\n",
    )

    stream = reader(tmp_path, fake).events(CHAT, OWUI)

    assert len(stream.lines) == 1
    assert stream.problems == ()


def test_the_stream_asks_for_a_replay_and_never_follows(tmp_path: Path) -> None:
    fake = FakeAttendance()
    fake.answer(EVENTS_PATH, ndjson([journal_line(7, "note", {})]))

    reader(tmp_path, fake).events(CHAT, OWUI, from_seq=6)

    _, params = fake.calls[0]
    assert params == {"from_seq": "6", "follow": "false"}


def test_a_broken_journal_line_never_hides_the_rest(tmp_path: Path) -> None:
    fake = FakeAttendance()
    fake.answer(EVENTS_PATH, ndjson([journal_line(1, "note", {})]) + b"{ broken\n")

    stream = reader(tmp_path, fake).events(CHAT, OWUI)

    assert len(stream.lines) == 1
    assert stream.problems


def test_an_enormous_stream_is_capped_and_says_so(tmp_path: Path) -> None:
    fake = FakeAttendance()
    rows = [journal_line(one, "note", {}) for one in range(MAX_STREAM_LINES + 5)]
    fake.answer(EVENTS_PATH, ndjson(rows))

    stream = reader(tmp_path, fake).events(CHAT, OWUI)

    assert len(stream.lines) == MAX_STREAM_LINES
    assert stream.truncated
    assert stream.problems


def test_the_list_limit_is_held_to_the_contract_cap(tmp_path: Path) -> None:
    fake = FakeAttendance()
    fake.answer(LIST_PATH, {"sessions": []})

    reader(tmp_path, fake).sessions(limit=9999)

    _, params = fake.calls[0]
    assert params["limit"] == "200"


def test_free_labels_are_bounded(tmp_path: Path) -> None:
    """§4.2 calls labels free strings, so a family writes them."""
    labels = {f"k{index}": "x" * 900 for index in range(60)}
    fake = FakeAttendance()
    fake.answer(LIST_PATH, {"sessions": [session_doc(labels=labels)]})

    answer = reader(tmp_path, fake).sessions()

    kept = answer.rows[0].labels
    assert len(kept) == 20
    assert all(len(one) <= 500 for one in kept.values())
