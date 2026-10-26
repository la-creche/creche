"""Each page of the noticeboard, over HTTP, with the files of one root.

`docs/rework/spec.md` §8.1 lists the pages. Each scenario asks for one page
as a browser behind the reverse proxy does, and reads the answer through an
HTML reader (`proc_html.py`). No scenario compares a whole page with a text.

The sources are the ones contract 05 §8 names: the status documents, the
validation report, the outcome records, the audit files and `attendance` as
`view-ro`. The suite writes the files that `caregiver` and the chaperone
write on the host. The session API left `attendance` in the phase 2 plan,
row 21, so a page that reads a session shows a problem. The scenarios that
read a session or an outcome record that `attendance` wrote left with it.

CONTRACT-QUESTION: §8.1 says what each page shows. No contract gives the
markup. Reading taken: the markup of the templates is the interface, because
the one stylesheet names its classes. So a scenario finds a table by its
class, a cell by the text of its column head, and a report by the classes
`problem`, `problems` and `issues`. A change of the markup costs the names
in this file.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from proc_board import KEY_HEADER, REVIEW, BoardStack, html_of, page_of
from proc_html import table_rows
from proc_tree import (
    FAMILY,
    SANDBOX,
    VIEW_KEY,
    Validity,
    append_audit,
    audit_record,
    token_of,
    write_status,
    write_validation_report,
)

TITLE = "Kitchen sensor"
PROMPT = "which sensor is cold"
JOB_PROMPT = "review the house"

#: Contract 05 §2 rule 5: a document older than 90 seconds is not current.
STALE_AGE = timedelta(minutes=10)

#: The two issues of one report (contract 01 §7), as `caregiver` writes them.
ISSUES = [
    {"severity": "error", "loc": "delegates[0]", "msg": "'code' is not a thin family"},
    {"severity": "warning", "loc": "model.router", "msg": "the alias was not checked"},
]

WRONG_KEY = "wrong-view-key-" + "w" * 32
SETTLE_DEADLINE_S = 60.0

#: Each page of §8.1 that needs no session.
PAGES = ["/", f"/families/{FAMILY}", f"/families/{REVIEW}", "/audit", f"/families/{FAMILY}/edit"]

#: A route parameter that has the form of an id and names no family and no
#: session.
BAD_PARAMETERS = [
    "/families/no-such-family",
    "/families/no-such-family/edit",
    f"/sessions/{FAMILY}/owui-no-such-session",
    "/sessions/no-such-family/owui-x",
]

#: A route parameter that is no family name (contract 01 §2) and no session
#: id (contract 02 §2).
NOT_AN_ID = [
    "/families/Not%20A%20Name",
    "/families/%2e%2e",
    "/families/Not%20A%20Name/edit",
    "/sessions/Not%20A%20Name/owui-x",
    f"/sessions/{FAMILY}/not%20an%20id",
]

BAD_QUERIES = ["offset=abc", "offset=-4", "offset=99999999", "offset="]


async def test_the_home_page_lists_each_family_of_the_root(board_alone: BoardStack) -> None:
    """`GET /`. One row per status document (contract 05 §2 rule 6)."""
    async with board_alone.client() as browser:
        page = await page_of(browser, "/")

    rows = {row["family"].text: row for row in table_rows(page.one("table", "families"))}

    assert sorted(rows) == [FAMILY, REVIEW]
    assert rows[FAMILY]["family"].links() == [f"/families/{FAMILY}"]
    assert rows[FAMILY]["kind"].text == "attended"
    assert rows[REVIEW]["kind"].text == "autonomous"
    # Contract 05 §2.2: `in_sync` in the file, "in sync" on the page.
    assert rows[FAMILY]["status"].text == "in sync"
    assert SANDBOX in rows[FAMILY]["serving"].text
    # Contract 05 §2.1: the queue limit of `attendance`, only where a queue is.
    assert rows[REVIEW]["queue cap"].text == "100"
    assert rows[FAMILY]["queue cap"].text != "100"


async def test_a_stale_status_document_reads_unknown(board_alone: BoardStack) -> None:
    """Contract 05 §2 rule 5. A reader never shows a stale state as current."""
    old = (datetime.now(UTC) - STALE_AGE).strftime("%Y-%m-%dT%H:%M:%SZ")
    write_status(board_alone.tree, written_at=old)

    async with board_alone.client() as browser:
        page = await page_of(browser, "/")

    rows = {row["family"].text: row for row in table_rows(page.one("table", "families"))}
    status = rows[FAMILY]["status"].text

    assert "unknown" in status
    assert "in sync" not in status
    assert rows[REVIEW]["status"].text == "in sync"


async def test_an_invalid_family_shows_its_report(board_alone: BoardStack) -> None:
    """Contract 05 §3.2. The page reads the report that the status document names."""
    tree = board_alone.tree
    write_status(tree, validity=Validity.INVALID)
    write_validation_report(tree, FAMILY, ISSUES)

    async with board_alone.client() as browser:
        page = await page_of(browser, f"/families/{FAMILY}")

    shown = [item.text for item in page.one("ul", "issues").all("li")]

    assert "invalid" in page.one("h1").text
    assert len(shown) == len(ISSUES)

    for issue, line in zip(ISSUES, shown, strict=True):
        assert issue["loc"] in line
        assert issue["msg"] in line


async def test_the_audit_page_shows_each_record_and_filters(board_alone: BoardStack) -> None:
    """`GET /audit`. Contract 04 §6: the page reads the day files, arguments and all."""
    append_audit(
        board_alone.tree,
        [
            audit_record(FAMILY, "embed", args={"input": "boiler service date"}),
            audit_record(REVIEW, "ha_call", decision="deny", reason="not_granted"),
        ],
    )

    async with board_alone.client() as browser:
        every = await page_of(browser, "/audit")
        one = await page_of(browser, f"/audit?family={REVIEW}")

    rows = table_rows(every.one("table", "audit"))
    filtered = table_rows(one.one("table", "audit"))

    assert sorted(row["tool"].one("code").text for row in rows) == ["embed", "ha_call"]
    assert any("boiler service date" in row["arguments"].text for row in rows)
    assert [row["tool"].one("code").text for row in filtered] == ["ha_call"]
    assert filtered[0]["decision"].text.startswith("deny")
    assert REVIEW in filtered[0]["family"].text


async def test_each_page_is_html_with_no_script(board_alone: BoardStack) -> None:
    """`docs/rework/spec.md` §8: no script tag on any page."""
    async with board_alone.client() as browser:
        pages = {path: await page_of(browser, path) for path in PAGES}

    for path, page in pages.items():
        assert page.all("script") == [], path
        assert page.one("title").text, path
        assert {"/", "/audit"} <= set(page.one("nav").links()), path


async def test_a_page_answers_while_attendance_is_down(board_alone: BoardStack) -> None:
    """Invariant 20. The page says what it cannot see. It does not fail."""
    session = f"owui-{uuid.uuid4()}"

    async with board_alone.client() as browser:
        home = await page_of(browser, "/")
        family = await page_of(browser, f"/families/{FAMILY}")
        missing = await page_of(browser, f"/sessions/{FAMILY}/{session}")

    assert len(table_rows(home.one("table", "families"))) == 2
    assert table_rows(family.all("table")[0])[0]["id"].text == SANDBOX
    assert family.all("p", "problem") != []
    assert missing.one("ul", "problems").all("li") != []


async def test_a_bad_route_parameter_gets_a_report(board: BoardStack) -> None:
    """A name that names nothing is a page with a report, never a failed request."""
    async with board.client() as browser:
        pages = {path: await page_of(browser, path) for path in BAD_PARAMETERS}

    for path, page in pages.items():
        assert page.all("p", "problem") + page.all("ul", "problems") != [], path


async def test_a_route_parameter_that_is_no_id_is_not_found(board_alone: BoardStack) -> None:
    """The noticeboard checks each route parameter before it reads a file.

    CONTRACT-QUESTION: `docs/rework/spec.md` §8.1 lists the routes and gives
    no answer for a parameter that is not an id. Reading taken: the answer of
    the noticeboard as it is, the 404 of a path that no route has. A change
    costs this scenario.
    """
    async with board_alone.client() as browser:
        answers = {path: await browser.get(path) for path in NOT_AN_ID}

    for path, answer in answers.items():
        assert answer.status_code == httpx.codes.NOT_FOUND, path


async def test_a_bad_query_of_the_audit_page_is_ignored(board_alone: BoardStack) -> None:
    append_audit(board_alone.tree, [audit_record(FAMILY, "embed")])

    async with board_alone.client() as browser:
        pages = {query: await page_of(browser, f"/audit?{query}") for query in BAD_QUERIES}

    for query, page in pages.items():
        assert page.one("table", "audit").one("thead").all("th") != [], query


async def test_a_path_that_names_no_page_is_not_found(board_alone: BoardStack) -> None:
    """An encoded slash in a name makes a path that no route has."""
    async with board_alone.client() as browser:
        deeper = await browser.get("/families/..%2F..%2Fstate")
        other = await browser.get("/no-such-page")

    assert deeper.status_code == httpx.codes.NOT_FOUND
    assert other.status_code == httpx.codes.NOT_FOUND


async def test_healthz_and_the_stylesheet_need_no_key(board_alone: BoardStack) -> None:
    """§8.1: a proxy fetches both outside the block that adds the key."""
    async with board_alone.client(key=None) as proxy:
        health = await proxy.get("/healthz")
        style = await proxy.get("/static/noticeboard.css")

    assert health.status_code == httpx.codes.OK
    assert style.status_code == httpx.codes.OK
    assert style.headers["content-type"].startswith("text/css")


@pytest.mark.parametrize("key", [None, WRONG_KEY, ""], ids=["missing", "wrong", "empty"])
async def test_a_page_without_the_key_is_refused(board_alone: BoardStack, key: str | None) -> None:
    """§8.3 rules 1 and 4: 403, and the answer never holds the value."""
    headers = {} if key is None else {KEY_HEADER: key}

    async with board_alone.client(key=None) as stranger:
        answers = {path: await stranger.get(path, headers=headers) for path in PAGES}

    for path, response in answers.items():
        assert response.status_code == httpx.codes.FORBIDDEN, path
        assert VIEW_KEY not in response.text
        assert WRONG_KEY not in response.text
        assert FAMILY not in response.text


async def test_no_page_shows_a_secret(board: BoardStack) -> None:
    """Invariant 13. No key and no token is on a page."""
    paths = ["/", f"/families/{FAMILY}", "/audit"]
    secrets = [VIEW_KEY, token_of("view-ro"), token_of("door-owui")]

    async with board.client() as browser:
        for path in paths:
            response = await browser.get(path)

            assert response.status_code == httpx.codes.OK
            assert not any(secret in response.text for secret in secrets), path
            assert html_of(response).all("script") == []
