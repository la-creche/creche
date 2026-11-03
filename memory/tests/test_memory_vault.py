"""`Vault.read_note` and `Vault.list_folder` stay inside the vault and follow no symlink."""

import hashlib
import os
from pathlib import Path

import pytest
from creche_memory.vault import Vault
from creche_memory.wire import (
    MAX_LIST_ENTRIES,
    MAX_TEXT_BYTES,
    FolderPath,
    ListReply,
    ReadReply,
    Refusal,
    RefusedReply,
    VaultPath,
)

NOTE = "# Printer\n\nEpson 522 ink, bought at the warehouse store.\n"


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "house" / "memories").mkdir(parents=True)
    (root / "house" / "memories" / "printer.md").write_text(NOTE, encoding="utf-8")
    (root / "house" / "notes.txt").write_text("not a note", encoding="utf-8")
    (root / ".obsidian").mkdir()
    (root / ".obsidian" / "app.md").write_text("hidden", encoding="utf-8")
    (root / "Read me.md").write_text("root note", encoding="utf-8")

    return root


def _read(root: Path, path: str) -> ReadReply | RefusedReply:
    return Vault(root).read_note(VaultPath(path))


def _list(root: Path, folder: str) -> ListReply | RefusedReply:
    return Vault(root).list_folder(FolderPath(folder))


def _refusal(reply: object) -> Refusal:
    assert isinstance(reply, RefusedReply), reply

    return reply.refused


def test_read_returns_the_text_and_its_digest(vault: Path) -> None:
    reply = _read(vault, "house/memories/printer.md")

    assert isinstance(reply, ReadReply)
    assert reply.text == NOTE
    assert reply.sha256.root == hashlib.sha256(NOTE.encode()).hexdigest()
    assert reply.bytes == len(NOTE.encode())
    assert reply.modified.utcoffset() is not None


def test_read_refuses_a_missing_note(vault: Path) -> None:
    assert _refusal(_read(vault, "house/memories/none.md")) is Refusal.NOT_FOUND
    assert _refusal(_read(vault, "house/notes.txt/a.md")) is Refusal.NOT_FOUND


def test_read_follows_no_symlink_to_a_file(vault: Path, tmp_path: Path) -> None:
    outside = tmp_path / "secret.md"
    outside.write_text("outside", encoding="utf-8")
    (vault / "house" / "link.md").symlink_to(outside)

    assert _refusal(_read(vault, "house/link.md")) is Refusal.NOT_ALLOWED_HERE


def test_read_follows_no_symlink_to_a_folder(vault: Path, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "a.md").write_text("outside", encoding="utf-8")
    (vault / "house" / "away").symlink_to(outside)

    refusal = _refusal(_read(vault, "house/away/a.md"))

    assert refusal in {Refusal.NOT_FOUND, Refusal.NOT_ALLOWED_HERE}


def test_read_refuses_a_note_over_the_cap(vault: Path) -> None:
    (vault / "big.md").write_bytes(b"x" * (MAX_TEXT_BYTES + 1))
    (vault / "full.md").write_bytes(b"x" * MAX_TEXT_BYTES)

    assert _refusal(_read(vault, "big.md")) is Refusal.TOO_LARGE
    assert isinstance(_read(vault, "full.md"), ReadReply)


def test_read_refuses_bytes_that_are_not_utf8(vault: Path) -> None:
    (vault / "latin.md").write_bytes("café".encode("latin-1"))

    assert _refusal(_read(vault, "latin.md")) is Refusal.BAD_REQUEST


def test_read_refuses_a_fifo(vault: Path) -> None:
    os.mkfifo(vault / "pipe.md")

    assert _refusal(_read(vault, "pipe.md")) is Refusal.NOT_FOUND


def test_list_returns_notes_and_folders_and_skips_the_rest(vault: Path, tmp_path: Path) -> None:
    (vault / "link.md").symlink_to(vault / "Read me.md")
    (vault / "linked").symlink_to(tmp_path)
    reply = _list(vault, "")

    assert isinstance(reply, ListReply)
    assert [note.path.root for note in reply.notes] == ["Read me.md"]
    assert [folder.root for folder in reply.folders] == ["house"]


def test_list_gives_paths_from_the_vault_root(vault: Path) -> None:
    reply = _list(vault, "house")

    assert isinstance(reply, ListReply)
    assert [note.path.root for note in reply.notes] == []
    assert [folder.root for folder in reply.folders] == ["house/memories"]


def test_list_refuses_a_missing_folder(vault: Path) -> None:
    assert _refusal(_list(vault, "garden")) is Refusal.NOT_FOUND


def test_list_refuses_a_folder_over_the_cap(vault: Path) -> None:
    crowd = vault / "crowd"
    crowd.mkdir()
    for index in range(MAX_LIST_ENTRIES + 1):
        (crowd / f"{index}.md").touch()

    assert _refusal(_list(vault, "crowd")) is Refusal.TOO_LARGE
