"""Journal lines folded into a readable transcript (contract 02 §8)."""

from __future__ import annotations

from typing import Any

from agent_view.transcript import MAX_ANSWER_CHARS, Voice, fold
from view_helpers import journal_line

TURN = "01K5J9QWB9R1V3T5Y7H9J2K4P6"


def delta(seq: int, text: str, turn: str = TURN) -> dict[str, Any]:
    return journal_line(
        seq,
        "pi_event",
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": text},
        },
        turn,
    )


def test_a_prompt_and_its_answer_become_two_entries() -> None:
    lines = [
        journal_line(1, "turn_started", {"prompt": "when is the boiler due?"}, TURN),
        delta(2, "The boiler "),
        delta(3, "is due in March."),
        journal_line(4, "turn_settled", {"usage": {}}, TURN),
    ]

    entries = fold(lines)

    assert [one.voice for one in entries] == [Voice.PROMPT, Voice.ANSWER, Voice.NOTE]
    assert entries[0].text == "when is the boiler due?"
    assert entries[1].text == "The boiler is due in March."


def test_a_heartbeat_never_enters_the_sequence() -> None:
    """Contract 02 §8.1: it is the one kind not written to disk."""
    beat: dict[str, Any] = {"journal_seq": None, "kind": "heartbeat", "body": {"last_seq": 1}}
    lines = [journal_line(1, "turn_started", {"prompt": "hello"}, TURN), beat]

    entries = fold(lines)

    assert len(entries) == 1


def test_settled_is_keyed_on_the_turn_settled_line() -> None:
    """§8.2: never on the wrapped agent_settled event. The window between
    the two is real, and a page read inside it shows the turn in flight."""
    lines = [
        journal_line(1, "turn_started", {"prompt": "hello"}, TURN),
        delta(2, "hi"),
        journal_line(3, "pi_event", {"type": "agent_settled"}, TURN),
    ]

    entries = fold(lines)

    assert not any(one.text == "turn settled" for one in entries)


def test_an_answer_still_in_flight_is_shown() -> None:
    entries = fold([delta(1, "half an ans")])

    assert entries[0].voice is Voice.ANSWER
    assert entries[0].text == "half an ans"


def test_a_stream_overrun_reads_as_a_note_not_a_failure() -> None:
    """§14: it is not a turn failure and no event was lost."""
    overrun: dict[str, Any] = {
        "journal_seq": None,
        "ts": "2026-09-19T12:00:00Z",
        "kind": "note",
        "turn": None,
        "body": {"note": "stream_overrun", "last_seq": 118},
    }

    entries = fold([overrun])

    assert entries[0].voice is Voice.NOTE
    assert "nothing was lost" in entries[0].text


def test_a_failure_carries_its_reason_and_message() -> None:
    lines = [
        journal_line(
            1, "turn_failed", {"reason": "approval_denied", "message": "the operator said no"}, TURN
        )
    ]

    entries = fold(lines)

    assert entries[0].voice is Voice.FAILURE
    assert "approval_denied" in entries[0].text
    assert entries[0].detail == "the operator said no"


def test_an_approval_shows_the_tool_and_the_decision() -> None:
    lines = [
        journal_line(
            1, "approval_requested", {"tool": "ha__ha_call", "summary": "unlock the door"}, TURN
        ),
        journal_line(2, "approval_resolved", {"decision": "approved", "waited_s": 12}, TURN),
    ]

    entries = fold(lines)

    assert [one.voice for one in entries] == [Voice.APPROVAL, Voice.APPROVAL]
    assert "ha__ha_call" in entries[0].text
    assert entries[1].text == "approved after 12s"


def test_a_stale_status_at_turn_start_is_said_on_the_prompt() -> None:
    lines = [journal_line(1, "turn_started", {"prompt": "hi", "status_stale": True}, TURN)]

    entries = fold(lines)

    assert "stale" in entries[0].detail


def test_an_enormous_answer_is_capped_and_says_so() -> None:
    lines = [delta(index, "x" * 1000) for index in range(1, 60)]

    entries = fold(lines)

    assert entries[0].truncated
    assert len(entries[0].text) == MAX_ANSWER_CHARS


def test_a_kind_this_view_has_no_word_for_is_dropped() -> None:
    """§8.1 calls the set closed, so an unknown kind is a bad line."""
    entries = fold([journal_line(1, "invented_kind", {"anything": 1}, TURN)])

    assert entries == ()


def test_a_line_with_no_body_does_not_raise() -> None:
    entries = fold([{"kind": "turn_started", "turn": TURN}])

    assert entries[0].voice is Voice.PROMPT
    assert entries[0].text == ""


def test_two_turns_keep_their_answers_apart() -> None:
    other = "01K5J9QWB2M4N6Q8S0V2W4Y6A8"
    lines = [
        journal_line(1, "turn_started", {"prompt": "one"}, TURN),
        delta(2, "first"),
        journal_line(3, "turn_settled", {}, TURN),
        journal_line(4, "turn_started", {"prompt": "two"}, other),
        delta(5, "second", other),
        journal_line(6, "turn_settled", {}, other),
    ]

    entries = fold(lines)
    answers = [one for one in entries if one.voice is Voice.ANSWER]

    assert [one.text for one in answers] == ["first", "second"]
    assert [one.turn for one in answers] == [TURN, other]
