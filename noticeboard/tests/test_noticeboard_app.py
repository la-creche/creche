"""Every page, the perimeter and one save.

Every source is a fixture: a state root on disk, a git registry in a tmp
dir, and a fake `attendance`. No host, no socket, no live service.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from agent_family import load_registry
from fastapi.testclient import TestClient
from noticeboard.app import build_app
from noticeboard.config import DEFAULT_PAGE_SIZE, Config
from noticeboard.familyform import Control, form_of
from noticeboard.pages import index_of
from noticeboard.security import ACCESS_HEADER, CSRF_COOKIE, CSRF_FIELD
from noticeboard.sessions import SessionReader
from noticeboard_helpers import (
    CHAT_COMMENT,
    CHAT_FAMILY_YAML,
    NOW,
    FakeAttendance,
    commit_count,
    journal_line,
    make_registry,
    make_state_root,
    ndjson,
    session_doc,
    turn_doc,
    write_token,
)

KEY = "k" * 40
CHAT = "chat"
OWUI = "owui-3f2a9c41-77b0-4a1e-9a4c-1d0e5f8b2c33"
HOST = "noticeboard.example.test"

#: A text that holds one half of a surrogate pair. JSON writes it as an escape.
HALF_PAIR = "a\ud800b"

#: How deep the `loc` of one issue nests in a test. One supported Python reads
#: this depth, and the others refuse it.
ISSUE_NESTING = 100_000

#: The largest count that the interpreter writes as text, and so the largest
#: that the JSON reader keeps.
LONGEST_COUNT = int("9" * 4300)


class Harness:
    def __init__(self, client: TestClient, config: Config, fake: FakeAttendance) -> None:
        self.client = client
        self.config = config
        self.fake = fake

    def get(self, path: str, key: str = KEY):
        return self.client.get(path, headers={ACCESS_HEADER: key})

    def post(self, path: str, body: dict[str, str], **extra: Any):
        token = self.client.cookies.get(CSRF_COOKIE) or ""
        headers = {
            ACCESS_HEADER: KEY,
            "Origin": f"http://{HOST}",
            "Host": HOST,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        headers.update(extra.pop("headers", {}))

        return self.client.post(
            path, data={CSRF_FIELD: token, **body}, headers=headers, follow_redirects=False
        )


@pytest.fixture
def board(tmp_path: Path) -> Iterator[Harness]:
    state = make_state_root(tmp_path)
    registry = make_registry(tmp_path)
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
    fake = FakeAttendance()
    fake.answer("/v1/sessions", {"sessions": [session_doc()], "next_cursor": None})
    detail = session_doc()
    detail["turns"] = [turn_doc()]
    fake.answer(f"/v1/sessions/{CHAT}/{OWUI}", detail)
    fake.answer(
        f"/v1/sessions/{CHAT}/{OWUI}/events",
        ndjson(
            [
                journal_line(41, "turn_started", {"prompt": "when is the boiler due?"}, "t1"),
                journal_line(
                    42,
                    "pi_event",
                    {
                        "type": "message_update",
                        "assistantMessageEvent": {"type": "text_delta", "delta": "In March."},
                    },
                    "t1",
                ),
                journal_line(43, "turn_settled", {"usage": {}}, "t1"),
            ]
        ),
    )
    reader = SessionReader(transport=fake, token_file=write_token(state))
    # Staleness is a comparison against a clock, so the clock is pinned
    # to the moment the fixtures were stamped from.
    app = build_app(config, reader, lambda: NOW)

    with TestClient(app, base_url=f"http://{HOST}") as client:
        yield Harness(client, config, fake)


def test_healthz_needs_no_key(board: Harness) -> None:
    """A prober must not be probing the proxy's sign-in page."""
    answer = board.client.get("/healthz")

    assert answer.status_code == 200
    assert answer.json() == {"ok": True}


def test_a_page_without_the_key_is_refused(board: Harness) -> None:
    answer = board.client.get("/")

    assert answer.status_code == 403
    assert answer.json()["error"] == "no_key"


