"""The family config mount: instructions.md, skills/, runtime.json,
rebuilt whole on every write (contract 01 section 6.1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from caregiver.config_mount import RuntimeConfig, config_mount_matches, write_config_mount


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
        skills={"grill-me": "# Grill Me\n", "handoff": "# Handoff\n"},
        runtime=RuntimeConfig(shell=False, sandbox_tools=(), model_alias="agent-router"),
    )
    assert (target / "skills" / "grill-me" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "# Grill Me\n"
    assert (target / "skills" / "handoff" / "SKILL.md").read_text(encoding="utf-8") == "# Handoff\n"


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
        skills={"grill-me": "old text"},
        runtime=RuntimeConfig(False, (), "agent-router"),
    )
    write_config_mount(
        target,
        instructions="x",
        skills={"handoff": "new text"},
        runtime=RuntimeConfig(False, (), "agent-router"),
    )
    assert not (target / "skills" / "grill-me").exists()
    assert (target / "skills" / "handoff" / "SKILL.md").read_text(encoding="utf-8") == "new text"


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
        "skills": {"notes": "Take notes."},
        "runtime": RuntimeConfig(shell=False, sandbox_tools=(), model_alias="agent-router"),
    }
    write_config_mount(target, **revision)
    assert config_mount_matches(target, **revision) is True

    (target / name).write_bytes(b"You are \xff chat.")

    assert config_mount_matches(target, **revision) is False
