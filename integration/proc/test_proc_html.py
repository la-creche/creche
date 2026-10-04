"""The HTML reader, held to what a browser does with the same page.

A scenario of the noticeboard trusts three things: which elements a page
holds, which cell is under which column head, and what a form posts. A
reader that gets one of them wrong makes a scenario pass on a page that a
browser shows differently.
"""

from __future__ import annotations

import pytest
from proc_html import form_values, parse, table_rows

PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>families</title>
<link rel="stylesheet" href="/static/site.css"></head>
<body>
<nav><a href="/">families</a> <a href="/audit">audit</a> <span>no link</span></nav>
<table class="families wide">
<thead><tr><th>family</th><th>status</th></tr></thead>
<tbody>
<tr><td><a href="/families/chat">chat</a></td><td>
    <span class="badge">in   sync</span>
</td></tr>
<tr><td colspan="2">a row that spans the table</td></tr>
</tbody>
</table>
<p class="problem">Tom &amp; Jerry &#39;quoted&#39;</p>
</body></html>
"""

FORM = """<form method="post" action="/families/chat/edit">
<input type="hidden" name="csrf_token" value="tok&amp;en">
<input type="text" name="description" value="a &quot;quoted&quot; value">
<input type="text" name="name" value="chat" disabled>
<input type="text" value="a control with no name">
<input type="checkbox" name="shell">
<input type="checkbox" name="codemode" checked>
<input type="checkbox" name="locked" checked disabled>
<textarea name="delegates">vault-oracle
code-sandbox
</textarea>
<textarea name="egress">
example.org</textarea>
<textarea name="frozen" disabled>kept out</textarea>
<button type="submit" name="verb" value="save">save</button>
</form>
"""


def test_an_element_is_found_by_its_tag_and_its_class() -> None:
    page = parse(PAGE)

    assert page.one("title").text == "families"
    assert page.one("table", "families").classes == {"families", "wide"}
    assert page.one("table", "wide") is page.one("table")
    assert page.all("table", "no-such-class") == []


def test_a_void_element_holds_nothing() -> None:
    """`meta` and `link` have no end tag. The elements after them are no children."""
    page = parse(PAGE)

    assert page.one("meta").children == []
    assert page.one("link").children == []
    assert page.one("head").all("title") != []


def test_the_text_of_an_element_is_what_a_person_reads() -> None:
    page = parse(PAGE)

    assert page.one("span", "badge").text == "in sync"
    assert page.one("p", "problem").text == "Tom & Jerry 'quoted'"


def test_the_links_are_in_page_order() -> None:
    assert parse(PAGE).one("nav").links() == ["/", "/audit"]


def test_a_missing_or_second_element_is_an_error() -> None:
    page = parse(PAGE)

    with pytest.raises(AssertionError, match="holds 0 elements h1, not 1"):
        page.one("h1")

    with pytest.raises(AssertionError, match="holds 2 elements a, not 1"):
        page.one("nav").one("a")


def test_a_table_row_is_its_cells_by_the_column_head() -> None:
    rows = table_rows(parse(PAGE).one("table"))

    assert len(rows) == 1, "the row that spans the table is no row of data"
    assert rows[0]["family"].text == "chat"
    assert rows[0]["family"].links() == ["/families/chat"]
    assert rows[0]["status"].text == "in sync"


def test_a_form_posts_what_a_browser_posts() -> None:
    values = form_values(parse(FORM).one("form"))

    assert values == {
        "csrf_token": "tok&en",
        "description": 'a "quoted" value',
        "codemode": "on",
        "delegates": "vault-oracle\ncode-sandbox\n",
        "egress": "example.org",
    }