def test_a_page_with_the_wrong_key_is_refused_without_naming_it(board: Harness) -> None:
    answer = board.get("/", key="wrong")

    assert answer.status_code == 403
    assert "wrong" not in answer.text


def test_styling_is_reachable_without_the_key(board: Harness) -> None:
    """A proxy fetches assets outside its header-injecting block."""
    answer = board.client.get("/static/noticeboard.css")

    assert answer.status_code == 200


def test_the_home_page_shows_a_row_per_family(board: Harness) -> None:
    answer = board.get("/")

    assert answer.status_code == 200
    assert "chat" in answer.text
    assert "scrum-lead" in answer.text
    assert "in sync" in answer.text


def test_spend_appears_once_per_family_and_never_beside_a_sandbox(board: Harness) -> None:
    """Contract 05 §7 rule 1: the budget belongs to the family key."""
    home = board.get("/").text
    family = board.get("/families/chat").text

    assert home.count("$3.42") == 1
    assert home.count("$1.11") == 1
    # The family page lists sandboxes and names no money at all, because
    # a sandbox has no budget to name.
    assert "$" not in family


def test_a_stale_status_document_is_flagged(board: Harness, tmp_path: Path) -> None:
    from noticeboard_helpers import status_doc, write_json

    write_json(
        board.config.families_dir / "chat" / "status.json",
        status_doc(age_s=600.0),
    )

    answer = board.get("/")

    assert "stale" in answer.text
    assert "past the 90s limit" in answer.text


def test_a_malformed_state_file_renders_a_report_not_a_stack_trace(board: Harness) -> None:
    (board.config.families_dir / "chat" / "status.json").write_text("{ broken", encoding="utf-8")

    answer = board.get("/")

    assert answer.status_code == 200
    assert "not JSON" in answer.text
    assert "Traceback" not in answer.text


def test_a_text_with_half_a_surrogate_pair_renders_as_its_escape(board: Harness) -> None:
    """JSON can escape one half of a surrogate pair, and UTF-8 has no form
    for it. The page shows the escape."""
    from noticeboard_helpers import audit_line, status_doc, write_audit_day, write_json

    write_json(board.config.families_dir / "chat" / "status.json", status_doc(kind=HALF_PAIR))
    write_audit_day(board.config.audit_dir, "2026-09-19", [audit_line(args={"query": HALF_PAIR})])

    home = board.get("/")
    audit = board.get("/audit")

    assert home.status_code == 200
    assert "a\\ud800b" in home.text
    assert audit.status_code == 200
    assert "a\\ud800b" in audit.text


