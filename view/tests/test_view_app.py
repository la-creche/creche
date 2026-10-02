"""Every page, the perimeter and one save.

Every source is a fixture: a state root on disk, a git registry in a tmp
dir, and a fake `sessiond`. No host, no socket, no live service.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from agent_family import load_registry
from agent_view.app import build_app
from agent_view.config import DEFAULT_PAGE_SIZE, Config
from agent_view.familyform import Control, form_of
from agent_view.pages import index_of
from agent_view.security import ACCESS_HEADER, CSRF_COOKIE, CSRF_FIELD
from agent_view.sessions import SessionReader
from fastapi.testclient import TestClient
from view_helpers import (
    CHAT_COMMENT,
    CHAT_FAMILY_YAML,
    NOW,
    FakeSessiond,
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
HOST = "view.example.test"


class Harness:
    def __init__(self, client: TestClient, config: Config, fake: FakeSessiond) -> None:
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
def view(tmp_path: Path) -> Iterator[Harness]:
    state = make_state_root(tmp_path)
    registry = make_registry(tmp_path)
    config = Config(
        bind="127.0.0.1",
        port=8370,
        state_root=state,
        registry_dir=registry,
        sessiond_socket=None,
        sessiond_url="http://sessiond",
        page_size=DEFAULT_PAGE_SIZE,
        cookie_secure=False,
        access_key=KEY,
    )
    fake = FakeSessiond()
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


def test_healthz_needs_no_key(view: Harness) -> None:
    """A prober must not be probing the proxy's sign-in page."""
    answer = view.client.get("/healthz")

    assert answer.status_code == 200
    assert answer.json() == {"ok": True}


def test_a_page_without_the_key_is_refused(view: Harness) -> None:
    answer = view.client.get("/")

    assert answer.status_code == 403
    assert answer.json()["error"] == "no_key"


def test_a_page_with_the_wrong_key_is_refused_without_naming_it(view: Harness) -> None:
    answer = view.get("/", key="wrong")

    assert answer.status_code == 403
    assert "wrong" not in answer.text


def test_styling_is_reachable_without_the_key(view: Harness) -> None:
    """A proxy fetches assets outside its header-injecting block."""
    answer = view.client.get("/static/view.css")

    assert answer.status_code == 200


def test_the_home_page_shows_a_row_per_family(view: Harness) -> None:
    answer = view.get("/")

    assert answer.status_code == 200
    assert "chat" in answer.text
    assert "scrum-lead" in answer.text
    assert "in sync" in answer.text


def test_spend_appears_once_per_family_and_never_beside_a_sandbox(view: Harness) -> None:
    """Contract 05 §7 rule 1: the budget belongs to the family key."""
    home = view.get("/").text
    family = view.get("/families/chat").text

    assert home.count("$3.42") == 1
    assert home.count("$1.11") == 1
    # The family page lists sandboxes and names no money at all, because
    # a sandbox has no budget to name.
    assert "$" not in family


def test_a_stale_status_document_is_flagged(view: Harness, tmp_path: Path) -> None:
    from view_helpers import status_doc, write_json

    write_json(
        view.config.families_dir / "chat" / "status.json",
        status_doc(age_s=600.0),
    )

    answer = view.get("/")

    assert "stale" in answer.text
    assert "past the 90s limit" in answer.text


def test_a_malformed_state_file_renders_a_report_not_a_stack_trace(view: Harness) -> None:
    (view.config.families_dir / "chat" / "status.json").write_text("{ broken", encoding="utf-8")

    answer = view.get("/")

    assert answer.status_code == 200
    assert "not JSON" in answer.text
    assert "Traceback" not in answer.text


def test_a_missing_state_file_renders_a_report(view: Harness) -> None:
    (view.config.families_dir / "chat" / "status.json").unlink()

    answer = view.get("/families/chat")

    assert answer.status_code == 200
    assert "missing" in answer.text


def test_the_family_page_lists_sandboxes_and_sessions(view: Harness) -> None:
    answer = view.get("/families/chat")

    assert answer.status_code == 200
    assert "chat-s3" in answer.text
    assert "Boiler service date" in answer.text


def test_the_session_page_shows_the_transcript_and_the_turns(view: Harness) -> None:
    answer = view.get(f"/sessions/{CHAT}/{OWUI}")

    assert answer.status_code == 200
    assert "when is the boiler due?" in answer.text
    assert "In March." in answer.text
    assert "settled" in answer.text


def test_a_dead_sessiond_still_renders_the_session_page(view: Harness) -> None:
    view.fake.fault = "cannot reach sessiond: ConnectError"

    answer = view.get(f"/sessions/{CHAT}/{OWUI}")

    assert answer.status_code == 200
    assert "cannot reach sessiond" in answer.text


def test_the_audit_page_says_it_shows_full_arguments(view: Harness) -> None:
    answer = view.get("/audit")

    assert "full tool arguments" in answer.text


