"""A form save keeps the file's comments.

The real family files carry a comment above every grant explaining why it
is there. Regenerating the document from the model would drop all of them
on the first save, so the file the operator edited would stop explaining
itself the moment they touched it.

`yamlkeep.edited_text` patches the bytes that are there instead: it loads
the original round-trip, replaces only the fields whose MODEL value moved,
and dumps. Everything it does not touch -- comments, key order, flow style,
quoting -- comes back byte for byte.

The fixture is `fixtures/commented-family.yaml`, a family file that carries
a comment of every shape.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_family import parse_family
from noticeboard.yamlkeep import edited_text

FIXTURE = Path(__file__).parent / "fixtures" / "commented-family.yaml"

#: Lines from the fixture that no save may drop. One per shape: a header
#: comment, a comment inside a list, a comment inside a mapping, and an
#: end-of-line comment.
KEPT_COMMENTS = (
    "# A family file with a comment of every shape a save must keep: this",
    "# The whole vault to read, and one memories directory per vault area to",
    "# One target: a ticket is asked for, never written.",
    "every reach goes through the PEP, so every reach is audited",
)


def original() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def model_of(text: str) -> dict[str, Any]:
    family, issues = parse_family(text)
    assert family is not None, issues

    return family.model_dump(mode="json")


def saved(**changes: object) -> str:
    """The fixture, saved through the form with those fields changed."""
    text = original()
    before = model_of(text)
    after = dict(before)
    after.update(changes)

    return edited_text(text, before, after) or ""


def test_a_save_that_changes_nothing_returns_the_same_bytes() -> None:
    """`registrywrite.save_family` compares the new text with the file and
    makes no commit when they match. That comparison only ever succeeds if
    an unchanged save is byte-identical, so this is what makes "a save that
    changes nothing produces NO commit" true."""
    assert saved() == original()


def test_a_changed_field_keeps_every_comment() -> None:
    text = saved(description="A new description.")

    for comment in KEPT_COMMENTS:
        assert comment in text, comment


def test_a_changed_field_actually_changes() -> None:
    text = saved(description="A new description.")

    assert model_of(text)["description"] == "A new description."


def test_an_untouched_field_keeps_its_flow_style() -> None:
    """`model:` and every `files:` entry are flow mappings in this file, and
    `tools:` values are flow sequences. A dump that expanded them would
    rewrite two thirds of the document for a one-field edit."""
    text = saved(description="A new description.")

    assert "model: { router: agent-router, budget_usd_per_day: 15 }" in text
    assert "- { path: /srv/agents/vault, mode: ro }" in text
    assert "kagi: [kagi_search_fetch, kagi_extract]" in text


def top_keys(text: str) -> list[str]:
    return [line.split(":")[0] for line in text.splitlines() if line and line[0].isalpha()]


def test_an_untouched_field_keeps_its_place() -> None:
    """Key order is the author's, not the schema's."""
    assert top_keys(saved(description="A new description.")) == top_keys(original())


def test_a_nested_change_leaves_its_neighbours_alone() -> None:
    """Only the one key inside `model:` moves. Its sibling, and the comment
    above the block, stay as they were."""
    text = saved(model={"router": "agent-router", "budget_usd_per_day": 9})

    assert model_of(text)["model"]["budget_usd_per_day"] == 9
    assert model_of(text)["model"]["router"] == "agent-router"
    assert "# The chat router. The budget is 15 USD per day." in text


def test_a_removed_tool_leaves_the_file_valid() -> None:
    """A key the form deleted has to go, or the save would not be the edit
    the operator asked for."""
    tools = dict(model_of(original())["tools"])
    del tools["kagi"]
    text = saved(tools=tools)

    assert "kagi:" not in text
    assert "ha:" in text
    assert "kagi" not in model_of(text)["tools"]


def test_an_added_tool_lands_in_the_block() -> None:
    tools = dict(model_of(original())["tools"])
    tools["vikunja-work"] = "all"
    text = saved(tools=tools)

    assert model_of(text)["tools"]["vikunja-work"] == "all"
    assert "# The four reads contract 01 §8.1 names." in text


def test_text_that_is_not_a_mapping_is_refused() -> None:
    """The caller falls back to the emitter rather than writing nonsense."""
    assert edited_text("- one\n- two\n", {}, {"name": "chat"}) is None


def test_text_that_does_not_parse_is_refused() -> None:
    assert edited_text("name: [unclosed\n", {}, {"name": "chat"}) is None
