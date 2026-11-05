"""The family config mount: instructions.md, skills/, runtime.json,
rebuilt whole on every write (contract 01 section 6.1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from agent_family.parse import Markup, parse_family
from caregiver.config_mount import RuntimeConfig, config_mount_matches, write_config_mount
from caregiver.paths import config_dir
from caregiver.steps import write_config


def test_writes_instructions_and_runtime(tmp_path: Path) -> None:
    target = tmp_path / "config"
    write_config_mount(
        target,
        instructions="You are chat. Know the operator.",
        skills={},
        runtime=RuntimeConfig(
            shell=False, sandbox_tools=("read", "grep"), model_alias="agent-router"
        ),
    )
    assert (target / "instructions.md").read_text(
        encoding="utf-8"
    ) == "You are chat. Know the operator."
    runtime = json.loads((target / "runtime.json").read_text(encoding="utf-8"))
    # `append` is the default and is not written, so a release that adds the
    # field rewrites no family's mount (see `RuntimeConfig`).
    assert runtime == {
        "shell": False,
        "sandbox_tools": ["read", "grep"],
        "model_alias": "agent-router",
    }


def test_writes_one_skill_file_per_granted_skill(tmp_path: Path) -> None:
    target = tmp_path / "config"
    write_config_mount(
        target,
        instructions="x",
        skills={
            "grill-me": {"SKILL.md": "# Grill Me\n"},
            "handoff": {"SKILL.md": "# Handoff\n"},
        },
        runtime=RuntimeConfig(shell=False, sandbox_tools=(), model_alias="agent-router"),
    )
    assert (target / "skills" / "grill-me" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "# Grill Me\n"
    assert (target / "skills" / "handoff" / "SKILL.md").read_text(encoding="utf-8") == "# Handoff\n"


def test_writes_a_declared_helper_at_its_own_path(tmp_path: Path) -> None:
    """A skill's helpers sit beside its SKILL.md, and one may live in a
    directory of its own. The mount has to carry that directory with it."""
    target = tmp_path / "config"
    write_config_mount(
        target,
        instructions="x",
        skills={
            "diagnosing-bugs": {
                "SKILL.md": "# Diagnosing Bugs\n",
                "reference/checklist.md": "# Checklist\n",
            }
        },
        runtime=RuntimeConfig(False, (), "agent-router"),
    )
    skill = target / "skills" / "diagnosing-bugs"
    assert (skill / "SKILL.md").read_text(encoding="utf-8") == "# Diagnosing Bugs\n"
    assert (skill / "reference" / "checklist.md").read_text(encoding="utf-8") == "# Checklist\n"


def test_a_declared_path_outside_the_skill_directory_is_not_written(tmp_path: Path) -> None:
    """The registry is a user-editable tree, so a path a SKILL.md declares
    is not trusted here. A path that climbs out of the skill's directory is
    skipped, and the mount still matches on the files that are left."""
    target = tmp_path / "config"
    revision = {
        "instructions": "x",
        "skills": {"notes": {"SKILL.md": "s", "../escape.md": "no", "/etc/passwd": "no"}},
        "runtime": RuntimeConfig(False, (), "agent-router"),
    }
    write_config_mount(target, **revision)

    assert not (tmp_path / "escape.md").exists()
    assert not (target / "skills" / "escape.md").exists()
    assert (target / "skills" / "notes" / "SKILL.md").is_file()
    assert config_mount_matches(target, **revision) is True


def test_empty_skills_still_makes_the_directory(tmp_path: Path) -> None:
    target = tmp_path / "config"
    write_config_mount(
        target, instructions="x", skills={}, runtime=RuntimeConfig(False, (), "agent-router")
    )
    assert (target / "skills").is_dir()
    assert list((target / "skills").iterdir()) == []


def test_reapplying_drops_a_skill_that_is_no_longer_granted(tmp_path: Path) -> None:
    target = tmp_path / "config"
    write_config_mount(
        target,
        instructions="x",
        skills={"grill-me": {"SKILL.md": "old text"}},
        runtime=RuntimeConfig(False, (), "agent-router"),
    )
    write_config_mount(
        target,
        instructions="x",
        skills={"handoff": {"SKILL.md": "new text"}},
        runtime=RuntimeConfig(False, (), "agent-router"),
    )
    assert not (target / "skills" / "grill-me").exists()
    assert (target / "skills" / "handoff" / "SKILL.md").read_text(encoding="utf-8") == "new text"


def test_reapplying_drops_a_helper_that_is_no_longer_declared(tmp_path: Path) -> None:
    target = tmp_path / "config"
    write_config_mount(
        target,
        instructions="x",
        skills={"notes": {"SKILL.md": "s", "helper.md": "h"}},
        runtime=RuntimeConfig(False, (), "agent-router"),
    )
    assert (target / "skills" / "notes" / "helper.md").is_file()

    write_config_mount(
        target,
        instructions="x",
        skills={"notes": {"SKILL.md": "s"}},
        runtime=RuntimeConfig(False, (), "agent-router"),
    )
    assert not (target / "skills" / "notes" / "helper.md").exists()


def test_reapplying_updates_instructions_and_runtime(tmp_path: Path) -> None:
    target = tmp_path / "config"
    write_config_mount(
        target,
        instructions="old",
        skills={},
        runtime=RuntimeConfig(False, ("read",), "agent-router"),
    )
    write_config_mount(
        target,
        instructions="new",
        skills={},
        runtime=RuntimeConfig(True, ("read", "write"), "code-router", "replace"),
    )
    assert (target / "instructions.md").read_text(encoding="utf-8") == "new"
    runtime = json.loads((target / "runtime.json").read_text(encoding="utf-8"))
    assert runtime == {
        "shell": True,
        "sandbox_tools": ["read", "write"],
        "model_alias": "code-router",
        "system_prompt": "replace",
    }


def test_leaves_no_staging_directory_behind(tmp_path: Path) -> None:
    target = tmp_path / "config"
    write_config_mount(
        target, instructions="x", skills={}, runtime=RuntimeConfig(False, (), "agent-router")
    )
    remaining = {p.name for p in tmp_path.iterdir()}
    assert remaining == {"config"}


@pytest.mark.parametrize("name", ["instructions.md", "runtime.json", "skills/notes/SKILL.md"])
def test_a_mount_file_that_is_not_utf8_does_not_match(tmp_path: Path, name: str) -> None:
    """The pass asks whether the mount holds the revision before it writes
    the mount. A file that is not UTF-8 holds no revision, so the answer is
    no and the pass writes the mount again. A raise here would make each
    pass fail before that write."""
    target = tmp_path / "config"
    revision = {
        "instructions": "You are chat.",
        "skills": {"notes": {"SKILL.md": "Take notes."}},
        "runtime": RuntimeConfig(shell=False, sandbox_tools=(), model_alias="agent-router"),
    }
    write_config_mount(target, **revision)
    assert config_mount_matches(target, **revision) is True

    (target / name).write_bytes(b"You are \xff chat.")

    assert config_mount_matches(target, **revision) is False


def test_a_changed_helper_does_not_match(tmp_path: Path) -> None:
    target = tmp_path / "config"
    revision = {
        "instructions": "x",
        "skills": {"notes": {"SKILL.md": "s", "helper.md": "old"}},
        "runtime": RuntimeConfig(False, (), "agent-router"),
    }
    write_config_mount(target, **revision)
    assert config_mount_matches(target, **revision) is True

    (target / "skills" / "notes" / "helper.md").write_text("new", encoding="utf-8")

    assert config_mount_matches(target, **revision) is False


def test_a_helper_that_is_no_longer_declared_does_not_match(tmp_path: Path) -> None:
    """A stale helper left in the mount is a difference, so the next pass
    rebuilds the directory and takes the file away."""
    target = tmp_path / "config"
    write_config_mount(
        target,
        instructions="x",
        skills={"notes": {"SKILL.md": "s", "helper.md": "h"}},
        runtime=RuntimeConfig(False, (), "agent-router"),
    )
    without = {
        "instructions": "x",
        "skills": {"notes": {"SKILL.md": "s"}},
        "runtime": RuntimeConfig(False, (), "agent-router"),
    }

    assert config_mount_matches(target, **without) is False


_FAMILY_HEAD = (
    'name = "chat"\nkind = "attended"\ndescription = "d"\n'
    'model.router = "agent-router"\nmodel.budget_usd_per_day = 1\n'
)


def _runtime_of(tmp_path: Path, family_text: str) -> dict[str, object]:
    family, issues = parse_family(family_text, Markup.TOML)
    assert family is not None, issues
    write_config(family, tmp_path, "x", {})
    runtime = config_dir(tmp_path, family.name) / "runtime.json"
    return json.loads(runtime.read_text(encoding="utf-8"))


def test_runtime_holds_the_family_flue_tools(tmp_path: Path) -> None:
    text = _FAMILY_HEAD + 'sandbox_tools = ["read", "write", "edit", "bash", "grep", "glob"]\n'
    tools = _runtime_of(tmp_path, text)["sandbox_tools"]
    assert tools == ["read", "write", "edit", "bash", "grep", "glob"]


def test_runtime_tools_default_to_empty(tmp_path: Path) -> None:
    assert _runtime_of(tmp_path, _FAMILY_HEAD)["sandbox_tools"] == []