def test_an_audit_filter_narrows_the_page(view: Harness) -> None:
    from view_helpers import audit_line, write_audit_day

    write_audit_day(
        view.config.audit_dir,
        "2026-09-18",
        [audit_line(tool="ha__ha_call", decision="deny", reason="not_granted")],
    )

    every = view.get("/audit").text
    denied = view.get("/audit?decision=deny").text

    assert "kagi__kagi_search_fetch" in every
    assert "ha__ha_call" in denied
    assert "kagi__kagi_search_fetch" not in denied


def test_the_edit_form_renders_from_the_schema(view: Harness) -> None:
    answer = view.get("/families/chat/edit")

    assert answer.status_code == 200
    assert 'name="description"' in answer.text
    assert 'name="model.router"' in answer.text


def test_a_locked_field_is_disabled_with_the_rule_as_its_label(view: Harness) -> None:
    answer = view.get("/families/chat/edit").text

    assert "disabled" in answer
    # The rule's own sentence, escaped by the template as everything is.
    assert "is autonomous only; this family is" in answer


def test_a_post_without_a_csrf_token_is_refused(view: Harness) -> None:
    answer = view.client.post(
        "/families/chat/edit",
        data={"verb": "save"},
        headers={ACCESS_HEADER: KEY, "Origin": f"http://{HOST}"},
    )

    assert answer.status_code == 403
    assert answer.json()["error"] in ("no_token", "bad_token")


def test_a_post_from_a_foreign_origin_is_refused(view: Harness) -> None:
    view.get("/families/chat/edit")

    answer = view.post("/families/chat/edit", {"verb": "save"}, headers={"Origin": "http://evil"})

    assert answer.status_code == 403
    assert answer.json()["error"] == "foreign_origin"


def posted_form(view: Harness) -> dict[str, str]:
    """Rebuild what the browser would post from the rendered form."""
    registry = load_registry(view.config.registry_dir)
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


def test_a_valid_edit_makes_exactly_one_commit(view: Harness) -> None:
    view.get("/families/chat/edit")
    before = commit_count(view.config.registry_dir)
    body = posted_form(view)
    body["description"] = "the house assistant, rewritten"
    body["verb"] = "save"
    body["subject"] = "widen the description"

    answer = view.post("/families/chat/edit", body)

    assert answer.status_code == 303
    assert commit_count(view.config.registry_dir) == before + 1


def test_a_save_keeps_the_files_comments(view: Harness) -> None:
    """The file explains each grant where it sits, and regenerating the
    document from the model would drop every line of that on the first save."""
    view.get("/families/chat/edit")
    body = posted_form(view)
    body["description"] = "the house assistant, rewritten"
    body["verb"] = "save"

    view.post("/families/chat/edit", body)

    saved = (view.config.registry_dir / "families/chat/family.yaml").read_text(encoding="utf-8")
    assert CHAT_COMMENT in saved
    assert "the house assistant, rewritten" in saved
    # The style the author wrote, on a line the edit never touched.
    assert "- { path: /srv/agents/vault, mode: ro }" in saved


def test_a_save_that_changes_nothing_makes_no_commit(view: Harness) -> None:
    """The form posts every field back, so a save with no edit must not
    rewrite the whole file from the model and commit the difference. The
    registry's git log is the grant audit trail."""
    view.get("/families/chat/edit")
    before = commit_count(view.config.registry_dir)
    original = (view.config.registry_dir / "families/chat/family.yaml").read_bytes()
    body = posted_form(view)
    body["verb"] = "save"

    answer = view.post("/families/chat/edit", body)

    assert answer.status_code == 303
    assert commit_count(view.config.registry_dir) == before
    assert (view.config.registry_dir / "families/chat/family.yaml").read_bytes() == original


def test_an_invalid_edit_writes_nothing_and_reports(view: Harness) -> None:
    view.get("/families/chat/edit")
    before = commit_count(view.config.registry_dir)
    original = (view.config.registry_dir / "families/chat/family.yaml").read_bytes()
    body = posted_form(view)
    body["model.budget_usd_per_day"] = "-4"
    body["verb"] = "save"

    answer = view.post("/families/chat/edit", body)

    assert answer.status_code == 200
    assert "refused" in answer.text
    assert commit_count(view.config.registry_dir) == before
    assert (view.config.registry_dir / "families/chat/family.yaml").read_bytes() == original


def test_a_preview_says_live_or_replace_and_writes_nothing(view: Harness) -> None:
    view.get("/families/chat/edit")
    before = commit_count(view.config.registry_dir)
    body = posted_form(view)
    body["sandbox.cpus"] = "6"
    body["verb"] = "preview"

    answer = view.post("/families/chat/edit", body)

    assert answer.status_code == 200
    assert "replaces the sandbox" in answer.text
    assert "sandbox.cpus" in answer.text
    assert commit_count(view.config.registry_dir) == before


def test_a_live_only_preview_says_the_sandbox_keeps_running(view: Harness) -> None:
    view.get("/families/chat/edit")
    body = posted_form(view)
    body["description"] = "the house assistant, again"
    body["verb"] = "preview"

    answer = view.post("/families/chat/edit", body)

    assert "lands live" in answer.text


def test_the_fixture_family_is_the_one_the_registry_holds(view: Harness) -> None:
    held = (view.config.registry_dir / "families/chat/family.yaml").read_text(encoding="utf-8")

    assert held == CHAT_FAMILY_YAML
