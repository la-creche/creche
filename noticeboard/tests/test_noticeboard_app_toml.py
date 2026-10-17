"""The edit form and the save of a TOML family, through the routes.

The fixture registry holds two TOML family files. A save writes
`family.toml`, keeps each comment of the file and changes the line of the
edit. When the patch of the file cannot be made, the save writes the text of
`tomlout.py`, and the validator of the whole registry proves that text.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from agent_family import Markup, load_registry, parse_family
from fastapi.testclient import TestClient
from noticeboard.app import build_app
from noticeboard.config import DEFAULT_PAGE_SIZE, Config
from noticeboard.familyform import Control, document_of, form_of
from noticeboard.pages import index_of
from noticeboard.security import ACCESS_HEADER, CSRF_COOKIE, CSRF_FIELD
from noticeboard.sessions import SessionReader
from noticeboard_helpers import (
    CHAT_COMMENT,
    CHAT_FAMILY_TOML,
    NOW,
    FakeAttendance,
    browser_values,
    commit_count,
    make_registry,
    make_state_root,
    write_token,
)

from noticeboard import app as app_module
from noticeboard import tomlkeep

KEY = "k" * 40
HOST = "noticeboard.example.test"
EDIT = "/families/chat/edit"


class Board:
    """The service, with a registry of TOML family files."""

    def __init__(self, client: TestClient, registry: Path) -> None:
        self.client = client
        self.registry = registry

    def get(self, path: str):
        return self.client.get(path, headers={ACCESS_HEADER: KEY})

    def post(self, path: str, body: dict[str, str]):
        token = self.client.cookies.get(CSRF_COOKIE) or ""
        headers = {
            ACCESS_HEADER: KEY,
            "Origin": f"http://{HOST}",
            "Host": HOST,
            "Content-Type": "application/x-www-form-urlencoded",
        }

        return self.client.post(
            path, data={CSRF_FIELD: token, **body}, headers=headers, follow_redirects=False
        )

    @property
    def file(self) -> Path:
        return self.registry / "families" / "chat" / "family.toml"

    def text(self) -> str:
        return self.file.read_text(encoding="utf-8")


@pytest.fixture
def board(tmp_path: Path) -> Iterator[Board]:
    state = make_state_root(tmp_path)
    registry = make_registry(tmp_path, Markup.TOML)
    config = Config(
        bind="127.0.0.1",
        port=8370,
        state_root=state,
        registry_dir=registry,
        attendance_socket=None,
        attendance_url="http://sessiond",
        page_size=DEFAULT_PAGE_SIZE,
        cookie_secure=False,
        access_key=KEY,
    )
    reader = SessionReader(transport=FakeAttendance(), token_file=write_token(state))
    app = build_app(config, reader, lambda: NOW)

    with TestClient(app, base_url=f"http://{HOST}") as client:
        yield Board(client, registry)


def posted_form(board: Board) -> dict[str, str]:
    """What a browser posts for the form of the TOML family `chat`."""
    registry = load_registry(board.registry)
    form = form_of(registry.families["chat"], index_of(registry), Markup.TOML)
    body: dict[str, str] = {}

    for one in form.fields:
        if one.locked:
            continue

        if one.control is Control.CHECKBOX:
            if one.checked:
                body[one.name] = "on"
            continue

        body[one.name] = one.value

    return body


def lines_that_moved(text: str) -> list[str]:
    held = set(CHAT_FAMILY_TOML.splitlines())

    return [line for line in text.splitlines() if line not in held]


def test_the_edit_page_of_a_toml_family_shows_toml_blocks(board: Board) -> None:
    answer = board.get(EDIT)

    assert answer.status_code == 200
    assert "TOML, including the key line" in answer.text
    assert "YAML, including the key line" not in answer.text
    assert browser_values(answer.text)["tools"] == "tools = {}\n"


def test_the_edit_page_posts_what_the_toml_form_holds(board: Board) -> None:
    values = browser_values(board.get(EDIT).text)

    assert values.pop(CSRF_FIELD) == board.client.cookies.get(CSRF_COOKIE)
    assert values.pop("subject") == ""
    assert values == posted_form(board)


def test_a_save_of_a_toml_family_changes_one_line_and_keeps_its_comments(board: Board) -> None:
    board.get(EDIT)
    before = commit_count(board.registry)
    body = posted_form(board)
    body["description"] = "the house assistant, rewritten"
    body["verb"] = "save"

    answer = board.post(EDIT, body)

    assert answer.status_code == 303
    assert commit_count(board.registry) == before + 1
    assert lines_that_moved(board.text()) == ['description = "the house assistant, rewritten"']
    assert CHAT_COMMENT in board.text()
    assert not board.file.with_name("family.yaml").exists()


def test_a_toml_save_that_changes_nothing_makes_no_commit(board: Board) -> None:
    board.get(EDIT)
    before = commit_count(board.registry)
    body = posted_form(board)
    body["verb"] = "save"

    answer = board.post(EDIT, body)

    assert answer.status_code == 303
    assert commit_count(board.registry) == before
    assert board.text() == CHAT_FAMILY_TOML


@pytest.mark.parametrize(
    "patched", [None, CHAT_FAMILY_TOML], ids=["no-patch", "a-patch-of-another-model"]
)
def test_a_patch_that_cannot_be_kept_gives_way_to_the_writer(
    board: Board, monkeypatch: pytest.MonkeyPatch, patched: str | None
) -> None:
    """The save then writes the text of `tomlout.py`. The validator of the
    whole registry reads that text before the commit."""
    monkeypatch.setattr(tomlkeep, "edited_text", lambda original, before, after: patched)
    board.get(EDIT)
    before = commit_count(board.registry)
    body = posted_form(board)
    body["description"] = "the house assistant, rewritten"
    body["verb"] = "save"
    registry = load_registry(board.registry)
    form = form_of(registry.families["chat"], index_of(registry), Markup.TOML)
    document, _ = document_of(form, body)

    answer = board.post(EDIT, body)

    family, _ = parse_family(board.text(), Markup.TOML)
    assert answer.status_code == 303
    assert commit_count(board.registry) == before + 1
    assert board.text() == document
    assert family is not None
    assert family.description == "the house assistant, rewritten"
    assert load_registry(board.registry).ok


def test_a_toml_block_with_a_key_two_times_is_refused_on_the_page(board: Board) -> None:
    board.get(EDIT)
    before = commit_count(board.registry)
    body = posted_form(board)
    typed = 'tools.kagi = "all"\ntools.kagi = "all"\n'
    body["tools"] = typed
    body["verb"] = "save"

    answer = board.post(EDIT, body)

    assert answer.status_code == 200
    assert "the tools block: the text is not TOML 1.0" in answer.text
    assert browser_values(answer.text)["tools"] == typed
    assert commit_count(board.registry) == before
    assert board.text() == CHAT_FAMILY_TOML


#: The `files` block of a reader who writes a list of tables with table lines.
FILES_WITH_TABLE_LINES = """\
[[files]]
path = "/srv/agents/vault"
mode = "ro"

