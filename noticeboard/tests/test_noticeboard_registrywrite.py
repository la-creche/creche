"""A family edit as one validated git commit.

The property under test everywhere here: after a refused save, the bytes
on disk and the git history are exactly what they were before.
"""

from __future__ import annotations

import os
import stat
import threading
from pathlib import Path

import pytest
from agent_family import Registry
from agent_family.registry import FAMILIES_DIR, MCP_DIR, SKILLS_DIR
from noticeboard.registrywrite import (
    COMMIT_TRAILER,
    TEMP_SUFFIX,
    SaveResult,
    head_sha,
    save_family,
)
from noticeboard.yamlout import to_yaml
from noticeboard_helpers import CHAT_FAMILY_YAML, commit_count, git, make_registry

from noticeboard import registrywrite

GOOD = CHAT_FAMILY_YAML.replace("the house assistant", "the house assistant, rewritten")

#: A second edit of the same file, for a save that runs beside the first.
OTHER = CHAT_FAMILY_YAML.replace("the house assistant", "the house assistant, a second edit")

#: `kind` is not one of contract 01's three, so the whole registry fails.
BAD = CHAT_FAMILY_YAML.replace("kind: attended", "kind: wizard")

#: How long the first save holds the lock while the second one waits. A save
#: with no lock ends in less time than this.
HELD_S = 0.5

#: How long a test waits for a thread that must end.
JOIN_S = 60


def family_file(root: Path, name: str = "chat") -> Path:
    return root / "families" / name / "family.yaml"


def names_in(directory: Path) -> list[str]:
    return sorted(one.name for one in directory.iterdir())


def watch_the_validator(monkeypatch: pytest.MonkeyPatch, watched: Path) -> list[bytes]:
    """Record what `watched` holds each time the validator reads a registry.

    `caregiver` reads the checkout at any time, so this is what it can read
    while a save is under way.
    """
    seen: list[bytes] = []
    real = registrywrite.load_registry

    def watching(root: Path) -> Registry:
        seen.append(watched.read_bytes())
        return real(root)

    monkeypatch.setattr(registrywrite, "load_registry", watching)

    return seen


def test_a_valid_edit_makes_exactly_one_commit(tmp_path: Path) -> None:
    root = make_registry(tmp_path)
    before = commit_count(root)

    result = save_family(root, "chat", GOOD, "widen the description")

    assert result.ok
    assert result.problem == ""
    assert commit_count(root) == before + 1
    assert family_file(root).read_text(encoding="utf-8") == GOOD


def test_the_commit_carries_the_via_noticeboard_trailer(tmp_path: Path) -> None:
    root = make_registry(tmp_path)

    save_family(root, "chat", GOOD, "widen the description")

    body = git(root, "log", "-1", "--format=%B").stdout
    assert COMMIT_TRAILER in body
    assert "widen the description" in body


def test_the_commit_author_is_pinned_not_the_checkout(tmp_path: Path) -> None:
    """The checkout's own user.name must not decide who wrote this."""
    root = make_registry(tmp_path)

    save_family(root, "chat", GOOD, "widen the description")

    assert git(root, "log", "-1", "--format=%an").stdout.strip() == "noticeboard"


def test_the_commit_email_names_no_host(tmp_path: Path) -> None:
    root = make_registry(tmp_path)

    save_family(root, "chat", GOOD, "widen the description")

    assert git(root, "log", "-1", "--format=%ae").stdout.strip() == "noticeboard@localhost"


def test_the_commit_is_scoped_to_one_family(tmp_path: Path) -> None:
    """A stray edit beside the save must not ride along."""
    root = make_registry(tmp_path)
    stray = root / "families" / "scrum-lead" / "family.yaml"
    stray.write_text(stray.read_text(encoding="utf-8") + "# edited by hand\n", encoding="utf-8")

    save_family(root, "chat", GOOD, "widen the description")

    changed = git(root, "show", "--name-only", "--format=", "HEAD").stdout.split()
    assert changed == ["families/chat/family.yaml"]


