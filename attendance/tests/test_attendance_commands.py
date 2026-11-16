"""The command list of a family's skills (`commands.py`).

Each case holds the door to the parse of `agent/src/skills.ts` and the
checks of Flue's `defineSkill`, so the door's list equals the facade's.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from attendance.commands import MAX_SKILL_BYTES, Command, command_of, read_commands

GOOD = "---\nname: grill-me\ndescription: Ask hard questions.\n---\nAsk.\n"


def test_a_skill_gives_its_name_and_description() -> None:
    assert command_of("grill-me", GOOD) == Command("grill-me", "Ask hard questions.")


def test_a_quoted_scalar_loses_its_quotes() -> None:
    text = "---\nname: 'tdd'\ndescription: \"Test first.\"\n---\nWrite it.\n"

    assert command_of("tdd", text) == Command("tdd", "Test first.")


def test_a_skill_with_no_name_takes_its_directory() -> None:
    assert command_of("tdd", "---\ndescription: Test first.\n---\nWrite it.\n") == Command(
        "tdd", "Test first."
    )


@pytest.mark.parametrize(
    "text",
    [
        "no frontmatter",
        "---\nname: grill-me\n---\nAsk.\n",
        "---\nname: grill-me\ndescription: Ask.\n---\n   \n",
        "---\nname: Grill_Me\ndescription: Ask.\n---\nAsk.\n",
        "---\nname: grill--me\ndescription: Ask.\n---\nAsk.\n",
        f"---\nname: {'a' * 65}\ndescription: Ask.\n---\nAsk.\n",
        f"---\nname: grill-me\ndescription: {'d' * 1025}\n---\nAsk.\n",
    ],
    ids=[
        "no_frontmatter",
        "no_description",
        "no_body",
        "bad_name",
        "double_hyphen",
        "long_name",
        "long_description",
    ],
)
def test_flue_s_refusals_give_no_command(text: str) -> None:
    assert command_of("grill-me", text) is None


def _skill(root: Path, directory: str, text: str) -> None:
    (root / directory).mkdir(parents=True)
    (root / directory / "SKILL.md").write_text(text, encoding="utf-8")


def test_the_list_is_by_name_and_leaves_out_a_bad_skill(tmp_path: Path) -> None:
    _skill(tmp_path, "tdd", "---\nname: tdd\ndescription: Test first.\n---\nWrite it.\n")
    _skill(tmp_path, "grill-me", GOOD)
    _skill(tmp_path, "broken", "no frontmatter")
    _skill(tmp_path, "huge", GOOD + "x" * MAX_SKILL_BYTES)

    assert read_commands(tmp_path) == [
        Command("grill-me", "Ask hard questions."),
        Command("tdd", "Test first."),
    ]


def test_a_family_with_no_skills_directory_has_no_commands(tmp_path: Path) -> None:
    assert read_commands(tmp_path / "skills") == []
