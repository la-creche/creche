"""Atomic writes leave a reader either the old bytes or the new ones, and
never a temp file behind."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from agent_managerd.atomic import atomic_replace_dir, atomic_write


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_write_creates_the_file_with_its_content_and_mode(tmp_path: Path) -> None:
    target = tmp_path / "creds.json"
    atomic_write(target, b'{"epoch": 1}', mode=0o600)
    assert target.read_bytes() == b'{"epoch": 1}'
    assert _mode(target) == 0o600


def test_write_creates_missing_parent_directories(tmp_path: Path) -> None:
    target = tmp_path / "families" / "chat" / "creds" / "creds.json"
    atomic_write(target, b"{}", mode=0o600)
    assert target.read_bytes() == b"{}"


def test_write_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    target = tmp_path / "status.json"
    atomic_write(target, b"{}", mode=0o644)
    leftovers = [p for p in tmp_path.iterdir() if p != target]
    assert leftovers == []


def test_write_replaces_old_content_entirely(tmp_path: Path) -> None:
    target = tmp_path / "grant.json"
    atomic_write(target, b"a much longer first version of the file", mode=0o640)
    atomic_write(target, b"short", mode=0o640)
    assert target.read_bytes() == b"short"


def test_a_failed_write_cleans_up_its_temp_file_and_keeps_the_old_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "grant.json"
    atomic_write(target, b"original", mode=0o640)

    def broken_fsync(fd: int) -> None:
        raise OSError("disk is full")

    monkeypatch.setattr(os, "fsync", broken_fsync)
    with pytest.raises(OSError, match="disk is full"):
        atomic_write(target, b"replacement", mode=0o640)

    assert target.read_bytes() == b"original"
    assert list(tmp_path.iterdir()) == [target]


def test_write_mode_excludes_group_and_other_when_asked(tmp_path: Path) -> None:
    """The family token's grant file is 0640 (contract 04 §1.1); creds.json
    is 0600 (contract 03 §12.2). Confirms `atomic_write` honours either."""
    target = tmp_path / "creds.json"
    atomic_write(target, b"{}", mode=0o600)
    assert _mode(target) & 0o077 == 0


# --- atomic_replace_dir --------------------------------------------------


def test_replace_dir_swaps_a_fresh_directory_in(tmp_path: Path) -> None:
    staging = tmp_path / "config.tmp"
    staging.mkdir()
    (staging / "instructions.md").write_text("hello", encoding="utf-8")
    target = tmp_path / "config"

    atomic_replace_dir(staging, target)

    assert (target / "instructions.md").read_text(encoding="utf-8") == "hello"
    assert not staging.exists()


def test_replace_dir_replaces_an_existing_directory(tmp_path: Path) -> None:
    target = tmp_path / "config"
    target.mkdir()
    (target / "instructions.md").write_text("old", encoding="utf-8")
    (target / "stale-file.txt").write_text("should not survive", encoding="utf-8")

    staging = tmp_path / "config.tmp"
    staging.mkdir()
    (staging / "instructions.md").write_text("new", encoding="utf-8")

    atomic_replace_dir(staging, target)

    assert (target / "instructions.md").read_text(encoding="utf-8") == "new"
    assert not (target / "stale-file.txt").exists()


def test_replace_dir_creates_missing_parents(tmp_path: Path) -> None:
    staging = tmp_path / "config.tmp"
    staging.mkdir()
    target = tmp_path / "families" / "chat" / "config"

    atomic_replace_dir(staging, target)

    assert target.is_dir()


def test_replace_dir_clears_a_stale_displaced_copy_from_a_crashed_attempt(tmp_path: Path) -> None:
    """A previous call that crashed after moving `target` aside but before
    the cleanup `rmtree` leaves `config.old/` behind. The next call must
    not trip over it."""
    target = tmp_path / "config"
    target.mkdir()
    (target / "instructions.md").write_text("current", encoding="utf-8")

    stale = tmp_path / "config.old"
    stale.mkdir()
    (stale / "instructions.md").write_text("from a crashed attempt", encoding="utf-8")

    staging = tmp_path / "config.tmp"
    staging.mkdir()
    (staging / "instructions.md").write_text("newest", encoding="utf-8")

    atomic_replace_dir(staging, target)

    assert (target / "instructions.md").read_text(encoding="utf-8") == "newest"
    assert not stale.exists()
