"""The one write of the noticeboard: a family edit, as one git commit.

`docs/rework/spec.md` §8.2 and §8.3. A test plays a browser behind the
reverse proxy:

1. It asks for the edit page, and reads the form and the CSRF cookie.
2. It changes one field, and posts every field that a browser posts.
3. It reads the registry back with `git`.

The registry is a throwaway git repository under the root
(`proc_registry.py`). No `caregiver` runs, so a save proves the commit and
nothing after it.
"""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlsplit

import httpx
import proc_registry
import pytest
import yaml
from proc_board import (
    CSRF_COOKIE,
    CSRF_FIELD,
    VERB_FIELD,
    VERB_PREVIEW,
    VERB_SAVE,
    BoardStack,
    csrf_of,
    html_of,
    page_of,
)
from proc_html import form_values, table_rows
from proc_registry import FILE_COMMENT, family_path
from proc_tree import FAMILY

NEW_DESCRIPTION = "Answers in metric units."
SUBJECT = "Change what the chat family says it is"
SUBJECT_FIELD = "subject"
OTHER_ORIGIN = "http://other.example"

#: A token that the noticeboard did not give to this form.
OTHER_TOKEN = "another-token-" + "t" * 32

FIRST_COMMIT = 1

#: How many browsers save one family at one time.
SAVES_AT_ONE_TIME = 4


class Browser:
    """One open edit form: its fields, its cookie, and how it posts."""

    def __init__(self, stack: BoardStack, client: httpx.AsyncClient, family: str) -> None:
        self.stack = stack
        self.client = client
        self.path = f"/families/{family}/edit"
        self.values: dict[str, str] = {}
        self.cookie = ""

    async def open(self) -> None:
        response = await self.client.get(self.path)
        assert response.status_code == httpx.codes.OK, response.text
        self.values = form_values(html_of(response).one("form"))
        self.cookie = csrf_of(response)

    def sender(self) -> dict[str, str]:
        """What a browser on the page of the form sends: its cookie and its origin."""
        return {"Cookie": f"{CSRF_COOKIE}={self.cookie}", "Origin": self.stack.origin}

    async def post(self, verb: str, headers: dict[str, str] | None = None) -> httpx.Response:
        """Click one button. `headers` takes the place of what a browser sends."""
        sent = self.sender() if headers is None else headers

        return await self.client.post(
            self.path, data=self.values | {VERB_FIELD: verb}, headers=sent
        )


async def test_the_form_holds_the_family_file_and_its_token(board_alone: BoardStack) -> None:
    """§8.3 rule 3: the token is in a `SameSite=Strict; HttpOnly` cookie and a hidden field."""
    async with board_alone.client() as client:
        response = await client.get(f"/families/{FAMILY}/edit")

    form = html_of(response).one("form")
    values = form_values(form)
    cookie = response.headers["set-cookie"].lower()

    assert form.attrs["method"].lower() == "post"
    assert form.attrs["action"] == f"/families/{FAMILY}/edit"
    assert values[CSRF_FIELD] == csrf_of(response)
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert values["description"] == "General assistant of the fixture."
    # Contract 01 §3.1: `name` and `kind` never change, so a browser posts neither.
    assert "name" not in values
    assert "kind" not in values


