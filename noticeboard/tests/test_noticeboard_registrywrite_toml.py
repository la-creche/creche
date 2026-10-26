"""The save of a TOML family file, and the name of the file of a family.

A save writes a family as `family.toml`. A directory that also holds a
`family.yaml` refuses the save.
"""

from __future__ import annotations

from pathlib import Path

from agent_family import Markup, load_registry, parse_family
from noticeboard.registrywrite import family_path, save_family
from noticeboard.tomlout import to_toml
from noticeboard_helpers import (
    CHAT_FAMILY_TOML,
    commit_count,
    git,
    make_registry,
)

GOOD = CHAT_FAMILY_TOML.replace("the house assistant", "the house assistant, rewritten")

#: `kind` is not one of contract 01's three, so the whole registry fails.
BAD = CHAT_FAMILY_TOML.replace('kind = "attended"', 'kind = "wizard"')


def names_in(directory: Path) -> list[str]:
    return sorted(one.name for one in directory.iterdir())


def toml_file(root: Path, name: str = "chat") -> Path:
    return root / "families" / name / "family.toml"


def test_a_toml_family_is_saved_as_family_toml(tmp_path: Path) -> None:
    root = make_registry(tmp_path)
    before = commit_count(root)

    result = save_family(root, "chat", GOOD, "widen the description")

    changed = git(root, "show", "--name-only", "--format=", "HEAD").stdout.split()
    assert result.ok, result.problem
    assert commit_count(root) == before + 1
    assert toml_file(root).read_text(encoding="utf-8") == GOOD
    assert changed == ["families/chat/family.toml"]
    assert names_in(root / "families" / "chat") == ["family.toml", "instructions.md"]


def test_the_path_of_a_toml_family_is_its_toml_file(tmp_path: Path) -> None:
    root = make_registry(tmp_path)

    assert family_path(root, "chat") == toml_file(root)


def test_an_unchanged_toml_save_commits_nothing(tmp_path: Path) -> None:
    root = make_registry(tmp_path)
    before = commit_count(root)

    result = save_family(root, "chat", CHAT_FAMILY_TOML, "no change")

    assert result.ok
    assert result.unchanged
    assert commit_count(root) == before


def test_an_invalid_toml_edit_writes_nothing(tmp_path: Path) -> None:
    root = make_registry(tmp_path)
    before = commit_count(root)
    original = toml_file(root).read_bytes()

    result = save_family(root, "chat", BAD, "break it")

    assert not result.ok
    assert result.errors
    assert commit_count(root) == before
    assert toml_file(root).read_bytes() == original
    assert git(root, "status", "--porcelain").stdout == ""


def test_a_new_family_in_a_toml_registry_is_family_toml(tmp_path: Path) -> None:
    root = make_registry(tmp_path)
    text = GOOD.replace('name = "chat"', 'name = "second"')

    result = save_family(root, "second", text, "add a family")

    assert result.ok, result.problem
    assert names_in(root / "families" / "second") == ["family.toml"]
    assert toml_file(root, "second").read_text(encoding="utf-8") == text


def test_a_new_family_in_a_registry_with_no_families_directory_is_family_toml(
    tmp_path: Path,
) -> None:
    root = tmp_path / "registry"
    root.mkdir()

    assert family_path(root, "chat") == toml_file(root)


def test_a_directory_with_both_names_refuses_the_save(tmp_path: Path) -> None:
    """The registry reads no family from such a directory, so the save that
    the validator judges is refused."""
    root = make_registry(tmp_path)
    yaml_file = root / "families" / "chat" / "family.yaml"
    yaml_file.write_text("name: chat\n", encoding="utf-8")
    before = commit_count(root)

    result = save_family(root, "chat", GOOD, "widen the description")

    assert not result.ok
    assert any("two family files" in one.msg for one in result.errors)
    assert commit_count(root) == before
    assert toml_file(root).read_text(encoding="utf-8") == CHAT_FAMILY_TOML
    assert yaml_file.read_text(encoding="utf-8") == "name: chat\n"


def test_the_path_of_a_directory_with_both_names_is_the_file_of_the_report(
    tmp_path: Path,
) -> None:
    """The registry names the TOML file in the report of such a directory.
    The save names the same file."""
    root = make_registry(tmp_path)
    (root / "families" / "chat" / "family.yaml").write_text("name: chat\n", encoding="utf-8")

    assert family_path(root, "chat") == toml_file(root)
    assert load_registry(root).reports["chat"].file == "families/chat/family.toml"


def test_a_toml_symlink_is_refused(tmp_path: Path) -> None:
    root = make_registry(tmp_path)
    base = root / "families" / "chat"
    target = root / "families" / "scrum-lead" / "family.toml"
    (base / "family.toml").unlink()
    (base / "family.toml").symlink_to(target)
    original = target.read_bytes()

    result = save_family(root, "chat", GOOD, "write through a link")

    assert not result.ok
    assert "symlink" in result.problem
    assert target.read_bytes() == original


def test_the_emitted_toml_document_saves(tmp_path: Path) -> None:
    """The text of `tomlout.py` is a text that this writer accepts."""
    root = make_registry(tmp_path)
    family, _ = parse_family(CHAT_FAMILY_TOML, Markup.TOML)
    assert family is not None
    edited = family.model_copy(update={"description": "the house assistant, v2"})

    result = save_family(root, "chat", to_toml(edited.model_dump(mode="json")), "from the form")

    assert result.ok, result.problem
    assert "v2" in toml_file(root).read_text(encoding="utf-8")
