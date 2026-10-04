"""The `git` of the registry module, held against rule 18 of this suite.

A scenario of the noticeboard reads the registry back with `git`. That is a
proof only when `git` reads the repository of the root, with no config file
of a person. Each test here holds one part of that.

No test here holds `GIT_CONFIG_NOSYSTEM`. A proof needs a config file of the
system, and a test writes nothing outside its root.
"""

from __future__ import annotations

import proc_registry
import pytest
from proc_registry import RegistryError
from proc_tree import FAMILY, Tree

ORACLE = "vault-oracle"

#: A config file of a person. The first setting hides each new file from
#: `git status`, so a save that left a file outside its commit would look
#: clean. The second one names another author.
PERSON = "A Person"
PERSON_CONFIG = f"[status]\n\tshowUntrackedFiles = no\n[user]\n\tname = {PERSON}\n"


def test_git_finds_no_repository_above_the_root(tree: Tree) -> None:
    """A root in a checkout of another repository. `git` must not find that one."""
    proc_registry.write_attended(tree, FAMILY)
    proc_registry.commit_all(tree)
    inner = Tree(tree.registry_root / "inner")
    inner.registry_root.mkdir(parents=True)

    with pytest.raises(RegistryError):
        proc_registry.uncommitted(inner)


def test_no_config_of_the_shell_reaches_git(tree: Tree, monkeypatch: pytest.MonkeyPatch) -> None:
    """The shell that runs the suite names a config file. `git` does not read it."""
    config = tree.root / "person.gitconfig"
    config.write_text(PERSON_CONFIG, encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))

    _commit_then_add_a_file(tree)

    assert proc_registry.head(tree).author != PERSON
    assert proc_registry.uncommitted(tree) != ""


def test_no_config_in_the_home_of_the_root_reaches_git(tree: Tree) -> None:
    """A service of a test can write a config file in its home. `git` does not read it."""
    tree.home.mkdir(parents=True)
    (tree.home / ".gitconfig").write_text(PERSON_CONFIG, encoding="utf-8")

    _commit_then_add_a_file(tree)

    assert proc_registry.head(tree).author != PERSON
    assert proc_registry.uncommitted(tree) != ""


def _commit_then_add_a_file(tree: Tree) -> None:
    """One commit, then one family that no commit holds."""
    proc_registry.write_attended(tree, FAMILY)
    proc_registry.commit_all(tree)
    proc_registry.write_thin(tree, ORACLE)