async def test_a_save_is_one_commit_of_one_file(board_alone: BoardStack) -> None:
    """§8.2. The noticeboard writes one validated commit, and nothing else.

    CONTRACT-QUESTION: §8.2 says what a save writes. No section gives the
    answer to the browser. Reading taken: the answer of the noticeboard as
    it is, a 303 to the page of the family with the start of the commit id
    in `saved`. A change costs the first three assertions here.
    """
    tree = board_alone.tree

    async with board_alone.client() as client:
        browser = Browser(board_alone, client, FAMILY)
        await browser.open()
        browser.values["description"] = NEW_DESCRIPTION
        browser.values[SUBJECT_FIELD] = SUBJECT
        response = await browser.post(VERB_SAVE)
        after = await page_of(client, response.headers.get("location", "/missing"))

    commit = proc_registry.head(tree)
    text = proc_registry.read_family(tree, FAMILY)
    target = urlsplit(response.headers["location"])

    assert response.status_code == httpx.codes.SEE_OTHER
    assert target.path == f"/families/{FAMILY}"
    assert commit.sha.startswith(parse_qs(target.query)["saved"][0])
    assert proc_registry.commit_count(tree) == FIRST_COMMIT + 1
    assert commit.subject == SUBJECT
    assert commit.paths == (family_path(tree, FAMILY),)
    assert proc_registry.uncommitted(tree) == ""
    assert yaml.safe_load(text)["description"] == NEW_DESCRIPTION
    # §8.2 step 1: the edit is a patch on the file, so its comments stay.
    assert FILE_COMMENT in text
    assert after.one("h1").text.startswith(FAMILY)


async def test_a_preview_writes_nothing(board_alone: BoardStack) -> None:
    """The preview shows what the change does. The registry stays as it was."""
    tree = board_alone.tree
    before = proc_registry.read_family(tree, FAMILY)

    async with board_alone.client() as client:
        browser = Browser(board_alone, client, FAMILY)
        await browser.open()
        browser.values["description"] = NEW_DESCRIPTION
        response = await browser.post(VERB_PREVIEW)

    page = html_of(response)
    changed = [row["field"].text for row in table_rows(page.one("table"))]

    assert response.status_code == httpx.codes.OK
    assert changed == ["description"]
    assert proc_registry.read_family(tree, FAMILY) == before
    assert proc_registry.commit_count(tree) == FIRST_COMMIT
    assert proc_registry.uncommitted(tree) == ""


async def test_a_save_after_a_preview_is_the_commit_of_the_edit(board_alone: BoardStack) -> None:
    """A browser posts the form of the preview page with the save button.

    CONTRACT-QUESTION: §8.2 says what a save writes. No section says which
    values the edit form shows on the page that answers a post. Reading
    taken: the values that the browser posted, so that a save after a
    preview writes the edit that the preview showed. A change costs this
    scenario.
    """
    tree = board_alone.tree

    async with board_alone.client() as client:
        browser = Browser(board_alone, client, FAMILY)
        await browser.open()
        browser.values["description"] = NEW_DESCRIPTION
        browser.values[SUBJECT_FIELD] = SUBJECT
        preview = await browser.post(VERB_PREVIEW)
        browser.values = form_values(html_of(preview).one("form"))
        response = await browser.post(VERB_SAVE)

    commit = proc_registry.head(tree)
    text = proc_registry.read_family(tree, FAMILY)

    assert response.status_code == httpx.codes.SEE_OTHER
    assert proc_registry.commit_count(tree) == FIRST_COMMIT + 1
    assert commit.subject == SUBJECT
    assert yaml.safe_load(text)["description"] == NEW_DESCRIPTION
    assert proc_registry.uncommitted(tree) == ""


async def test_saves_at_one_time_leave_no_edit_without_a_commit(board_alone: BoardStack) -> None:
    """§8.2. The one thing that the noticeboard writes is a commit.

    Each browser saves another description. A save can be refused, and a
    refused save writes nothing. After the last answer, the checkout holds
    one commit for each save and no change that no commit holds.
    """
    tree = board_alone.tree

    async with board_alone.client() as client:
        browsers = [Browser(board_alone, client, FAMILY) for _ in range(SAVES_AT_ONE_TIME)]

        for number, browser in enumerate(browsers):
            await browser.open()
            browser.values["description"] = f"{NEW_DESCRIPTION} Edit {number}."

        responses = await asyncio.gather(*(browser.post(VERB_SAVE) for browser in browsers))

    codes = [response.status_code for response in responses]
    saved = codes.count(httpx.codes.SEE_OTHER)

    assert set(codes) <= {httpx.codes.SEE_OTHER, httpx.codes.OK}
    assert saved >= 1
    assert proc_registry.commit_count(tree) == FIRST_COMMIT + saved
    assert proc_registry.uncommitted(tree) == ""