def test_an_exception_nobody_predicted_answers_the_refusal_body(
    board: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The answer holds one word and no text of the exception."""
    from noticeboard import pages

    def broken(*args: object) -> None:
        raise RuntimeError("a detail that belongs in the log")

    monkeypatch.setattr(pages, "home", broken)

    with TestClient(board.client.app, raise_server_exceptions=False) as client:
        answer = client.get("/", headers={ACCESS_HEADER: KEY})

    assert answer.status_code == 500
    assert answer.json() == {"ok": False, "error": "internal"}
    assert "detail" not in answer.text


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 0000 directory anyway")
def test_a_families_directory_that_cannot_be_listed_says_so(board: Harness) -> None:
    """ "No families" and "cannot look" are different sentences."""
    families = board.config.families_dir
    families.chmod(0o000)

    try:
        answer = board.get("/")
    finally:
        families.chmod(0o755)

    assert answer.status_code == 200
    assert "cannot list the families directory" in answer.text


def test_a_missing_state_file_renders_a_report(board: Harness) -> None:
    (board.config.families_dir / "chat" / "status.json").unlink()

    answer = board.get("/families/chat")

    assert answer.status_code == 200
    assert "missing" in answer.text


def test_an_issue_field_that_is_no_text_still_renders(board: Harness, tmp_path: Path) -> None:
    """An issue field is text in contract 01 §7. The page shows a marker for
    another value and never the form of that value."""
    from noticeboard_helpers import status_doc, write_json

    report = tmp_path / "validation.json"
    nested = "[" * ISSUE_NESTING + "]" * ISSUE_NESTING
    report.write_text(f'{{"issues": [{{"loc": {nested}, "msg": "unknown"}}]}}', encoding="utf-8")
    validation = {"ok": False, "error_count": 1, "report_path": str(report)}
    write_json(
        board.config.families_dir / "chat" / "status.json",
        status_doc(state="invalid", validation=validation),
    )

    answer = board.get("/families/chat")

    assert answer.status_code == 200
    assert "[[" not in answer.text


def test_the_family_page_lists_sandboxes_and_sessions(board: Harness) -> None:
    answer = board.get("/families/chat")

    assert answer.status_code == 200
    assert "chat-s3" in answer.text
    assert "Boiler service date" in answer.text


def test_the_outcomes_of_a_family_come_from_its_own_directory(board: Harness) -> None:
    """A status document also names a family. That text is input from
    another process, and the page takes the directory from the route."""
    from noticeboard_helpers import status_doc, write_json

    write_json(
        board.config.families_dir / "scrum-lead" / "status.json",
        status_doc(family="chat", kind="autonomous"),
    )

    answer = board.get("/families/scrum-lead")

    assert answer.status_code == 200
    assert "01JBQ80M4F7S2YQ1VZK6W3TDEN" in answer.text


def test_the_session_page_shows_the_transcript_and_the_turns(board: Harness) -> None:
    answer = board.get(f"/sessions/{CHAT}/{OWUI}")

    assert answer.status_code == 200
    assert "when is the boiler due?" in answer.text
    assert "In March." in answer.text
    assert "settled" in answer.text


def test_a_dead_attendance_still_renders_the_session_page(board: Harness) -> None:
    board.fake.fault = "cannot reach attendance: ConnectError"

    answer = board.get(f"/sessions/{CHAT}/{OWUI}")

    assert answer.status_code == 200
    assert "cannot reach attendance" in answer.text


def test_a_token_sum_with_no_text_form_shows_as_unknown(board: Harness) -> None:
    """The page shows `unknown` for a sum that the interpreter cannot write
    as text."""
    usage = dict.fromkeys(("input", "output", "cache_read", "cache_write"), LONGEST_COUNT)
    detail = session_doc()
    detail["turns"] = [turn_doc(usage=usage)]
    board.fake.answer(f"/v1/sessions/{CHAT}/{OWUI}", detail)

    answer = board.get(f"/sessions/{CHAT}/{OWUI}")

    assert answer.status_code == 200
    assert "<td>unknown" in answer.text


def test_the_audit_page_says_it_shows_full_arguments(board: Harness) -> None:
    answer = board.get("/audit")

    assert "full tool arguments" in answer.text


def test_an_audit_filter_narrows_the_page(board: Harness) -> None:
    from noticeboard_helpers import audit_line, write_audit_day

    write_audit_day(
        board.config.audit_dir,
        "2026-09-18",
        [audit_line(tool="ha__ha_call", decision="deny", reason="not_granted")],
    )

    every = board.get("/audit").text
    denied = board.get("/audit?decision=deny").text

    assert "kagi__kagi_search_fetch" in every
    assert "ha__ha_call" in denied
    assert "kagi__kagi_search_fetch" not in denied


def test_the_edit_form_renders_from_the_schema(board: Harness) -> None:
    answer = board.get("/families/chat/edit")

    assert answer.status_code == 200
    assert 'name="description"' in answer.text
    assert 'name="model.router"' in answer.text


def test_a_locked_field_is_disabled_with_the_rule_as_its_label(board: Harness) -> None:
    answer = board.get("/families/chat/edit").text

    assert "disabled" in answer
    # The rule's own sentence, escaped by the template as everything is.
    assert "is autonomous only; this family is" in answer


def test_a_post_without_a_csrf_token_is_refused(board: Harness) -> None:
    answer = board.client.post(
        "/families/chat/edit",
        data={"verb": "save"},
        headers={ACCESS_HEADER: KEY, "Origin": f"http://{HOST}"},
    )

    assert answer.status_code == 403
    assert answer.json()["error"] in ("no_token", "bad_token")


def test_a_post_from_a_foreign_origin_is_refused(board: Harness) -> None:
    board.get("/families/chat/edit")

    answer = board.post("/families/chat/edit", {"verb": "save"}, headers={"Origin": "http://evil"})

    assert answer.status_code == 403
    assert answer.json()["error"] == "foreign_origin"


def posted_form(board: Harness) -> dict[str, str]:
    """Rebuild what the browser would post from the rendered form."""
    registry = load_registry(board.config.registry_dir)
    family = registry.families["chat"]
    body: dict[str, str] = {}

    for one in form_of(family, index_of(registry)).fields:
        if one.locked:
            continue

        if one.control is Control.CHECKBOX:
            if one.checked:
                body[one.name] = "on"
            continue

        body[one.name] = one.value

    return body


def test_a_valid_edit_makes_exactly_one_commit(board: Harness) -> None:
    board.get("/families/chat/edit")
    before = commit_count(board.config.registry_dir)
    body = posted_form(board)
    body["description"] = "the house assistant, rewritten"
    body["verb"] = "save"
    body["subject"] = "widen the description"

    answer = board.post("/families/chat/edit", body)

    assert answer.status_code == 303
    assert commit_count(board.config.registry_dir) == before + 1


def test_a_save_keeps_the_files_comments(board: Harness) -> None:
    """The file explains each grant where it sits, and regenerating the
    document from the model would drop every line of that on the first save."""
    board.get("/families/chat/edit")
    body = posted_form(board)
    body["description"] = "the house assistant, rewritten"
    body["verb"] = "save"

    board.post("/families/chat/edit", body)

    saved = (board.config.registry_dir / "families/chat/family.yaml").read_text(encoding="utf-8")
    assert CHAT_COMMENT in saved
    assert "the house assistant, rewritten" in saved
    # The style the author wrote, on a line the edit never touched.
    assert "- { path: /srv/agents/vault, mode: ro }" in saved


@pytest.mark.parametrize(
    "patched", [CHAT_FAMILY_YAML, "name: chat\n{}\n"], ids=["another-model", "no-model"]
)
def test_a_patch_that_does_not_read_as_the_edit_gives_way_to_the_emitter(
    board: Harness, monkeypatch: pytest.MonkeyPatch, patched: str
) -> None:
    """The patched text goes back through the reader. A text that reads as
    another model, or as none, must not be what the save writes."""
    from agent_family import parse_family

    from noticeboard import app as app_module

    monkeypatch.setattr(app_module, "edited_text", lambda original, before, after: patched)
    board.get("/families/chat/edit")
    before = commit_count(board.config.registry_dir)
    body = posted_form(board)
    body["description"] = "the house assistant, rewritten"
    body["verb"] = "save"

    answer = board.post("/families/chat/edit", body)

    saved = (board.config.registry_dir / "families/chat/family.yaml").read_text(encoding="utf-8")
    family, _ = parse_family(saved)
    assert answer.status_code == 303
    assert commit_count(board.config.registry_dir) == before + 1
    assert family is not None
    assert family.description == "the house assistant, rewritten"


def test_a_save_that_changes_nothing_makes_no_commit(board: Harness) -> None:
    """The form posts every field back, so a save with no edit must not
    rewrite the whole file from the model and commit the difference. The
    registry's git log is the grant audit trail."""
    board.get("/families/chat/edit")
    before = commit_count(board.config.registry_dir)
    original = (board.config.registry_dir / "families/chat/family.yaml").read_bytes()
    body = posted_form(board)
    body["verb"] = "save"

    answer = board.post("/families/chat/edit", body)

    assert answer.status_code == 303
    assert commit_count(board.config.registry_dir) == before
    assert (board.config.registry_dir / "families/chat/family.yaml").read_bytes() == original


def test_an_invalid_edit_writes_nothing_and_reports(board: Harness) -> None:
    board.get("/families/chat/edit")
    before = commit_count(board.config.registry_dir)
    original = (board.config.registry_dir / "families/chat/family.yaml").read_bytes()
    body = posted_form(board)
    body["model.budget_usd_per_day"] = "-4"
    body["verb"] = "save"

    answer = board.post("/families/chat/edit", body)

    assert answer.status_code == 200
    assert "refused" in answer.text
    assert commit_count(board.config.registry_dir) == before
    assert (board.config.registry_dir / "families/chat/family.yaml").read_bytes() == original


def test_a_preview_says_live_or_replace_and_writes_nothing(board: Harness) -> None:
    board.get("/families/chat/edit")
    before = commit_count(board.config.registry_dir)
    body = posted_form(board)
    body["sandbox.cpus"] = "6"
    body["verb"] = "preview"

    answer = board.post("/families/chat/edit", body)

    assert answer.status_code == 200
    assert "replaces the sandbox" in answer.text
    assert "sandbox.cpus" in answer.text
    assert commit_count(board.config.registry_dir) == before


def test_a_live_only_preview_says_the_sandbox_keeps_running(board: Harness) -> None:
    board.get("/families/chat/edit")
    body = posted_form(board)
    body["description"] = "the house assistant, again"
    body["verb"] = "preview"

    answer = board.post("/families/chat/edit", body)

    assert "lands live" in answer.text


#: Path segments that are not a family name (contract 01 §2), as a URL holds them.
NOT_A_FAMILY = ("Chat", "c", "chat_1", "chat%20", "%2E%2E", "caf%C3%A9", "a" * 32)

#: Path segments that are not a session id (contract 02 §2), as a URL holds them.
NOT_A_SESSION = ("-x", "%2Ehidden", "a%20b", "a%3Ab", "%C3%A4", "a" * 129)


@pytest.mark.parametrize("name", NOT_A_FAMILY)
def test_a_family_page_answers_404_for_a_name_that_is_no_family_name(
    board: Harness, name: str
) -> None:
    for path in (f"/families/{name}", f"/families/{name}/edit"):
        answer = board.get(path)

        assert answer.status_code == 404
        assert answer.json() == {"detail": "Not Found"}

    # Nothing asked `attendance` for the sessions of such a name.
    assert board.fake.calls == []


@pytest.mark.parametrize("name", NOT_A_FAMILY)
def test_a_save_answers_404_for_a_name_that_is_no_family_name(board: Harness, name: str) -> None:
    board.get("/families/chat/edit")
    before = commit_count(board.config.registry_dir)

    answer = board.post(f"/families/{name}/edit", {"verb": "save"})

    assert answer.status_code == 404
    assert commit_count(board.config.registry_dir) == before


@pytest.mark.parametrize("family", NOT_A_FAMILY)
def test_a_session_page_answers_404_for_a_family_that_is_no_family_name(
    board: Harness, family: str
) -> None:
    answer = board.get(f"/sessions/{family}/{OWUI}")

    assert answer.status_code == 404
    assert board.fake.calls == []


@pytest.mark.parametrize("session", NOT_A_SESSION)
def test_a_session_page_answers_404_for_a_session_that_is_no_session_id(
    board: Harness, session: str
) -> None:
    answer = board.get(f"/sessions/{CHAT}/{session}")

    assert answer.status_code == 404
    assert board.fake.calls == []


def test_a_family_name_of_the_longest_form_reaches_its_page(board: Harness) -> None:
    """31 characters is a family name. The page then says what it cannot read."""
    answer = board.get("/families/" + "a" * 31)

    assert answer.status_code == 200
    assert "status.json is missing" in answer.text


def test_a_session_id_of_the_longest_form_reaches_its_page(board: Harness) -> None:
    session = "a" * 128

    answer = board.get(f"/sessions/{CHAT}/{session}")

    assert answer.status_code == 200
    assert [path for path, _ in board.fake.calls] == [
        f"/v1/sessions/{CHAT}/{session}",
        f"/v1/sessions/{CHAT}/{session}/events",
    ]


def test_the_fixture_family_is_the_one_the_registry_holds(board: Harness) -> None:
    held = (board.config.registry_dir / "families/chat/family.yaml").read_text(encoding="utf-8")

    assert held == CHAT_FAMILY_YAML
