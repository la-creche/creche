"""What the scenarios of `test_proc_board_reports.py` share.

Three kinds of helper are here:

1. A start of the noticeboard with a changed environment. `board_env` of
   `proc_board.py` gives the variables of the unit. A scenario that needs
   another value changes a copy, and `board_env` stays as it is.
2. Readers of an answer that `proc_board.py` does not have: an element by
   its class alone, a link by its text, a row of the audit table that holds a
   report, the attributes of the cookie, the check of a JSON body.
3. One post of the edit form, as a browser sends it.

Nothing here knows a module of a service. Each reader takes an HTTP answer.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Final

import httpx
from proc_board import (
    CSRF_COOKIE,
    HTTP_OK,
    VERB_FIELD,
    VERB_SAVE,
    BoardStack,
    board_env,
    csrf_of,
    html_of,
    page_of,
)
from proc_harness import ProcError
from proc_html import Element, form_values, table_rows
from proc_services import Service
from proc_tree import Tree

#: The variables of `noticeboard/AGENTS.md` that a scenario sets, replaces or
#: removes.
KEY_ENV: Final = "VIEW_ACCESS_KEY"
KEY_FILE_ENV: Final = "VIEW_ACCESS_KEY_FILE"
COOKIE_SECURE_ENV: Final = "VIEW_COOKIE_SECURE"
PAGE_SIZE_ENV: Final = "VIEW_PAGE_SIZE"
PORT_ENV: Final = "VIEW_PORT"

#: The classes of a report on a page, and of a mark beside a value. The one
#: stylesheet of the noticeboard names each one.
PROBLEM: Final = "problem"
PROBLEMS: Final = "problems"
FLAG: Final = "flag"

#: The class of the audit table and of the table of the home page.
AUDIT_TABLE: Final = "audit"
FAMILIES_TABLE: Final = "families"

_SET_COOKIE: Final = "set-cookie"


def changed_env(
    tree: Tree, host: str, port: int, changes: Mapping[str, str | None]
) -> dict[str, str]:
    """The variables of the unit, with each change applied to a copy.

    A value of None removes the variable.
    """
    env = board_env(tree, host, port)

    for name, value in changes.items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value

    return env


def start_board_with(stack: BoardStack, changes: Mapping[str, str | None]) -> None:
    """Start the noticeboard of a prepared stack with a changed environment."""

    def env_for(bind: str) -> dict[str, str]:
        host, _, port = bind.rpartition(":")

        return changed_env(stack.tree, host, int(port), changes)

    stack.board, stack.board_port = stack.start_on_port(Service.NOTICEBOARD, env_for)


def with_class(element: Element, cls: str) -> list[Element]:
    """Each element below this one that has the class, with each tag."""
    found: list[Element] = []

    for child in element.children:
        if isinstance(child, str):
            continue

        if cls in child.classes:
            found.append(child)

        found.extend(with_class(child, cls))

    return found


def link_named(element: Element, text: str) -> str | None:
    """The target of the one link with this text, or None when no link has it."""
    targets = [
        link.attrs["href"]
        for link in element.all("a")
        if link.text == text and "href" in link.attrs
    ]

    if len(targets) > 1:
        raise ProcError(f"the page holds {len(targets)} links with the text {text!r}")

    return targets[0] if targets else None


async def follow(client: httpx.AsyncClient, page: Element, text: str) -> Element:
    """The page behind the one link with this text."""
    target = link_named(page, text)

    if target is None:
        raise ProcError(f"the page holds no link with the text {text!r}")

    return await page_of(client, target)


def families_of(page: Element) -> dict[str, dict[str, Element]]:
    """Each row of a family on the home page, by the text of its first cell."""
    rows = table_rows(page.one("table", FAMILIES_TABLE))

    return {row["family"].text: row for row in rows}


def audit_rows(page: Element) -> list[Element]:
    """Each row in the body of the audit table: a record or a report."""
    return page.one("table", AUDIT_TABLE).one("tbody").all("tr")


def problem_rows(page: Element) -> list[Element]:
    """Each row of the audit table that is a report, or that holds one."""
    return [row for row in audit_rows(page) if PROBLEM in row.classes or with_class(row, PROBLEM)]


def tools_of(page: Element) -> list[str]:
    """The tool of each record on the audit page, in page order."""
    records = table_rows(page.one("table", AUDIT_TABLE))

    return [row["tool"].one("code").text for row in records]


def cookie_attributes(response: httpx.Response) -> dict[str, str]:
    """The attributes of the CSRF cookie of one answer, by name in lower case.

    The name of an attribute has no case (RFC 6265 §5.2). An attribute with
    no value has the empty text here.
    """
    for header in response.headers.get_list(_SET_COOKIE):
        pair, *attributes = [part.strip() for part in header.split(";")]

        if pair.partition("=")[0] != CSRF_COOKIE:
            continue

        found: dict[str, str] = {}

        for attribute in attributes:
            name, _, value = attribute.partition("=")
            found[name.strip().lower()] = value.strip()

        return found

    raise ProcError(f"the answer sets no {CSRF_COOKIE} cookie")


def is_json(body: bytes) -> bool:
    """Whether the bytes of one answer are a strict JSON text in UTF-8.

    `json.loads` alone takes more than that. This check refuses each of
    these: a byte order mark, UTF-16 and UTF-32, the tokens `NaN`, `Infinity`
    and `-Infinity`, a number past the range of a float, and one half of a
    surrogate pair.
    """
    try:
        value = json.loads(
            body.decode("utf-8"), parse_constant=_refuse_constant, parse_float=_finite
        )
        # One half of a surrogate pair has no UTF-8 form.
        json.dumps(value, ensure_ascii=False).encode("utf-8")
    except ValueError:
        return False

    return True


def _refuse_constant(token: str) -> float:
    raise ValueError(f"{token} is no token of JSON")


def _finite(token: str) -> float:
    number = float(token)

    if not math.isfinite(number):
        raise ValueError(f"{token} is past the range of a float")

    return number


async def save_with(
    stack: BoardStack, client: httpx.AsyncClient, family: str, extra: Mapping[str, str]
) -> httpx.Response:
    """Open the edit form of one family, then post it with the save button.

    The post holds each field that a browser posts, the cookie of the form
    and the origin of the page. `extra` adds a field or replaces one.
    """
    path = f"/families/{family}/edit"
    opened = await client.get(path)

    if opened.status_code != HTTP_OK:
        raise ProcError(f"GET {path} answered {opened.status_code}\n{opened.text}")

    values = form_values(html_of(opened).one("form"))
    sender = {"Cookie": f"{CSRF_COOKIE}={csrf_of(opened)}", "Origin": stack.origin}

    return await client.post(
        path, data=values | dict(extra) | {VERB_FIELD: VERB_SAVE}, headers=sender
    )
