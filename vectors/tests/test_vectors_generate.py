"""The generator and the files on disk: what it reads, reports and writes.

Each test gives the generator a directory of its own. No test here builds
the vectors.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vectors import generate

FILES = {"index.json": "{}\n", "ids/a.json": "{}\n"}


def _check(root: Path) -> list[str]:
    return generate.stale(FILES, generate.committed(root), generate.strays(root))


def test_write_makes_the_directory_hold_the_files(tmp_path: Path) -> None:
    (tmp_path / "ids").mkdir()
    (tmp_path / "ids" / "old.json").write_text("{}\n", encoding="utf-8")

    generate.write(FILES, tmp_path)

    assert generate.committed(tmp_path) == FILES
    assert _check(tmp_path) == []


def test_a_changed_and_a_missing_file_are_reported(tmp_path: Path) -> None:
    generate.write(FILES, tmp_path)
    (tmp_path / "index.json").write_text("{ }\n", encoding="utf-8")
    (tmp_path / "ids" / "a.json").unlink()

    assert _check(tmp_path) == ["missing: ids/a.json", "differs: index.json"]


def test_a_file_of_another_kind_is_left_over(tmp_path: Path) -> None:
    """A file browser writes such a file, and `.gitignore` hides it from git."""
    generate.write(FILES, tmp_path)
    (tmp_path / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1\xff\xfe")

    assert _check(tmp_path) == ["left over: .DS_Store"]

    generate.write(FILES, tmp_path)

    assert (tmp_path / ".DS_Store").exists()


def test_a_json_file_that_is_not_utf8_differs(tmp_path: Path) -> None:
    generate.write(FILES, tmp_path)
    (tmp_path / "index.json").write_bytes(b"{}\xff\n")

    assert _check(tmp_path) == ["differs: index.json"]


def test_write_refuses_a_symbolic_link(tmp_path: Path) -> None:
    root = tmp_path / "data"
    outside = tmp_path / "outside.json"
    outside.write_text("keep\n", encoding="utf-8")
    root.mkdir()
    (root / "index.json").symlink_to(outside)

    assert "left over: index.json" in _check(root)

    with pytest.raises(ValueError, match="symbolic link"):
        generate.write(FILES, root)

    assert outside.read_text(encoding="utf-8") == "keep\n"


def test_write_refuses_a_linked_directory(tmp_path: Path) -> None:
    root = tmp_path / "data"
    outside = tmp_path / "outside"
    outside.mkdir()
    root.mkdir()
    (root / "ids").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symbolic link"):
        generate.write(FILES, root)

    assert list(outside.iterdir()) == []
