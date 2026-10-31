"""Skill helper files: the declaration, the staged texts, and the faults.

A skill declares its helpers in its own frontmatter; the loader stages what
the directory honors and faults what it does not; the validator puts every
fault on each family that grants the skill."""

from __future__ import annotations

import shutil
from pathlib import Path

from agent_family.registry import load_registry

from family_helpers import FIXTURES

#: One skill with helpers. `loop.sh` sits in a nested directory, so the
#: declaration has to carry a relative path, not only a sibling name.
_SKILL_MD = "---\nfiles:\n  - mocking.md\n  - scripts/loop.sh\n---\n# TDD\n"

#: How each fixture family comes to grant the new skill.
_GRANTS = {
    "chat": ('skills = ["grill-me", "handoff", "memory-writing"]', 'skills = ["helpers"]'),
    "scrum-lead": ("skills = []", 'skills = ["helpers"]'),
}


def _registry(
    tmp_path: Path,
    skill_md: str,
    helpers: dict[str, bytes],
    strays: dict[str, str] | None = None,
):
    """The fixture registry with a skill `helpers` granted by two families."""
    strays = strays or {}
    shutil.copytree(FIXTURES, tmp_path / "registry")
    root = tmp_path / "registry"
    skill = root / "skills" / "helpers"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(skill_md, encoding="utf-8")
    for name, content in helpers.items():
        path = skill / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    for name, content in strays.items():
        (skill / name).write_text(content, encoding="utf-8")

    for family, (before, after) in _GRANTS.items():
        toml = root / "families" / family / "family.toml"
        text = toml.read_text(encoding="utf-8")
        assert before in text
        toml.write_text(text.replace(before, after), encoding="utf-8")

    return load_registry(root)


def _skill_issues(registry, family: str) -> list[str]:
    issues = registry.reports[family].issues
    return [issue.msg for issue in issues if issue.loc.startswith("skills[")]


def test_a_honored_declaration_stages_each_helper(tmp_path: Path) -> None:
    registry = _registry(
        tmp_path,
        _SKILL_MD,
        {"mocking.md": b"# Mocking\n", "scripts/loop.sh": b"#!/bin/sh\n"},
    )
    assert registry.reports["chat"].ok
    files = registry.skill_texts["helpers"].files
    assert set(files) == {"SKILL.md", "mocking.md", "scripts/loop.sh"}
    assert files["scripts/loop.sh"] == "#!/bin/sh\n"
    assert registry.skill_texts["helpers"].faults == ()


def test_every_granting_family_carries_the_faults(tmp_path: Path) -> None:
    registry = _registry(
        tmp_path,
        _SKILL_MD,
        {"mocking.md": b"# Mocking\n", "scripts/loop.sh": b"#!/bin/sh\n"},
    )
    assert _skill_issues(registry, "chat") == []
    assert _skill_issues(registry, "scrum-lead") == []


def test_a_missing_helper_is_a_fault_not_a_silence(tmp_path: Path) -> None:
    registry = _registry(tmp_path, _SKILL_MD, {"scripts/loop.sh": b"#!/bin/sh\n"})
    assert registry.skill_texts["helpers"].faults == (
        "declares 'mocking.md', which the skill directory does not hold",
    )
    assert any("declares 'mocking.md'" in msg for msg in _skill_issues(registry, "chat"))
    assert any("declares 'mocking.md'" in msg for msg in _skill_issues(registry, "scrum-lead"))


def test_a_stray_file_is_a_fault_not_a_staging(tmp_path: Path) -> None:
    registry = _registry(
        tmp_path,
        _SKILL_MD,
        {"mocking.md": b"# Mocking\n", "scripts/loop.sh": b"#!/bin/sh\n"},
        strays={"NOTES.md": "never declared\n"},
    )
    assert registry.skill_texts["helpers"].faults == (
        "holds 'NOTES.md', which its frontmatter does not declare",
    )


def test_an_unreadable_helper_is_a_fault(tmp_path: Path) -> None:
    registry = _registry(
        tmp_path,
        _SKILL_MD,
        {"mocking.md": b"\xff\xfe\x00bad", "scripts/loop.sh": b"#!/bin/sh\n"},
    )
    assert registry.skill_texts["helpers"].faults == (
        "has an unreadable helper 'mocking.md' (cannot read mocking.md: it is not UTF-8 text)",
    )


def test_a_frontmatter_that_never_closes_is_a_fault(tmp_path: Path) -> None:
    registry = _registry(tmp_path, "---\nfiles:\n  - mocking.md\n# TDD\n", {"mocking.md": b"x\n"})
    assert registry.skill_texts["helpers"].faults == ("has a frontmatter that never closes",)


def test_a_path_that_leaves_the_directory_is_dropped_and_named(tmp_path: Path) -> None:
    registry = _registry(tmp_path, "---\nfiles:\n  - ../escape.md\n---\n", {})
    assert registry.skill_texts["helpers"].faults == (
        "declares '../escape.md', which is not a helper path (is not a plain relative path)",
    )