def test_an_invalid_edit_writes_nothing_and_commits_nothing(tmp_path: Path) -> None:
    root = make_registry(tmp_path)
    before = commit_count(root)
    original = family_file(root).read_bytes()

    result = save_family(root, "chat", BAD, "break it")

    assert not result.ok
    assert result.errors
    assert commit_count(root) == before
    assert family_file(root).read_bytes() == original


def test_an_invalid_edit_never_reaches_the_family_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The validator reads the new text, and the checkout keeps the old one."""
    root = make_registry(tmp_path)
    original = family_file(root).read_bytes()
    seen = watch_the_validator(monkeypatch, family_file(root))

    result = save_family(root, "chat", BAD, "break it")

    assert not result.ok
    assert result.errors
    assert seen == [original]


def test_a_valid_edit_reaches_the_family_file_after_it_validates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_registry(tmp_path)
    original = family_file(root).read_bytes()
    seen = watch_the_validator(monkeypatch, family_file(root))

    result = save_family(root, "chat", GOOD, "widen the description")

    assert result.ok
    assert seen == [original]
    assert family_file(root).read_text(encoding="utf-8") == GOOD


def test_the_validator_gets_only_the_directories_it_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The copy holds the three directories of a registry and no other path
    of the checkout."""
    root = make_registry(tmp_path)
    (root / "notes").mkdir()
    (root / "notes" / "plan.txt").write_text("not a registry file\n", encoding="utf-8")
    (root / "README").write_text("not a registry file\n", encoding="utf-8")
    seen: list[list[str]] = []
    real = registrywrite.load_registry

    def watching(copy: Path) -> Registry:
        seen.append(names_in(copy))
        return real(copy)

    monkeypatch.setattr(registrywrite, "load_registry", watching)

    result = save_family(root, "chat", GOOD, "widen the description")

    assert result.ok, result.problem
    assert seen == [["families", "mcp", "skills"]]
    assert set(registrywrite.VALIDATED_DIRS) == {FAMILIES_DIR, MCP_DIR, SKILLS_DIR}


def test_a_file_that_no_copy_can_take_does_not_refuse_the_save(tmp_path: Path) -> None:
    """A path that the validator does not read is not a reason to refuse."""
    root = make_registry(tmp_path)
    os.mkfifo(root / "pipe")

    result = save_family(root, "chat", GOOD, "widen the description")

    assert result.ok, result.problem
    assert family_file(root).read_text(encoding="utf-8") == GOOD


def test_a_save_puts_a_new_file_in_place_and_keeps_its_mode(tmp_path: Path) -> None:
    """A rename swaps the name to a whole new file. A write in place would
    leave a part of a file for a reader to see."""
    root = make_registry(tmp_path)
    family_file(root).chmod(0o640)
    before = family_file(root).stat()

    result = save_family(root, "chat", GOOD, "widen the description")

    after = family_file(root).stat()
    assert result.ok
    assert after.st_ino != before.st_ino
    assert stat.S_IMODE(after.st_mode) == 0o640
    assert names_in(root / "families" / "chat") == ["family.yaml", "instructions.md"]


def test_a_save_of_a_new_family_leaves_one_file(tmp_path: Path) -> None:
    root = make_registry(tmp_path)
    text = GOOD.replace("name: chat", "name: second")

    result = save_family(root, "second", text, "add a family")

    assert result.ok, result.problem
    assert names_in(root / "families" / "second") == ["family.yaml"]
    assert family_file(root, "second").read_text(encoding="utf-8") == text


def test_a_temporary_file_of_a_killed_save_does_not_reach_the_commit(tmp_path: Path) -> None:
    """A save that the system killed between its write and its rename leaves
    its temporary file. The next save must not commit that file."""
    root = make_registry(tmp_path)
    stale = root / "families" / "chat" / f".family.yaml.0123456789abcdef{TEMP_SUFFIX}"
    stale.write_text(BAD, encoding="utf-8")

    result = save_family(root, "chat", GOOD, "widen the description")

    changed = git(root, "show", "--name-only", "--format=", "HEAD").stdout.split()
    assert result.ok
    assert changed == ["families/chat/family.yaml"]
    assert names_in(root / "families" / "chat") == ["family.yaml", "instructions.md"]


