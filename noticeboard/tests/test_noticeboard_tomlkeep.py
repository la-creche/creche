"""A form save keeps the comments of a TOML family file.

This suite is the TOML twin of `test_noticeboard_yamlkeep.py`.
`tomlkeep.edited_text` patches the bytes that are there: it reads the
original with `tomlkit`, replaces only the fields whose MODEL value moved,
and writes the text again. Each other line comes back byte for byte.

The fixtures `fixtures/commented-family.toml` and `fixtures/styled-family.toml`
are the twins of the two YAML fixtures. `bin/yaml-to-toml.py` wrote them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_family import Markup, parse_family
from noticeboard.tomlkeep import edited_text

FIXTURE = Path(__file__).parent / "fixtures" / "commented-family.toml"

#: A second family file, with the shapes that a save must leave alone.
STYLED = FIXTURE.with_name("styled-family.toml")

#: Lines from the fixture that no save may drop. One for each place: a
#: header comment, a comment inside a list, a comment inside a table, and an
#: end-of-line comment.
KEPT_COMMENTS = (
    "# A family file with a comment of every shape a save must keep: this",
    "    # The whole vault to read, and one memories directory per vault area to",
    "# The four reads contract 01 §8.1 names.",
    "egress = [] # every reach goes through the PEP, so every reach is audited",
)


def original() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def model_of(text: str) -> dict[str, Any]:
    family, issues = parse_family(text, Markup.TOML)
    assert family is not None, issues

    return family.model_dump(mode="json")


def edited(text: str, **changes: object) -> str:
    """`text`, saved through the form with those fields changed."""
    before = model_of(text)
    after = dict(before)
    after.update(changes)

    return edited_text(text, before, after) or ""


def saved(**changes: object) -> str:
    """The fixture, saved through the form with those fields changed."""
    return edited(original(), **changes)


def styled(**changes: object) -> str:
    """The styled fixture, saved through the form with those fields changed."""
    return edited(STYLED.read_text(encoding="utf-8"), **changes)


def lines_that_moved(text: str, source: Path = STYLED) -> list[str]:
    """Each line of `text` that `source` does not hold."""
    held = set(source.read_text(encoding="utf-8").splitlines())

    return [line for line in text.splitlines() if line not in held]


def test_a_save_that_changes_nothing_returns_the_same_bytes() -> None:
    """`registrywrite.save_family` makes no commit for a text equal to the
    file. Only an exact answer here makes that comparison hold."""
    assert saved() == original()


def test_a_save_that_changes_nothing_returns_a_text_that_tomlkit_cannot_read() -> None:
    """Rule 2 comes first: the original goes back, and nothing reads it."""
    text = "name = [unclosed\n"

    assert edited_text(text, {"name": "chat"}, {"name": "chat"}) == text


def test_a_changed_field_keeps_every_comment() -> None:
    text = saved(description="A new description.")

    for comment in KEPT_COMMENTS:
        assert comment in text.splitlines(), comment


def test_a_changed_field_actually_changes() -> None:
    text = saved(description="A new description.")

    assert model_of(text)["description"] == "A new description."


def test_a_change_of_one_field_changes_one_line() -> None:
    """The git log of the registry is the audit trail of the grants. A save
    that rewrites a line it did not edit is noise in that log."""
    text = saved(description="A new description.")

    assert lines_that_moved(text, FIXTURE) == ['description = "A new description."']
    assert len(text.splitlines()) == len(original().splitlines())


def test_an_untouched_field_keeps_its_layout() -> None:
    text = saved(description="A new description.")

    assert 'model.router = "agent-router"' in text.splitlines()
    assert '    { path = "/srv/agents/vault", mode = "ro" },' in text.splitlines()
    assert 'tools.kagi = ["kagi_search_fetch", "kagi_extract"]' in text.splitlines()


def keys_of(text: str) -> list[str]:
    return [line.split(" = ")[0] for line in text.splitlines() if line and line[0].isalpha()]


def test_an_untouched_field_keeps_its_place() -> None:
    """The order of the keys is the author's, not the schema's."""
    assert keys_of(saved(description="A new description.")) == keys_of(original())


def test_a_nested_change_leaves_its_neighbours_alone() -> None:
    text = saved(model={"router": "agent-router", "budget_usd_per_day": 9})

    assert model_of(text)["model"] == {"router": "agent-router", "budget_usd_per_day": 9}
    assert lines_that_moved(text, FIXTURE) == ["model.budget_usd_per_day = 9"]
    assert "# The chat router. The budget is 15 USD per day." in text.splitlines()


def test_a_removed_tool_leaves_the_file_valid() -> None:
    tools = dict(model_of(original())["tools"])
    del tools["kagi"]
    text = saved(tools=tools)

    assert "tools.kagi" not in text
    assert 'tools.ha = "all"' in text.splitlines()
    assert "kagi" not in model_of(text)["tools"]


def test_an_added_tool_lands_in_the_table() -> None:
    tools = dict(model_of(original())["tools"])
    tools["vikunja-work"] = "all"
    text = saved(tools=tools)

    assert model_of(text)["tools"]["vikunja-work"] == "all"
    assert 'tools.vikunja-work = "all"' in text.splitlines()
    assert "# The four reads contract 01 §8.1 names." in text.splitlines()


def test_a_table_that_loses_each_key_and_gains_one_takes_the_edit() -> None:
    """Each change of a table goes through a view of `tomlkit` that the
    module reads again. A view that a change went through does not follow
    that change."""
    text = saved(tools={"vikunja-work": "all"})

    assert model_of(text)["tools"] == {"vikunja-work": "all"}
    assert 'tools.vikunja-work = "all"' in text.splitlines()
    assert "# The four reads contract 01 §8.1 names." in text.splitlines()


def test_a_replaced_list_has_the_layout_of_the_writer() -> None:
    """A list goes whole. Its new text is the text of `tomlout.py`."""
    files = [{"path": "/srv/agents/vault", "mode": "rw"}]
    text = saved(files=files)

    assert model_of(text)["files"] == files
    assert '    { path = "/srv/agents/vault", mode = "rw" },' in text.splitlines()


def test_a_key_with_no_value_leaves_the_file() -> None:
    """TOML has no null, so the key goes, and the model reads `None`."""
    text = edited(
        'name = "scrum-lead"\nkind = "autonomous"\ndescription = "triage"\n'
        'model.router = "agent-router"\nmodel.budget_usd_per_day = 5\n'
        "max_running_turns = 1 # one at a time\n",
        max_running_turns=None,
    )

    assert "max_running_turns" not in text
    assert model_of(text)["max_running_turns"] is None


#: A small family file for the shapes below. Each test adds its own lines.
SMALL = """\
name = "scrum-lead"
kind = "autonomous"
description = "the morning triage agent"
model.router = "agent-router"
model.budget_usd_per_day = 5
"""

QUIET = {"board": None, "daily": {"call": "ha__ha_call", "hour": 7, "zone": "UTC"}}


def test_a_new_table_becomes_dotted_keys() -> None:
    text = edited(SMALL, quiet={**QUIET, "floor_hours": 6})

    assert model_of(text)["quiet"]["daily"]["hour"] == 7
    assert "quiet.daily.hour = 7" in text.splitlines()
    assert [line for line in text.splitlines() if line.startswith("[")] == []


def test_a_new_table_inside_a_table_of_the_file_stays_in_that_table() -> None:
    text = edited(
        SMALL + '\n[verbs]\nenqueue.targets = ["chat"]\n',
        verbs={
            "embed": None,
            "ha_call": None,
            "enqueue": {"targets": ["chat"]},
            "job_status": None,
            "release": {"components": ["noticeboard"]},
        },
    )

    assert model_of(text)["verbs"]["release"] == {"components": ["noticeboard"]}
    assert 'release.components = ["noticeboard"]' in text.splitlines()


def test_a_new_key_before_a_table_line_stays_out_of_that_table() -> None:
    text = edited(SMALL + "\n[sandbox]\ncpus = 2\n", max_running_turns=3)

    assert model_of(text)["max_running_turns"] == 3
    assert model_of(text)["sandbox"]["cpus"] == 2


def test_a_text_that_does_not_parse_is_refused() -> None:
    """The caller writes the text of `tomlout.py` in its place."""
    assert edited_text("name = [unclosed\n", {}, {"name": "chat"}) is None


def test_a_new_table_inside_an_inline_table_is_refused() -> None:
    """An inline table is one value. This module adds no table to it, and
    the caller writes the text of `tomlout.py`."""
    text = SMALL + "quiet = { floor_hours = 6 }\n"
    before = model_of(text)
    after = {**before, "quiet": {**QUIET, "floor_hours": 6}}

    assert edited_text(text, before, after) is None


def test_a_key_inside_an_inline_table_changes_in_place() -> None:
    text = edited(
        'name = "chat"\nkind = "attended"\ndescription = "d"\n'
        'model = { router = "agent-router", budget_usd_per_day = 15 } # the router\n',
        model={"router": "agent-router", "budget_usd_per_day": 9},
    )

    assert 'model = { router = "agent-router", budget_usd_per_day = 9 } # the router' in (
        text.splitlines()
    )


def test_a_one_field_edit_of_the_styled_file_moves_one_line() -> None:
    text = styled(shell=True)

    assert lines_that_moved(text) == ["shell = true"]
    assert len(text.splitlines()) == len(STYLED.read_text(encoding="utf-8").splitlines())


def test_an_edited_line_keeps_its_end_of_line_comment() -> None:
    text = styled(model={"router": "agent-router", "budget_usd_per_day": 9})

    assert lines_that_moved(text) == ["model.budget_usd_per_day = 9 # the chat router"]


def test_an_item_of_a_list_keeps_its_end_of_line_comment() -> None:
    text = styled(shell=True)

    assert '    { path = "/srv/agents/vault", mode = "ro" }, # the whole vault, to read' in (
        text.splitlines()
    )


def test_a_line_past_column_100_comes_back_whole() -> None:
    held = [line for line in STYLED.read_text(encoding="utf-8").splitlines() if len(line) > 100]
    assert len(held) == 2

    text = styled(shell=True).splitlines()

    for line in held:
        assert line in text


def test_removing_the_only_tool_leaves_a_file_that_parses() -> None:
    """The table goes, and `tools = {}` takes its place in the model. The
    comment above the tool stays: a save removes no comment."""
    text = styled(tools={})

    assert model_of(text)["tools"] == {}
    assert "kagi" not in text
    assert "tools = {}" in text.splitlines()
    assert "egress = []" in text.splitlines()
    assert "# Search only. No other server is granted." in text.splitlines()
