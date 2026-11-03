"""`Writer` writes only where agents may write, and never over a changed file."""

import hashlib
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest
from creche_memory.wire import (
    CreateRequest,
    Family,
    IfMatch,
    Refusal,
    RefusedReply,
    RewriteRequest,
    Slug,
    VaultPath,
    WriteReply,
)
from creche_memory.writes import Writer

NOW = datetime(2026, 11, 3, 4, 5, 6, tzinfo=UTC)
STATE = "# State\n\nSprint 12.\n"


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "house" / "scrum").mkdir(parents=True)
    (root / "house" / "scrum" / "STATE.md").write_text(STATE, encoding="utf-8")
    (root / "profile").mkdir()
    (root / "profile" / "dentist.md").write_text("David's note\n", encoding="utf-8")

    return root


def _writer(root: Path) -> Writer:
    return Writer(root, clock=lambda: NOW)


def _create(root: Path, slug: str = "dentist") -> WriteReply | RefusedReply:
    request = CreateRequest(
        v=1, op="create", caller=Family("chat"), slug=Slug(slug), title="Dentist", body="Oak St."
    )

    return _writer(root).create(request)


def _rewrite(root: Path, path: str, text: str, if_match: str) -> WriteReply | RefusedReply:
    request = RewriteRequest(
        v=1,
        op="rewrite",
        caller=Family("scrum-lead"),
        path=VaultPath(path),
        text=text,
        if_match=IfMatch.model_validate(if_match),
    )

    return _writer(root).rewrite(request)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _refusal(reply: object) -> Refusal:
    assert isinstance(reply, RefusedReply), reply

    return reply.refused


def _no_temp_files(root: Path) -> bool:
    return not any(path.name.startswith(".memory-") for path in root.rglob("*"))


def test_create_writes_an_inbox_note_with_frontmatter(vault: Path) -> None:
    reply = _create(vault)
    note = vault / "inbox" / "2026-11-03-dentist.md"
    text = note.read_text(encoding="utf-8")

    assert isinstance(reply, WriteReply)
    assert reply.path.root == "inbox/2026-11-03-dentist.md"
    assert reply.sha256.root == _digest(text)
    assert text == (
        "---\nfamily: chat\ncreated: 2026-11-03T04:05:06Z\n---\n\n# Dentist\n\nOak St.\n"
    )
    assert stat.S_IMODE(note.stat().st_mode) == 0o644
    assert stat.S_IMODE((vault / "inbox").stat().st_mode) == 0o755
    assert _no_temp_files(vault)


def test_create_refuses_a_note_that_exists(vault: Path) -> None:
    assert isinstance(_create(vault), WriteReply)
    assert _refusal(_create(vault)) is Refusal.EXISTS
    assert _no_temp_files(vault)


def test_rewrite_replaces_the_file_it_read(vault: Path) -> None:
    reply = _rewrite(vault, "house/scrum/STATE.md", "# State\n\nSprint 13.\n", _digest(STATE))

    assert isinstance(reply, WriteReply)
    assert (vault / "house" / "scrum" / "STATE.md").read_text(encoding="utf-8").endswith("13.\n")
    assert _no_temp_files(vault)


def test_rewrite_refuses_a_file_that_changed(vault: Path) -> None:
    reply = _rewrite(vault, "house/scrum/STATE.md", "x", _digest("an older text"))

    assert _refusal(reply) is Refusal.CHANGED
    assert (vault / "house" / "scrum" / "STATE.md").read_text(encoding="utf-8") == STATE


def test_rewrite_refuses_a_missing_file_with_a_digest(vault: Path) -> None:
    assert _refusal(_rewrite(vault, "house/scrum/none.md", "x", _digest("x"))) is Refusal.NOT_FOUND
    assert _refusal(_rewrite(vault, "house/ha/a/b.md", "x", _digest("x"))) is Refusal.NOT_FOUND
    assert not (vault / "house" / "ha").exists()


def test_rewrite_new_makes_the_folders_and_refuses_an_existing_file(vault: Path) -> None:
    assert isinstance(_rewrite(vault, "house/ha/reports/week-45.md", "report", "new"), WriteReply)
    assert (vault / "house" / "ha" / "reports" / "week-45.md").read_text(
        encoding="utf-8"
    ) == "report"
    assert _refusal(_rewrite(vault, "house/scrum/STATE.md", "x", "new")) is Refusal.EXISTS


@pytest.mark.parametrize(
    "path",
    ["profile/dentist.md", "house/hax/a.md", "house/notes.md", "inbox.md", "docs/creche/a.md"],
)
def test_rewrite_refuses_a_place_agents_do_not_own(vault: Path, path: str) -> None:
    assert _refusal(_rewrite(vault, path, "x", "new")) is Refusal.NOT_ALLOWED_HERE
    assert (vault / "profile" / "dentist.md").read_text(encoding="utf-8") == "David's note\n"


def test_rewrite_follows_no_symlink_out_of_the_vault(vault: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (vault / "house" / "ha").symlink_to(outside)

    refusal = _refusal(_rewrite(vault, "house/ha/a.md", "x", "new"))

    assert refusal in {Refusal.NOT_FOUND, Refusal.NOT_ALLOWED_HERE}
    assert list(outside.iterdir()) == []


def test_rewrite_follows_no_symlink_at_the_file(vault: Path, tmp_path: Path) -> None:
    target = tmp_path / "target.md"
    target.write_text("keep", encoding="utf-8")
    (vault / "house" / "scrum" / "link.md").symlink_to(target)

    refusal = _refusal(_rewrite(vault, "house/scrum/link.md", "x", _digest("keep")))

    assert refusal is Refusal.NOT_ALLOWED_HERE
    assert target.read_text(encoding="utf-8") == "keep"