def test_an_invalid_edit_leaves_the_tree_clean(tmp_path: Path) -> None:
    """Restoring the bytes is not enough if the index still holds them."""
    root = make_registry(tmp_path)

    save_family(root, "chat", BAD, "break it")

    assert git(root, "status", "--porcelain").stdout.strip() == ""


def test_a_neighbour_broken_by_this_edit_refuses_the_save(tmp_path: Path) -> None:
    """Validation covers the whole registry, not the edited file alone."""
    root = make_registry(tmp_path)
    scrum = root / "families" / "scrum-lead" / "family.yaml"
    scrum.write_text(
        scrum.read_text(encoding="utf-8") + 'delegates:\n  - "chat"\n  - "gone"\n',
        encoding="utf-8",
    )
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "name a family that does not exist")
    before = commit_count(root)

    result = save_family(root, "chat", GOOD, "widen the description")

    assert not result.ok
    assert commit_count(root) == before
    assert family_file(root).read_text(encoding="utf-8") == CHAT_FAMILY_YAML


def test_a_failed_save_of_a_new_family_removes_the_directory(tmp_path: Path) -> None:
    root = make_registry(tmp_path)

    result = save_family(root, "wizard", BAD.replace("name: chat", "name: wizard"), "add it")

    assert not result.ok
    assert not (root / "families" / "wizard").exists()


def test_a_failed_save_leaves_the_whole_directory_as_it_was(tmp_path: Path) -> None:
    """The snapshot covers the directory, not two known paths."""
    root = make_registry(tmp_path)
    base = root / "families" / "chat"
    before = {one.name: one.read_bytes() for one in base.iterdir() if one.is_file()}

    save_family(root, "chat", BAD, "break it")

    after = {one.name: one.read_bytes() for one in base.iterdir() if one.is_file()}
    assert after == before
    assert "instructions.md" in after


def committed(root: Path, name: str = "chat") -> str:
    """The family file as the newest commit holds it."""
    return git(root, "show", f"HEAD:families/{name}/family.yaml").stdout


def test_a_save_that_cannot_take_the_lock_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two saves at one time. The second one starts between the write and
    the commit of the first, and its wait ends before the first save does.
    It is refused, and the first save is one commit of its own text."""
    root = make_registry(tmp_path)
    before = commit_count(root)
    real = registrywrite._commit
    second: list[SaveResult] = []

    def commit_after_a_second_save(registry_dir: Path, name: str, subject: str) -> str:
        monkeypatch.setattr(registrywrite, "_commit", real)
        monkeypatch.setattr(registrywrite, "LOCK_WAIT_S", 0.0)
        second.append(save_family(root, "chat", OTHER, "the second edit"))

        return real(registry_dir, name, subject)

    monkeypatch.setattr(registrywrite, "_commit", commit_after_a_second_save)

    first = save_family(root, "chat", GOOD, "the first edit")

    assert first.ok, first.problem
    assert not second[0].ok
    assert second[0].problem == registrywrite.BUSY
    assert commit_count(root) == before + 1
    assert family_file(root).read_text(encoding="utf-8") == GOOD
    assert committed(root) == GOOD
    assert git(root, "status", "--porcelain").stdout == ""


def test_a_save_waits_for_the_save_before_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second save starts while the first one holds the lock. It waits,
    and then it is the second commit."""
    root = make_registry(tmp_path)
    before = commit_count(root)
    real = registrywrite._commit
    second: list[SaveResult] = []
    worker = threading.Thread(
        target=lambda: second.append(save_family(root, "chat", OTHER, "the second edit"))
    )

    def commit_while_a_second_save_waits(registry_dir: Path, name: str, subject: str) -> str:
        monkeypatch.setattr(registrywrite, "_commit", real)
        worker.start()
        worker.join(HELD_S)

        return real(registry_dir, name, subject)

    monkeypatch.setattr(registrywrite, "_commit", commit_while_a_second_save_waits)

    first = save_family(root, "chat", GOOD, "the first edit")
    worker.join(JOIN_S)

    subjects = git(root, "log", "-2", "--format=%s").stdout.splitlines()
    assert first.ok, first.problem
    assert second[0].ok, second[0].problem
    assert commit_count(root) == before + 2
    assert subjects == ["the second edit", "the first edit"]
    assert family_file(root).read_text(encoding="utf-8") == OTHER
    assert committed(root) == OTHER
    assert git(root, "status", "--porcelain").stdout == ""


