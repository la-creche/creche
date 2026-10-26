"""What the noticeboard reports, how its audit page pages, and its perimeter.

`test_proc_board_pages.py` holds each page with sources that are whole. The
scenarios here are in three groups:

1. The home page and the family page, with a status document that a reader
   cannot use, and with the fault, spend and reconcile blocks of
   contract 05 §3.3, §7 and §3.4.
2. The audit page over the audit file of the Rust chaperone: the pages, a
   line that is no record, the filters, a file that is not there.
3. The perimeter of `docs/rework/spec.md` §8.3: the key, the body of a
   refusal, the cookie, the size of a post, and a start that the config
   refuses.

Two tests at the end hold the JSON check of `proc_board_reports.py` against
its own rule. They start no service.

Each scenario asks as a browser behind the reverse proxy does, and reads the
answer through `proc_html.py`. The suite writes each file that `caregiver`
and the chaperone write on the host (`proc_tree.py`).

A scenario that needs its own environment starts the noticeboard itself, with
`start_board_with`. The variables are the ones that `noticeboard/AGENTS.md`
names. No contract names them.

The markup is the interface, as in `test_proc_board_pages.py`. A scenario
finds a report by the classes `problem` and `problems`, and a mark beside a
value by the class `flag`. The one stylesheet names the three.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import proc_registry
import pytest
from proc_board import KEY_HEADER, REVIEW, BoardStack, page_of
from proc_board_reports import (
    AUDIT_TABLE,
    COOKIE_SECURE_ENV,
    FLAG,
    KEY_ENV,
    KEY_FILE_ENV,
    PAGE_SIZE_ENV,
    PORT_ENV,
    PROBLEM,
    PROBLEMS,
    audit_rows,
    changed_env,
    cookie_attributes,
    families_of,
    follow,
    is_json,
    link_named,
    problem_rows,
    save_of_size,
    start_board_with,
    tools_of,
    with_class,
)
from proc_harness import LOOPBACK, TcpAddress, is_listening
from proc_services import Service
from proc_tree import (
    FAMILY,
    VIEW_KEY,
    Fault,
    append_audit_lines,
    audit_line,
    audit_record,
    remove_families_dir,
    remove_status,
    replace_secret,
    status_fault,
    status_reconcile,
    status_spend,
    write_status,
    write_status_text,
)

#: Contract 05 §2.2: `in_sync` in the file, "in sync" on the page.
IN_SYNC = "in sync"

#: The start of a status document. No JSON reader takes it.
NOT_JSON = f'{{"family": "{FAMILY}", "state": "in_sync", '

#: Contract 05 §7: what one family spent in the window, and how a page
#: writes that number of dollars.
SPEND_USD = 3.42
SPEND_TEXT = f"{SPEND_USD:.2f}"

#: How far the `as_of` of a spend that is not current is behind `written_at`.
OLD_SPEND_AGE = timedelta(hours=1)
RFC3339 = "%Y-%m-%dT%H:%M:%SZ"

#: Contract 05 §3.4: one step of a pass. No other part of a page holds it.
STEP = "create_sandbox"

#: `VIEW_PAGE_SIZE` of the paging scenario, and the tool of each call that
#: the scenario writes, the oldest first.
PAGE_SIZE = 2
CALLS = ["tool__call_1", "tool__call_2", "tool__call_3", "tool__call_4", "tool__call_5"]

#: The texts of the two paging links.
OLDER = "older"
NEWER = "newer"

#: How many characters of a record a line holds that the writer did not end.
TORN_CHARS = 20

#: The three tools of the filter scenario: exposed names of the Rust
#: chaperone. No name is a part of another name.
EMBED = "kagi__kagi_search_fetch"
HA_CALL = "ha-call__call_service"
JOB_STATUS = "attendance__job_status"

#: The detail of a call to a tool that no grant holds.
NOT_GRANTED = "deny:tool_not_granted"

#: U+2028. Built with `chr`, so this file holds no character that an editor
#: does not show.
LINE_SEPARATOR = chr(0x2028)
BEFORE_SEPARATOR = "boiler"
AFTER_SEPARATOR = "service"

UNKNOWN_PATH = "/no-such-page"
EDIT_PATH = f"/families/{FAMILY}/edit"
WRONG_KEY = "wrong-view-key-" + "w" * 32

#: Two more keys. Each is an obvious fixture and never a credential. The
#: first has the 32 bytes of the floor for a key on a LAN bind.
KEY_BYTES = 32
LITERAL_KEY = "literal-view-key-".ljust(KEY_BYTES, "k")
OTHER_KEY = "another-view-key-" + "a" * 32

#: The value of `VIEW_COOKIE_SECURE` for a run with no TLS.
NOT_SECURE = "0"

#: 1 MiB: the longest body of a post that the noticeboard reads.
BODY_LIMIT = 1 << 20
NEW_DESCRIPTION = "Answers in metric units."
FIRST_COMMIT = 1

EXIT_DEADLINE_S = 30.0

#: A refusal that is strict JSON in UTF-8, and seven bodies that are not. A
#: lax reader takes each of the seven.
STRICT_BODY = '{"ok": false, "error": "no_key"}'
LAX_BODIES = {
    "nan": b'{"ok": false, "error": NaN}',
    "infinity": b'{"ok": false, "error": -Infinity}',
    "float-past-the-range": b'{"ok": false, "error": 1e999}',
    "byte-order-mark": b"\xef\xbb\xbf" + STRICT_BODY.encode("utf-8"),
    "utf-16": STRICT_BODY.encode("utf-16"),
    "utf-32": STRICT_BODY.encode("utf-32"),
    "lone-surrogate": b'{"ok": false, "error": "\\ud800"}',
}


# ------------------------------------------------- the home and family pages


async def test_a_status_document_that_is_not_json_has_a_row(board_alone: BoardStack) -> None:
    """One file that a reader cannot use never hides a family (invariant 20).

    CONTRACT-QUESTION: contract 05 §2 rule 2 has the writer put a whole
    document in place. No contract says what a page shows for a file that is
    not JSON, or for a family directory with no document. Reading taken: the
    family has a row, the row does not read `in sync`, and each other row is
    as before. The noticeboard shows the status `unreadable` in each case.
    Its sentence is `status.json is not JSON` with the text of its JSON
    reader, or `status.json is missing`. A change costs two scenarios.
    """
    write_status_text(board_alone.tree, FAMILY, NOT_JSON)

    async with board_alone.client() as browser:
        page = await page_of(browser, "/")

    rows = families_of(page)

    assert sorted(rows) == [FAMILY, REVIEW]
    assert IN_SYNC not in rows[FAMILY]["status"].text
    assert rows[REVIEW]["status"].text == IN_SYNC


async def test_a_family_directory_with_no_document_has_a_row(board_alone: BoardStack) -> None:
    """Contract 05 §2 rule 6: a reader lists the directory to find each family.

    The directory of the family stays, with each other file of it. Only the
    status document is not there.

    CONTRACT-QUESTION: see `test_a_status_document_that_is_not_json_has_a_row`.
    """
    remove_status(board_alone.tree, FAMILY)

    async with board_alone.client() as browser:
        page = await page_of(browser, "/")

    rows = families_of(page)

    assert sorted(rows) == [FAMILY, REVIEW]
    assert IN_SYNC not in rows[FAMILY]["status"].text
    assert rows[REVIEW]["status"].text == IN_SYNC


async def test_a_degraded_family_shows_its_fault(board_alone: BoardStack) -> None:
    """Contract 05 §3.3. The row of the family holds the code of each fault."""
    code = Fault.IMAGE_BEHIND
    write_status(board_alone.tree, faults=(status_fault(code),))

    async with board_alone.client() as browser:
        page = await page_of(browser, "/")

    rows = families_of(page)

    assert "degraded" in rows[FAMILY]["status"].text
    assert code.value in rows[FAMILY]["faults"].text
    assert code.value not in rows[REVIEW]["faults"].text


async def test_the_home_page_shows_the_spend_of_a_family(board_alone: BoardStack) -> None:
    """Contract 05 §7. Spend is one number for each family, from its document."""
    write_status(board_alone.tree, spend=status_spend(SPEND_USD))

    async with board_alone.client() as browser:
        page = await page_of(browser, "/")

    rows = families_of(page)

    assert SPEND_TEXT in rows[FAMILY]["spend today"].text
    assert SPEND_TEXT not in rows[REVIEW]["spend today"].text


async def test_a_spend_with_an_old_as_of_has_a_flag(board_alone: BoardStack) -> None:
    """Contract 05 §7 rule 4. A reader never shows a stale number as current.

    A spend read that fails leaves the old numbers with their old `as_of`,
    and it raises the fault `spend_unknown`. The document here holds both.

    CONTRACT-QUESTION: rule 4 has the reader compare `as_of` with
    `written_at`. It gives no age and no form of the mark. Reading taken: an
    `as_of` one hour behind `written_at` is not current, and the spend cell
    then holds an element with the class `flag`. The noticeboard takes
    90 seconds as the limit and shows `spend is stale`. A change costs the
    name of the class in `proc_board_reports.py`.
    """
    old = (datetime.now(UTC) - OLD_SPEND_AGE).strftime(RFC3339)
    write_status(
        board_alone.tree,
        faults=(status_fault(Fault.SPEND_UNKNOWN),),
        spend=status_spend(SPEND_USD, as_of=old),
    )

    async with board_alone.client() as browser:
        page = await page_of(browser, "/")

    rows = families_of(page)

    assert with_class(rows[FAMILY]["spend today"], FLAG) != []
    assert with_class(rows[REVIEW]["spend today"], FLAG) == []


async def test_a_reconciling_family_names_its_step(board_alone: BoardStack) -> None:
    """Contract 05 §3.4. The block names the step in flight, and so does the page."""
    write_status(board_alone.tree, reconcile=status_reconcile(STEP))

    async with board_alone.client() as browser:
        page = await page_of(browser, f"/families/{FAMILY}")
        other = await page_of(browser, f"/families/{REVIEW}")

    assert "reconciling" in page.one("h1").text
    assert STEP in page.one("main").text
    assert STEP not in other.one("main").text


async def test_a_state_root_with_no_families_directory_gets_a_report(
    board_prepared: BoardStack,
) -> None:
    """The page says what it cannot see. It does not fail (invariant 20).

    CONTRACT-QUESTION: contract 05 §2 rule 6 has a reader list the families
    directory. No contract says what the home page shows when no such
    directory is there. Reading taken: the page answers 200, it holds an
    element with the class `problem`, and its table holds no row of a
    family. The noticeboard shows the sentence `no families directory at`
    with the path, and one row with the text `no families`. A change costs
    this scenario.
    """
    remove_families_dir(board_prepared.tree)
    board_prepared.start_board()

    async with board_prepared.client() as browser:
        page = await page_of(browser, "/")

    assert with_class(page, PROBLEM) != []
    assert families_of(page) == {}


# ------------------------------------------------------------ the audit page


async def test_the_audit_page_pages_from_the_newest_record(board_prepared: BoardStack) -> None:
    """`docs/rework/spec.md` §8.1: the audit page shows the audit file, paged.

    Five calls are in the file, and a page holds two.

    CONTRACT-QUESTION: §8.1 says `paged`. No contract gives the order of the
    calls or the links between two pages. Reading taken: the page of the
    noticeboard as it is. The call with the newest record is first. A link
    with the text `older` leads to the next calls, and a link with the text
    `newer` leads one page back. A change costs this scenario.
    """
    tree = board_prepared.tree
    records = [audit_record(FAMILY, tool, call) for call, tool in enumerate(CALLS, start=1)]
    append_audit_lines(tree, [audit_line(record) for record in records])
    start_board_with(board_prepared, {PAGE_SIZE_ENV: str(PAGE_SIZE)})

    async with board_prepared.client() as browser:
        newest = await page_of(browser, "/audit")
        middle = await follow(browser, newest, OLDER)
        oldest = await follow(browser, middle, OLDER)
        back = await follow(browser, middle, NEWER)
        again = await follow(browser, oldest, NEWER)

    newest_first = CALLS[::-1]

    assert tools_of(newest) == newest_first[:PAGE_SIZE]
    assert link_named(newest, NEWER) is None
    assert tools_of(middle) == newest_first[PAGE_SIZE : 2 * PAGE_SIZE]
    assert tools_of(oldest) == newest_first[2 * PAGE_SIZE :]
    assert link_named(oldest, OLDER) is None
    assert tools_of(back) == tools_of(newest)
    assert tools_of(again) == tools_of(middle)


async def test_an_audit_line_that_is_not_json_takes_one_row(board_alone: BoardStack) -> None:
    """A hole in the audit is a row. It hides no record (invariant 15).

    CONTRACT-QUESTION: the Rust sink writes one record for each line. No
    contract says what the page shows for a line that is no record. Reading
    taken: the line takes one row that is a report, with the class
    `problem` on the row or on a cell of it, and each record beside the line
    has its row. The noticeboard puts the class on the one cell of the row.
    A change costs this scenario.
    """
    tree = board_alone.tree
    first, second = (
        audit_line(audit_record(FAMILY, tool, call))
        for call, tool in enumerate((EMBED, HA_CALL), start=1)
    )
    append_audit_lines(tree, [first, first[:TORN_CHARS], second])

    async with board_alone.client() as browser:
        page = await page_of(browser, "/audit")

    assert len(problem_rows(page)) == 1
    assert len(audit_rows(page)) == 3
    assert sorted(tools_of(page)) == sorted([EMBED, HA_CALL])


@pytest.mark.parametrize(
    ("query", "tool"),
    [
        ("decision=deny", HA_CALL),
        (f"tool={JOB_STATUS}", JOB_STATUS),
    ],
    ids=["decision", "tool"],
)
async def test_a_filter_of_the_audit_page_keeps_its_records(
    board_alone: BoardStack, query: str, tool: str
) -> None:
    """The two filters that `test_proc_board_pages.py` does not hold.

    Each filter keeps one of three calls, and each one keeps another.

    CONTRACT-QUESTION: no contract names a filter of the audit page. Reading
    taken: the query names of the form on the page. The noticeboard keeps a
    call whose decision is equal to the value. For a tool it keeps a call
    whose tool holds the value as a part. A whole value gives one result
    with each rule. A change costs this scenario.
    """
    tree = board_alone.tree
    records = [
        audit_record(FAMILY, EMBED, 1),
        audit_record(FAMILY, HA_CALL, 2, detail=NOT_GRANTED),
        audit_record(REVIEW, JOB_STATUS, 3),
    ]
    append_audit_lines(tree, [audit_line(record) for record in records])

    async with board_alone.client() as browser:
        every = await page_of(browser, "/audit")
        kept = await page_of(browser, f"/audit?{query}")

    assert len(tools_of(every)) == len(records)
    assert tools_of(kept) == [tool]


async def test_the_audit_page_reports_a_missing_file(board_alone: BoardStack) -> None:
    """The page says what it cannot read, and it shows the rest of itself.

    No scenario of this root wrote an audit record, so the root holds no
    audit file.

    CONTRACT-QUESTION: `docs/rework/spec.md` §8.3 gives a banner for an
    audit file that the service cannot read. No contract names a root with
    no audit file. Reading taken: that page gets the same report, a list
    with the class `problems`, and it still holds the table. The
    noticeboard shows `audit.jsonl is missing`. A change costs this
    scenario.
    """
    assert not board_alone.tree.audit_file.exists()

    async with board_alone.client() as browser:
        page = await page_of(browser, "/audit")

    assert page.one("ul", PROBLEMS).all("li") != []
    assert page.one("table", AUDIT_TABLE).one("thead").all("th") != []


async def test_a_line_with_a_line_separator_is_one_row(board_alone: BoardStack) -> None:
    """LF alone ends a line. U+2028 is legal inside a JSON string.

    The Rust sink writes JSONL. Contract 02 §8 gives the rule for the event
    stream: a reader splits on LF only. The noticeboard uses the same rule
    for the audit file. No field of a Rust record holds U+2028, so the line
    is one row that is a report, not two rows.
    """
    tree = board_alone.tree
    text = f"deny:{BEFORE_SEPARATOR}{LINE_SEPARATOR}{AFTER_SEPARATOR}"
    line = audit_line(audit_record(FAMILY, EMBED, detail=text))
    append_audit_lines(tree, [line])

    async with board_alone.client() as browser:
        page = await page_of(browser, "/audit")

    assert LINE_SEPARATOR in line
    assert len(audit_rows(page)) == 1
    assert len(problem_rows(page)) == 1


# ------------------------------------------------------------- the perimeter


async def test_an_unknown_path_with_no_key_is_refused(board_alone: BoardStack) -> None:
    """§8.3 rule 1: the key is checked on each path, also on one with no route."""
    async with board_alone.client(key=None) as stranger:
        answer = await stranger.get(UNKNOWN_PATH)

    assert answer.status_code == httpx.codes.FORBIDDEN


@pytest.mark.parametrize("key", [None, WRONG_KEY], ids=["missing", "wrong"])
async def test_the_body_of_a_refusal_is_json_with_no_key(
    board_alone: BoardStack, key: str | None
) -> None:
    """§8.3 rule 4: a refusal says what was wrong. It never holds a value.

    CONTRACT-QUESTION: rule 4 gives no form of the body and no word. Reading
    taken: the body is a JSON text, and it holds no key. The noticeboard
    answers `{"ok":false,"error":"no_key"}` for a missing key and the word
    `bad_key` for a wrong key, with the type `application/json`. A change
    costs one assertion here.
    """
    headers = {} if key is None else {KEY_HEADER: key}

    async with board_alone.client(key=None) as stranger:
        answer = await stranger.get("/", headers=headers)

    assert answer.status_code == httpx.codes.FORBIDDEN
    assert is_json(answer.content), answer.text
    assert VIEW_KEY not in answer.text
    assert WRONG_KEY not in answer.text


async def test_the_cookie_has_secure_with_no_variable(board_alone: BoardStack) -> None:
    """The proxy ends TLS, so a browser gets the cookie over HTTPS only.

    CONTRACT-QUESTION: §8.3 rule 3 gives the cookie `SameSite=Strict` and
    `HttpOnly`. No contract gives `Secure`, `Path` or a variable for one.
    Reading taken: the cookie as the noticeboard sets it. It has `Secure`
    unless `VIEW_COOKIE_SECURE` is `0`, and it has `Path=/`. The header of
    the noticeboard ends in `HttpOnly; Path=/; SameSite=strict; Secure`. A
    change costs the three cookie scenarios.
    """
    async with board_alone.client() as browser:
        answer = await browser.get(EDIT_PATH)

    assert "secure" in cookie_attributes(answer)


async def test_the_cookie_has_no_secure_when_the_variable_is_0(
    board_prepared: BoardStack,
) -> None:
    """A run with no TLS: a browser sends a `Secure` cookie over HTTPS only.

    CONTRACT-QUESTION: see `test_the_cookie_has_secure_with_no_variable`.
    """
    start_board_with(board_prepared, {COOKIE_SECURE_ENV: NOT_SECURE})

    async with board_prepared.client() as browser:
        answer = await browser.get(EDIT_PATH)

    assert "secure" not in cookie_attributes(answer)


async def test_the_cookie_has_the_root_path(board_alone: BoardStack) -> None:
    """The form of each family posts the one cookie, so its path is the root.

    CONTRACT-QUESTION: see `test_the_cookie_has_secure_with_no_variable`.
    """
    async with board_alone.client() as browser:
        answer = await browser.get(EDIT_PATH)

    assert cookie_attributes(answer).get("path") == "/"


async def test_a_post_past_the_body_limit_is_refused(board_alone: BoardStack) -> None:
    """Each byte of a request body is input. The size is checked before use.

    The post is a save with an edit, from the form and with its token. One
    more field makes the body one byte longer than 1 MiB. The same post with
    one byte less is a save: `test_a_post_at_the_body_limit_is_a_save`.

    CONTRACT-QUESTION: no contract gives a limit for the body of a post or
    the answer to a longer one. Reading taken: the noticeboard as it is. A
    body past 1 MiB gets 403 and writes nothing, and a body of exactly
    1 MiB is read. The noticeboard answers with the word `bad_token`, and it
    sets the cookie. It reads no field that its form does not have. A change
    costs one assertion here and one scenario.
    """
    tree = board_alone.tree
    before = proc_registry.read_family(tree, FAMILY)
    edit = {"description": NEW_DESCRIPTION}

    async with board_alone.client() as browser:
        answer = await save_of_size(board_alone, browser, FAMILY, edit, BODY_LIMIT + 1)

    assert answer.status_code == httpx.codes.FORBIDDEN
    assert proc_registry.read_family(tree, FAMILY) == before
    assert proc_registry.commit_count(tree) == FIRST_COMMIT
    assert proc_registry.uncommitted(tree) == ""


async def test_a_post_at_the_body_limit_is_a_save(board_alone: BoardStack) -> None:
    """The limit is 1 MiB and no less: a body of exactly that size is a save.

    CONTRACT-QUESTION: see `test_a_post_past_the_body_limit_is_refused`.
    """
    tree = board_alone.tree
    edit = {"description": NEW_DESCRIPTION}

    async with board_alone.client() as browser:
        answer = await save_of_size(board_alone, browser, FAMILY, edit, BODY_LIMIT)

    assert answer.status_code == httpx.codes.SEE_OTHER
    assert proc_registry.commit_count(tree) == FIRST_COMMIT + 1
    assert NEW_DESCRIPTION in proc_registry.read_family(tree, FAMILY)
    assert proc_registry.uncommitted(tree) == ""


async def test_a_literal_key_in_the_environment_is_the_key(board_prepared: BoardStack) -> None:
    """`VIEW_ACCESS_KEY` holds the key when no variable names a key file.

    CONTRACT-QUESTION: §8.3 names the key. No contract says where the
    process reads it. Reading taken: the two variables of
    `noticeboard/AGENTS.md`. `VIEW_ACCESS_KEY` holds the key itself, and
    `VIEW_ACCESS_KEY_FILE` names a file that holds it. A final newline of
    the file is no part of the key. The noticeboard removes each white space
    at the two ends of the text. A change costs two scenarios.
    """
    start_board_with(board_prepared, {KEY_FILE_ENV: None, KEY_ENV: LITERAL_KEY})

    async with board_prepared.client(key=LITERAL_KEY) as browser:
        with_key = await browser.get("/")

    async with board_prepared.client(key=None) as stranger:
        without = await stranger.get("/")

    assert with_key.status_code == httpx.codes.OK
    assert without.status_code == httpx.codes.FORBIDDEN


async def test_an_empty_key_file_on_loopback_needs_no_key(board_prepared: BoardStack) -> None:
    """A loopback bind with no key is the run of a developer, and it serves.

    CONTRACT-QUESTION: §8.3 rule 2 refuses a LAN bind with no key. Rule 1
    has no branch that skips the check. No section says what a loopback
    bind with no key serves. Reading taken: rule 1 of the perimeter rules
    in `noticeboard/AGENTS.md`. An empty key is legal on a loopback bind,
    and a page then needs no key. A change costs this scenario.
    """
    replace_secret(board_prepared.tree.view_key_file, "")
    board_prepared.start_board()

    async with board_prepared.client(key=None) as stranger:
        answer = await stranger.get("/")

    assert answer.status_code == httpx.codes.OK


async def test_a_key_file_with_a_final_newline_is_the_key(board_prepared: BoardStack) -> None:
    """An editor ends a file with a newline. The newline is no part of the key.

    CONTRACT-QUESTION: see `test_a_literal_key_in_the_environment_is_the_key`.
    """
    replace_secret(board_prepared.tree.view_key_file, OTHER_KEY + "\n")
    board_prepared.start_board()

    async with board_prepared.client(key=OTHER_KEY) as browser:
        with_key = await browser.get("/")

    async with board_prepared.client(key=None) as stranger:
        without = await stranger.get("/")

    assert with_key.status_code == httpx.codes.OK
    assert without.status_code == httpx.codes.FORBIDDEN


@pytest.mark.parametrize(
    ("name", "value"), [(PORT_ENV, "abc"), (PAGE_SIZE_ENV, "0")], ids=["port", "page-size"]
)
def test_a_value_that_is_not_valid_refuses_to_start(
    board_prepared: BoardStack, name: str, value: str
) -> None:
    """Fail closed. A config that is not valid refuses to start.

    CONTRACT-QUESTION: no contract names `VIEW_PORT` or `VIEW_PAGE_SIZE`, a
    range for one, or an exit code for a value outside it. Reading taken: a
    port that is no number and a page size of 0 each refuse to start, with
    a code that is not 0, as for each other refused start of this suite.
    The noticeboard writes one line on stderr that names the variable. Its
    code is the code of each other start that its config refuses. A change
    to one fixed code costs one assertion here.
    """
    tree = board_prepared.tree
    port = board_prepared.supervisor.free_port()
    env = changed_env(tree, LOOPBACK, port, {name: value})

    child = board_prepared.spawn(Service.NOTICEBOARD, env)

    assert child.wait(EXIT_DEADLINE_S) != 0, child.output()
    # Only the page-size case gives the service this port.
    assert not is_listening(TcpAddress(port))


# ------------------------------------------------------------ the JSON check


def test_the_json_check_takes_a_strict_text() -> None:
    """The check of the refusal scenario takes a strict JSON text in UTF-8."""
    assert is_json(STRICT_BODY.encode("utf-8"))


@pytest.mark.parametrize("body", LAX_BODIES.values(), ids=LAX_BODIES.keys())
def test_the_json_check_refuses_a_lax_text(body: bytes) -> None:
    """A body that only a lax reader takes is no JSON text for this suite."""
    assert not is_json(body)
