"""Pairing a terminal's pi entries into exchanges (contract 02 §10.5)."""

from __future__ import annotations

from agent_sessiond.terminal import pair
from agent_sessiond.wire import PiEntry


def entry(entry_id: str, role: str, text: str = "") -> PiEntry:
    return PiEntry(id=entry_id, role=role, text=text)


def test_pairs_a_user_entry_with_the_answer_that_follows() -> None:
    read = pair(
        [
            entry("e1", "user", "what did the sensor read"),
            entry("e2", "assistant", "21.4 degrees"),
        ]
    )

    assert len(read.exchanges) == 1
    assert read.exchanges[0].prompt == "what did the sensor read"
    assert read.exchanges[0].answer == "21.4 degrees"
    assert read.exchanges[0].user_entry_id == "e1"
    assert read.exchanges[0].entry_id == "e2"
    assert read.cursor == "e2"


def test_leaves_a_trailing_user_entry_for_the_next_read() -> None:
    # Rule 2. The terminal may have been killed before pi answered, and the
    # cursor stopping short is what lets the next read see the whole thing.
    read = pair(
        [
            entry("e1", "user", "one"),
            entry("e2", "assistant", "first"),
            entry("e3", "user", "two"),
        ]
    )

    assert len(read.exchanges) == 1
    assert read.cursor == "e2"


def test_skips_a_role_that_is_neither_user_nor_assistant() -> None:
    read = pair(
        [
            entry("e1", "user", "one"),
            entry("e2", "tool", "a tool call"),
            entry("e3", "assistant", "first"),
        ]
    )

    assert len(read.exchanges) == 1
    assert read.exchanges[0].answer == "first"
    assert "a tool call" not in read.exchanges[0].prompt


def test_skips_an_entry_the_host_already_knows() -> None:
    # A turn this service ran wrote its own entries into the same store.
    read = pair(
        [
            entry("e1", "user", "a turn"),
            entry("e2", "assistant", "its answer"),
            entry("e3", "user", "a terminal exchange"),
            entry("e4", "assistant", "its answer"),
        ],
        known=["e1", "e2"],
    )

    assert len(read.exchanges) == 1
    assert read.exchanges[0].prompt == "a terminal exchange"
    # The cursor still passes the known pair, or it would read them forever.
    assert read.cursor == "e4"


def test_an_empty_read_moves_nothing() -> None:
    read = pair([])

    assert read.exchanges == []
    assert read.cursor is None


def test_a_second_user_entry_with_no_answer_replaces_the_first() -> None:
    read = pair(
        [
            entry("e1", "user", "abandoned"),
            entry("e2", "user", "asked again"),
            entry("e3", "assistant", "answered"),
        ]
    )

    assert len(read.exchanges) == 1
    assert read.exchanges[0].prompt == "asked again"
    assert read.exchanges[0].user_entry_id == "e2"