def test_saves_at_one_time_leave_no_edit_without_a_commit(tmp_path: Path) -> None:
    """`caregiver` converges on the file of the checkout. After each save
    ended, that file is the file of the newest commit."""
    root = make_registry(tmp_path)
    before = commit_count(root)
    texts = [
        CHAT_FAMILY_YAML.replace("the house assistant", f"the house assistant, edit {number}")
        for number in range(4)
    ]
    start = threading.Barrier(len(texts))
    results: dict[int, SaveResult] = {}

    def one(number: int) -> None:
        start.wait(JOIN_S)
        results[number] = save_family(root, "chat", texts[number], f"edit {number}")

    workers = [threading.Thread(target=one, args=(number,)) for number in range(len(texts))]

    for worker in workers:
        worker.start()

    for worker in workers:
        worker.join(JOIN_S)

    assert [results[number].problem for number in range(len(texts))] == [""] * len(texts)
    assert commit_count(root) == before + len(texts)
    assert family_file(root).read_text(encoding="utf-8") in texts
    assert committed(root) == family_file(root).read_text(encoding="utf-8")
    assert git(root, "status", "--porcelain").stdout == ""


@pytest.mark.parametrize("text", [BAD, GOOD], ids=["a-refused-save", "a-save"])
def test_a_save_that_ended_releases_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    """The next save does not wait."""
    root = make_registry(tmp_path)
    monkeypatch.setattr(registrywrite, "LOCK_WAIT_S", 0.0)
    save_family(root, "chat", text, "the first edit")

    result = save_family(root, "chat", OTHER, "the second edit")

    assert result.ok, result.problem
    assert committed(root) == OTHER


def test_a_registry_that_is_not_there_refuses_the_save(tmp_path: Path) -> None:
    """The save cannot take the lock on a directory that is not there. It
    makes no directory."""
    root = tmp_path / "no-registry"

    result = save_family(root, "chat", GOOD, "widen the description")

    assert not result.ok
    assert "cannot lock the registry" in result.problem
    assert not root.exists()


