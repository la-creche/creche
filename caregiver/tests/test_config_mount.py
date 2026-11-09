"""The family config mount: instructions.md, skills/, runtime.json and
shape.json, rebuilt whole on every write (contract 01 section 6.1,
decisions CH23-1, CH36 G6, CH43 rule 3, CH45)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final, cast

import pytest
from agent_family import FamilyFile
from agent_family.parse import Markup, parse_family
from caregiver.config_mount import (
    RuntimeConfig,
    config_mount_matches,
    read_model,
    write_config_mount,
)
from caregiver.paths import config_dir
from caregiver.steps import model_live_at, write_config
from creche_wire import FamilyShape, read, strictjson

#: The shape of the default `sandbox` fields.
SHAPE: Final = FamilyShape.model_validate({"cpus": 2, "flavor": "base", "memory_mib": 2048})

#: The wire vectors of `creche-wire`. The vmm reads `shape.json` as the
#: Rust `FamilyShape` that these vectors pin.
WIRE_VECTORS: Final = (
    Path(__file__).resolve().parents[2] / "rust" / "crates" / "wire" / "vectors" / "wire.json"
)

#: `SHAPE_CAP` of `rust/crates/vmm-confine/src/family.rs`: the most bytes
#: of `shape.json` that the vmm reads (decision CH50 rule 9).
VMM_SHAPE_CAP: Final = 4096


def test_writes_instructions_and_runtime(tmp_path: Path) -> None:
    target = tmp_path / "config"
    write_config_mount(
        target,
        instructions="You are chat. Know the operator.",
        skills={},
        runtime=RuntimeConfig(
            shell=False, sandbox_tools=("read", "grep"), model_alias="agent-router"
        ),
        shape=SHAPE,
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
        shape=SHAPE,
    )
    assert (target / "skills" / "grill-me" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "# Grill Me\n"
    assert (target / "skills" / "handoff" / "SKILL.md").read_text(encoding="utf-8") == "# Handoff\n"


def test_empty_skills_still_makes_the_directory(tmp_path: Path) -> None:
    target = tmp_path / "config"
    write_config_mount(
        target,
        instructions="x",
        skills={},
        runtime=RuntimeConfig(False, (), "agent-router"),
        shape=SHAPE,
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
        shape=SHAPE,
    )
    write_config_mount(
        target,
        instructions="x",
        skills={"handoff": "new text"},
        runtime=RuntimeConfig(False, (), "agent-router"),
        shape=SHAPE,
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
        shape=SHAPE,
    )
    write_config_mount(
        target,
        instructions="new",
        skills={},
        runtime=RuntimeConfig(True, ("read", "write"), "code-router", "replace"),
        shape=SHAPE,
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
        target,
        instructions="x",
        skills={},
        runtime=RuntimeConfig(False, (), "agent-router"),
        shape=SHAPE,
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
        "shape": SHAPE,
    }
    write_config_mount(target, **revision)
    assert config_mount_matches(target, **revision) is True

    (target / name).write_bytes(b"You are \xff chat.")

    assert config_mount_matches(target, **revision) is False


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


def _family(text: str) -> FamilyFile:
    family, issues = parse_family(text, Markup.TOML)
    assert family is not None, issues
    return family


def _shape_of(tmp_path: Path, family_text: str) -> FamilyShape:
    """`shape.json` as the vmm reads it: strict JSON under its cap, then
    the wire type."""
    family = _family(family_text)
    write_config(family, tmp_path, "x", {})
    data = (config_dir(tmp_path, family.name) / "shape.json").read_bytes()
    return read(FamilyShape, data, cap=VMM_SHAPE_CAP)


def _written_shapes() -> list[tuple[str, str]]:
    """Each accepted `FamilyShape` vector: its note and its written text."""
    vectors = strictjson.loads(WIRE_VECTORS.read_bytes(), cap=1 << 22)
    assert isinstance(vectors, dict)
    cases = cast(dict[str, list[dict[str, object]]], vectors)["FamilyShape"]
    return [(str(one["note"]), str(one["written"])) for one in cases if one["written"] is not None]


@pytest.mark.parametrize(("note", "written"), _written_shapes())
def test_shape_json_reads_back_as_each_wire_vector(tmp_path: Path, note: str, written: str) -> None:
    """The proof of the plan: the family file that gives each accepted
    `FamilyShape` vector writes a `shape.json` equal to it."""
    shape = read(FamilyShape, written.encode("utf-8"), cap=VMM_SHAPE_CAP)
    sandbox = (
        f"sandbox.cpus = {shape.cpus.root}\n"
        f'sandbox.image = "{shape.flavor}"\n'
        f'sandbox.memory = "{shape.memory_mib.root}m"\n'
    )
    assert _shape_of(tmp_path, _FAMILY_HEAD + sandbox) == shape, note


def test_the_default_sandbox_is_the_default_shape_vector(tmp_path: Path) -> None:
    assert _shape_of(tmp_path, _FAMILY_HEAD) == SHAPE


def test_shape_json_counts_1g_as_1024_mib(tmp_path: Path) -> None:
    text = _FAMILY_HEAD + 'sandbox.memory = "3g"\nsandbox.image = "coding"\n'
    assert _shape_of(tmp_path, text) == FamilyShape.model_validate(
        {"cpus": 2, "flavor": "coding", "memory_mib": 3072}
    )


def test_a_shape_change_does_not_match_the_mount(tmp_path: Path) -> None:
    target = tmp_path / "config"
    runtime = RuntimeConfig(False, (), "agent-router")
    write_config_mount(target, instructions="x", skills={}, runtime=runtime, shape=SHAPE)
    larger = FamilyShape.model_validate({"cpus": 4, "flavor": "base", "memory_mib": 2048})
    assert config_mount_matches(target, instructions="x", skills={}, runtime=runtime, shape=SHAPE)
    assert not config_mount_matches(
        target, instructions="x", skills={}, runtime=runtime, shape=larger
    )


def test_a_first_write_sets_no_model_live_at(tmp_path: Path) -> None:
    """Decision CH45 rule 1: the key is left out until a model change sets
    it."""
    assert "model_live_at" not in _runtime_of(tmp_path, _FAMILY_HEAD)
    assert model_live_at("chat", tmp_path) is None


def test_a_new_alias_sets_model_live_at_in_the_same_swap(tmp_path: Path) -> None:
    """Decision CH23-1: the alias and its live time change together."""
    _runtime_of(tmp_path, _FAMILY_HEAD)
    runtime = _runtime_of(tmp_path, _FAMILY_HEAD.replace("agent-router", "agent-fast"))
    assert runtime["model_alias"] == "agent-fast"
    live_at = runtime["model_live_at"]
    assert isinstance(live_at, str)
    assert model_live_at("chat", tmp_path) == live_at


def test_an_alias_that_stays_keeps_its_model_live_at(tmp_path: Path) -> None:
    """Decision CH23-1: `write_config` carries the old value forward, so
    a later edit neither drops nor restarts the 10 seconds."""
    _runtime_of(tmp_path, _FAMILY_HEAD)
    fast = _FAMILY_HEAD.replace("agent-router", "agent-fast")
    live_at = _runtime_of(tmp_path, fast)["model_live_at"]
    runtime = _runtime_of(tmp_path, fast + 'sandbox_tools = ["read"]\n')
    assert runtime["sandbox_tools"] == ["read"]
    assert runtime["model_live_at"] == live_at


def test_model_live_at_is_written_in_the_form_of_the_vmm(tmp_path: Path) -> None:
    """`live_at` of the vmm reads only `YYYY-MM-DDTHH:MM:SSZ` (see the
    CONTRACT-QUESTION of `RuntimeConfig`)."""
    target = tmp_path / "config"
    runtime = RuntimeConfig(False, (), "agent-fast", model_live_at="2026-11-06T12:00:10Z")
    write_config_mount(target, instructions="x", skills={}, runtime=runtime, shape=SHAPE)
    model = read_model(target)
    assert model is not None
    assert (model.alias, model.live_at) == ("agent-fast", "2026-11-06T12:00:10Z")
    assert json.loads((target / "runtime.json").read_text(encoding="utf-8")) == {
        "shell": False,
        "sandbox_tools": [],
        "model_alias": "agent-fast",
        "model_live_at": "2026-11-06T12:00:10Z",
    }


@pytest.mark.parametrize("body", ["[]", '{"model_live_at": "x"}', '{"model_alias": 1}'])
def test_a_runtime_json_of_another_shape_has_no_model(tmp_path: Path, body: str) -> None:
    target = tmp_path / "config"
    target.mkdir()
    (target / "runtime.json").write_text(body, encoding="utf-8")
    assert read_model(target) is None