async def test_a_save_that_does_not_validate_changes_nothing(board_alone: BoardStack) -> None:
    """§8.2 steps 2 and 3. The whole registry validates, or the file comes back.

    Contract 01 §3.6 rule 1: each delegate exists. The edit names one that
    does not.
    """
    tree = board_alone.tree
    before = proc_registry.read_family(tree, FAMILY)

    async with board_alone.client() as client:
        browser = Browser(board_alone, client, FAMILY)
        await browser.open()
        browser.values["delegates"] = "no-such-family\n"
        response = await browser.post(VERB_SAVE)

    refused = [item.text for item in html_of(response).one("ul", "issues").all("li")]

    assert response.status_code == httpx.codes.OK
    assert any("delegates" in line for line in refused)
    assert proc_registry.read_family(tree, FAMILY) == before
    assert proc_registry.commit_count(tree) == FIRST_COMMIT
    assert proc_registry.uncommitted(tree) == ""


async def test_a_save_for_a_family_that_is_not_there_writes_nothing(
    board_alone: BoardStack,
) -> None:
    """A bad route parameter on the one write. The answer is a report."""
    tree = board_alone.tree

    async with board_alone.client() as client:
        browser = Browser(board_alone, client, "no-such-family")
        await browser.open()
        browser.values["description"] = NEW_DESCRIPTION
        response = await browser.post(VERB_SAVE)

    assert response.status_code == httpx.codes.OK
    assert html_of(response).all("p", "problem") != []
    assert proc_registry.commit_count(tree) == FIRST_COMMIT
    assert proc_registry.uncommitted(tree) == ""


@pytest.mark.parametrize(
    "case", ["no-cookie", "another-cookie", "another-field", "another-origin", "no-sender"]
)
async def test_a_post_that_fails_the_csrf_check_is_refused(
    board_alone: BoardStack, case: str
) -> None:
    """§8.3 rules 3 and 4: 403, nothing written, and no value in the answer.

    The origin check runs when the token matches too. A page of a sibling
    domain can hold a token that leaked.
    """
    tree = board_alone.tree

    async with board_alone.client() as client:
        browser = Browser(board_alone, client, FAMILY)
        await browser.open()
        token = browser.cookie
        browser.values["description"] = NEW_DESCRIPTION
        headers = browser.sender()

        if case == "no-cookie":
            del headers["Cookie"]
        elif case == "another-cookie":
            headers["Cookie"] = f"{CSRF_COOKIE}={OTHER_TOKEN}"
        elif case == "another-field":
            browser.values[CSRF_FIELD] = OTHER_TOKEN
        elif case == "another-origin":
            headers["Origin"] = OTHER_ORIGIN
        else:
            del headers["Origin"]

        response = await browser.post(VERB_SAVE, headers)

    assert response.status_code == httpx.codes.FORBIDDEN
    assert token not in response.text
    assert OTHER_TOKEN not in response.text
    assert proc_registry.commit_count(tree) == FIRST_COMMIT
    assert proc_registry.uncommitted(tree) == ""


async def test_a_post_with_a_referer_and_no_origin_is_accepted(board_alone: BoardStack) -> None:
    """§8.3 rule 3 names both headers. One of the two is enough."""
    async with board_alone.client() as client:
        browser = Browser(board_alone, client, FAMILY)
        await browser.open()
        browser.values["description"] = NEW_DESCRIPTION
        headers = browser.sender()
        del headers["Origin"]
        headers["Referer"] = f"{board_alone.origin}/families/{FAMILY}/edit"
        response = await browser.post(VERB_SAVE, headers)

    assert response.status_code == httpx.codes.SEE_OTHER
    assert proc_registry.commit_count(board_alone.tree) == FIRST_COMMIT + 1