def test_a_registry_that_is_no_directory_refuses_the_save(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    root.write_text("not a checkout\n", encoding="utf-8")

    result = save_family(root, "chat", GOOD, "widen the description")

    assert not result.ok
    assert "cannot lock the registry" in result.problem
    assert root.read_text(encoding="utf-8") == "not a checkout\n"


def test_a_concurrent_git_lock_rolls_the_save_back(tmp_path: Path) -> None:
    """The lock of a save does not stop another program that writes the
    checkout. Git's index.lock does: the commit fails, and the save restores."""
    root = make_registry(tmp_path)
    (root / ".git" / "index.lock").write_text("", encoding="utf-8")
    original = family_file(root).read_bytes()
    before = head_sha(root)

    result = save_family(root, "chat", GOOD, "widen the description")

    assert not result.ok
    assert "git" in result.problem
    assert family_file(root).read_bytes() == original
    assert head_sha(root) == before
    assert names_in(root / "families" / "chat") == ["family.yaml", "instructions.md"]


def test_a_failed_commit_puts_the_old_file_back_with_a_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`caregiver` reads the checkout at any time. The restore swaps the
    name to a whole file, as the save does, and writes no file in place."""
    root = make_registry(tmp_path)
    original = family_file(root).read_bytes()
    written: list[int] = []

    def failing(registry_dir: Path, name: str, subject: str) -> str:
        written.append(family_file(root).stat().st_ino)
        return "git commit failed: a commit that this test refuses"

    monkeypatch.setattr(registrywrite, "_commit", failing)

    result = save_family(root, "chat", GOOD, "widen the description")

    assert not result.ok
    assert len(written) == 1
    assert family_file(root).stat().st_ino != written[0]
    assert family_file(root).read_bytes() == original
    assert names_in(root / "families" / "chat") == ["family.yaml", "instructions.md"]


def test_a_rename_that_fails_leaves_no_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The step that puts a file in place removes its temporary file when
    the rename fails, and raises the failure again."""
    root = make_registry(tmp_path)
    original = family_file(root).read_bytes()

    def refusing(self: Path, target: Path) -> Path:
        raise OSError("a rename that this test refuses")

    monkeypatch.setattr(Path, "replace", refusing)

    with pytest.raises(OSError, match="a rename that this test refuses"):
        registrywrite._replace(family_file(root), GOOD.encode("utf-8"))

    assert family_file(root).read_bytes() == original
    assert names_in(root / "families" / "chat") == ["family.yaml", "instructions.md"]


def test_an_unchanged_save_commits_nothing_and_says_so(tmp_path: Path) -> None:
    root = make_registry(tmp_path)
    before = commit_count(root)

    result = save_family(root, "chat", CHAT_FAMILY_YAML, "no change")

    assert result.ok
    assert result.unchanged
    assert commit_count(root) == before


def test_a_name_that_is_not_a_family_name_is_refused(tmp_path: Path) -> None:
    root = make_registry(tmp_path)

    result = save_family(root, "../../etc", GOOD, "escape")

    assert not result.ok
    assert "not a family name" in result.problem


def test_a_family_name_with_a_trailing_newline_is_refused(tmp_path: Path) -> None:
    """A `$` also matches before a final newline. `\\Z` does not."""
    root = make_registry(tmp_path)
    before = commit_count(root)

    result = save_family(root, "chat\n", GOOD, "newline")

    assert not result.ok
    assert "not a family name" in result.problem
    assert commit_count(root) == before


def test_a_family_directory_pointing_out_of_the_registry_is_refused(tmp_path: Path) -> None:
    root = make_registry(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / "families" / "sneaky").symlink_to(elsewhere, target_is_directory=True)

    result = save_family(root, "sneaky", GOOD, "write through a link")

    assert not result.ok
    assert "outside the registry" in result.problem
    assert not (elsewhere / "family.yaml").exists()


def test_a_symlink_pointing_back_inside_the_registry_is_refused(tmp_path: Path) -> None:
    """`resolve()` alone accepts this one, so the link check has to exist."""
    root = make_registry(tmp_path)
    (root / "families" / "sneaky").symlink_to(root / "families" / "chat", True)
    original = family_file(root).read_bytes()

    result = save_family(root, "sneaky", GOOD, "write through a link")

    assert not result.ok
    assert "symlink" in result.problem
    assert family_file(root).read_bytes() == original


def test_a_families_link_into_the_checkout_refuses_the_save(tmp_path: Path) -> None:
    """The validator reads a copy, and a copied link still points at the
    checkout. The new text must not reach the checkout through it."""
    root = make_registry(tmp_path)
    held = root / "held"
    (root / "families").rename(held)
    (root / "families").symlink_to(held, target_is_directory=True)
    original = (held / "chat" / "family.yaml").read_bytes()

    result = save_family(root, "chat", BAD, "break it")

    assert not result.ok
    assert "resolves outside the copy" in result.problem
    assert (held / "chat" / "family.yaml").read_bytes() == original


def test_an_oversized_document_is_refused_before_any_write(tmp_path: Path) -> None:
    root = make_registry(tmp_path)
    original = family_file(root).read_bytes()

    result = save_family(root, "chat", "x" * (300 * 1024), "flood it")

    assert not result.ok
    assert "over" in result.problem
    assert family_file(root).read_bytes() == original


def test_the_emitted_document_saves(tmp_path: Path) -> None:
    """The form's output must be text this writer accepts."""
    from agent_family import parse_family

    root = make_registry(tmp_path)
    family, _ = parse_family(CHAT_FAMILY_YAML)
    assert family is not None
    edited = family.model_copy(update={"description": "the house assistant, v2"})

    result = save_family(root, "chat", to_yaml(edited.model_dump(mode="json")), "from the form")

    assert result.ok, result.problem
    assert "v2" in family_file(root).read_text(encoding="utf-8")