[[files]]
path = "/srv/agents/vault/house/memories"
mode = "rw"
"""


def test_a_toml_block_edit_saves_and_keeps_the_other_lines(board: Board) -> None:
    board.get(EDIT)
    body = posted_form(board)
    body["files"] = FILES_WITH_TABLE_LINES
    body["verb"] = "save"

    answer = board.post(EDIT, body)

    family, _ = parse_family(board.text(), Markup.TOML)
    assert answer.status_code == 303, answer.text
    assert family is not None
    assert [one.path for one in family.files] == [
        "/srv/agents/vault",
        "/srv/agents/vault/house/memories",
    ]
    assert family.shell is False
    assert family.egress == []
    assert [line for line in board.text().splitlines() if line.startswith("[")] == []


def test_the_service_uses_the_toml_patch_for_a_toml_family(
    board: Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The YAML patch never sees a TOML file."""
    seen: list[str] = []
    monkeypatch.setattr(app_module, "edited_text", lambda original, before, after: seen.append(""))
    board.get(EDIT)
    body = posted_form(board)
    body["description"] = "the house assistant, rewritten"
    body["verb"] = "save"

    answer = board.post(EDIT, body)

    assert answer.status_code == 303
    assert seen == []
    assert CHAT_COMMENT in board.text()


def test_the_patch_takes_the_markup_of_the_form(
    board: Board, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One save has one markup: the markup of the form. The name of the file
    that the patch reads does not change it."""
    other = tmp_path / "other" / "family.yaml"
    other.parent.mkdir()
    other.write_text(board.text(), encoding="utf-8")
    monkeypatch.setattr(app_module, "family_path", lambda registry_dir, name: other)
    board.get(EDIT)
    body = posted_form(board)
    body["description"] = "the house assistant, rewritten"
    body["verb"] = "save"

    answer = board.post(EDIT, body)

    assert answer.status_code == 303
    assert lines_that_moved(board.text()) == ['description = "the house assistant, rewritten"']
    assert CHAT_COMMENT in board.text()
