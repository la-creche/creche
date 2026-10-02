"""`quiet/board.py`: the fingerprint moves with every move the lead acts on
and with nothing else."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from agent_door_trigger.quiet.board import fingerprint


def _ticket(ident: int, **fields: Any) -> dict[str, Any]:
    ticket: dict[str, Any] = {
        "id": ident,
        "ref": f"#{ident}",
        "title": f"ticket {ident}",
        "column": "TRIAGE",
        "epic": None,
        "points": None,
        "refined": False,
        "labels": [],
        "assignees": [],
    }
    ticket.update(fields)
    return ticket


def _survey() -> dict[str, Any]:
    """agent-mcp `lead/board.py`'s `survey()` shape."""
    return {
        "project": "House",
        "columns": {
            "TRIAGE": [_ticket(14)],
            "TODO": [_ticket(12, epic="finance", points=3, refined=True)],
            "IN PROGRESS": [],
            "BLOCKED": [],
            "VERIFICATION": [],
            "DONE": [_ticket(3, epic="networking", points=1, refined=True)],
        },
    }


def test_the_same_board_has_the_same_fingerprint() -> None:
    first = fingerprint(_survey())

    assert first is not None
    assert len(first) == 64
    assert fingerprint(_survey()) == first


def test_order_titles_and_assignees_do_not_move_it() -> None:
    board = _survey()
    board["columns"]["DONE"].insert(0, _ticket(2, title="renamed", assignees=["bot-finance"]))
    moved = copy.deepcopy(board)
    moved["columns"]["DONE"].reverse()
    moved["columns"]["DONE"][0]["title"] = "renamed again"

    assert fingerprint(moved) == fingerprint(board)


MOVES: tuple[tuple[str, Any], ...] = (
    ("points", 5),
    ("refined", False),
    ("epic", "agent-control"),
)


@pytest.mark.parametrize(("field", "value"), MOVES)
def test_a_changed_field_moves_it(field: str, value: Any) -> None:
    board = _survey()
    board["columns"]["TODO"][0][field] = value

    assert fingerprint(board) != fingerprint(_survey())


def test_a_ticket_changing_column_moves_it() -> None:
    board = _survey()
    board["columns"]["TODO"].append(board["columns"]["TRIAGE"].pop())

    assert fingerprint(board) != fingerprint(_survey())


def test_a_new_ticket_moves_it() -> None:
    board = _survey()
    board["columns"]["TRIAGE"].append(_ticket(15))

    assert fingerprint(board) != fingerprint(_survey())


def test_an_empty_board_is_a_board() -> None:
    assert fingerprint({"columns": {}}) is not None


BROKEN: tuple[object, ...] = (
    None,
    "not a survey",
    {"refused": True, "reason": "no"},
    {"columns": []},
    {"columns": {"TODO": "x"}},
    {"columns": {"TODO": [{"id": "12", "refined": True}]}},
    {"columns": {"TODO": [{"id": True, "refined": True}]}},
    {"columns": {"TODO": [{"id": 12, "points": "3", "refined": True}]}},
    {"columns": {"TODO": [{"id": 12, "refined": "yes"}]}},
    {"columns": {"TODO": [{"id": 12, "refined": True, "epic": 7}]}},
)


@pytest.mark.parametrize("answer", BROKEN)
def test_anything_else_is_no_fingerprint(answer: object) -> None:
    assert fingerprint(answer) is None
