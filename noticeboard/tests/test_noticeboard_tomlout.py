"""The whole TOML text of a family, from the model.

The writer is the fallback of a save and the text of a block of the form.
Each text it writes must read back, through the one reader, as the model
that went in. A value that does not come back cannot reach the registry: the
save checks the model and gives the text to the validator.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from agent_family import Markup, parse_family
from agent_family.tomlfile import FileKind, loads
from noticeboard.tomlout import INDENT, to_toml
from noticeboard_helpers import CHAT_FAMILY_TOML, SCRUM_FAMILY_TOML

FIXTURES = Path(__file__).parent / "fixtures"

#: The TOML families of this suite. Each one is a converted family file.
FAMILIES = {
    "commented": (FIXTURES / "commented-family.toml").read_text(encoding="utf-8"),
    "styled": (FIXTURES / "styled-family.toml").read_text(encoding="utf-8"),
    "chat": CHAT_FAMILY_TOML,
    "scrum-lead": SCRUM_FAMILY_TOML,
}


def model_of(text: str) -> dict[str, Any]:
    family, issues = parse_family(text, Markup.TOML)
    assert family is not None, issues

    return family.model_dump(mode="json")


def read(text: str) -> dict[str, Any]:
    """The values of a text, as the one reader gives them."""
    return loads(text.encode("utf-8"), FileKind.FAMILY)


@pytest.mark.parametrize("text", FAMILIES.values(), ids=FAMILIES.keys())
def test_the_text_of_a_model_reads_as_that_model(text: str) -> None:
    model = model_of(text)

    assert model_of(to_toml(model)) == model


@pytest.mark.parametrize("text", FAMILIES.values(), ids=FAMILIES.keys())
def test_the_text_has_no_table_line(text: str) -> None:
    """A `[table]` line takes each later key into its table. The text has
    dotted keys only."""
    written = to_toml(model_of(text))

    assert [line for line in written.splitlines() if line.startswith("[")] == []


def test_a_nested_mapping_is_dotted_keys() -> None:
    written = to_toml({"model": {"router": "agent-router", "budget_usd_per_day": 15.0}})

    assert written == 'model.router = "agent-router"\nmodel.budget_usd_per_day = 15.0\n'


def test_a_list_of_tables_has_one_line_for_each_item() -> None:
    written = to_toml({"files": [{"path": "/srv/a", "mode": "ro"}, {"path": "/srv/b"}]})

    assert written == (
        "files = [\n"
        f'{INDENT}{{ path = "/srv/a", mode = "ro" }},\n'
        f'{INDENT}{{ path = "/srv/b" }},\n'
        "]\n"
    )


def test_a_list_of_scalars_is_one_line() -> None:
    assert to_toml({"egress": ["a.example.test", "b.example.test"]}) == (
        'egress = ["a.example.test", "b.example.test"]\n'
    )


def test_an_empty_mapping_and_an_empty_list_keep_their_key() -> None:
    """`tools = {}` and `egress = []` say that nothing is granted."""
    assert to_toml({"tools": {}, "egress": []}) == "tools = {}\negress = []\n"


def test_a_key_with_no_value_gets_no_line() -> None:
    """TOML has no null. An absent key reads as `None` in the model."""
    assert to_toml({"name": "chat", "job": None, "quiet": {"board": None}}) == (
        'name = "chat"\nquiet = {}\n'
    )


def test_a_table_in_a_list_drops_a_key_with_no_value() -> None:
    written = to_toml({"triggers": [{"cron": "0 6 * * *", "webhook": None, "enqueue": None}]})

    assert read(written) == {"triggers": [{"cron": "0 6 * * *"}]}


def test_a_table_in_a_table_in_a_list_is_dotted_keys() -> None:
    written = to_toml({"verbs": [{"enqueue": {"targets": ["scrum-lead"]}}]})

    assert f'{INDENT}{{ enqueue.targets = ["scrum-lead"] }},' in written.splitlines()


#: Texts with each sign that a basic string escapes, and two that it keeps.
TEXTS = {
    "a-quote": 'a "quoted" word',
    "a-backslash": "a\\b",
    "a-line-end": "one\ntwo",
    "a-tab-and-a-return": "a\tb\rc",
    "a-control-character": "a\x07b",
    "the-delete-character": "a\x7fb",
    "a-letter-outside-ascii": "café ☃",
    "a-hash": "not # a comment",
}


@pytest.mark.parametrize("text", TEXTS.values(), ids=TEXTS.keys())
def test_a_text_comes_back_as_it_went_in(text: str) -> None:
    assert read(to_toml({"description": text})) == {"description": text}


def test_a_control_character_is_an_escape() -> None:
    assert to_toml({"description": "a\x07b"}) == 'description = "a\\u0007b"\n'


def test_a_key_that_is_not_bare_is_quoted() -> None:
    written = to_toml({"tools": {"a.b": "all", "plain-key_1": "all"}})

    assert read(written) == {"tools": {"a.b": "all", "plain-key_1": "all"}}
    assert 'tools."a.b" = "all"' in written.splitlines()


@pytest.mark.parametrize(
    ("value", "spelled"),
    [(True, "true"), (False, "false"), (7, "7"), (2.5, "2.5"), (1e16, "1e+16")],
)
def test_a_scalar_has_its_toml_spelling(value: object, spelled: str) -> None:
    assert to_toml({"value": value}) == f"value = {spelled}\n"


@pytest.mark.parametrize(
    ("value", "spelled"), [(float("inf"), "inf"), (float("-inf"), "-inf"), (float("nan"), "nan")]
)
def test_a_float_that_is_not_finite_has_its_toml_word(value: float, spelled: str) -> None:
    assert to_toml({"value": value}) == f"value = {spelled}\n"


def test_an_integer_past_64_bits_reaches_no_family() -> None:
    """The writer writes the digits. The reader refuses them, so the check
    of the save refuses the text."""
    family, issues = parse_family(
        to_toml({"name": "chat", "max_running_turns": 2**64}), Markup.TOML
    )

    assert family is None
    assert issues
