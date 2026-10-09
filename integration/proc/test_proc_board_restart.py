"""The noticeboard across two starts on one root and one port.

A release stops the service and starts it again
(`creche-noticeboard.service`). The registry stays, and the browser of the
operator stays open. Each scenario here stops the first process with
`SIGTERM`, as the unit does. Then it starts a second process with the same
environment.

The second process gets nothing from the first one. It reads the registry,
and it reads the cookie that the browser sends.
"""

from __future__ import annotations

import httpx
import proc_registry
from proc_board import LATER_DESCRIPTION, NEW_DESCRIPTION, VERB_SAVE, BoardStack, Browser
from proc_tree import FAMILY


async def test_the_next_start_reads_a_saved_family(board_alone: BoardStack) -> None:
    """`docs/rework/spec.md` §8.2: a save is a commit in the registry, and the registry stays.

    The form of the second process holds what the first process saved. A
    save through the second process is the next commit of the same history.
    """
    tree = board_alone.tree
    commits = proc_registry.commit_count(tree)

    async with board_alone.client() as client:
        first = Browser(board_alone, client, FAMILY)
        await first.open()
        first.values["description"] = NEW_DESCRIPTION
        saved = await first.post(VERB_SAVE)

    board_alone.restart_board()

    async with board_alone.client() as client:
        second = Browser(board_alone, client, FAMILY)
        await second.open()
        shown = second.values["description"]
        second.values["description"] = LATER_DESCRIPTION
        saved_again = await second.post(VERB_SAVE)

    assert saved.status_code == httpx.codes.SEE_OTHER
    assert shown == NEW_DESCRIPTION
    assert saved_again.status_code == httpx.codes.SEE_OTHER
    assert proc_registry.commit_count(tree) == commits + 2
    assert proc_registry.load_family(tree, FAMILY)["description"] == LATER_DESCRIPTION
    assert proc_registry.uncommitted(tree) == ""


async def test_a_form_of_one_start_posts_to_the_next_start(board_alone: BoardStack) -> None:
    """A browser holds an open form while a release restarts the service.

    The browser then posts the fields and the cookie that the first process
    gave it.

    CONTRACT-QUESTION: `docs/rework/spec.md` §8.3 rule 3 puts the CSRF token
    in a cookie and in a hidden field. No section says when a token ends.
    Reading taken: the noticeboard as it is. The token is valid while the
    cookie and the field hold the same value, and a restart ends no token.
    A token that ends with its process costs this scenario, and the operator
    then loses each edit that is open at a release.
    """
    tree = board_alone.tree
    commits = proc_registry.commit_count(tree)

    async with board_alone.client() as client:
        browser = Browser(board_alone, client, FAMILY)
        await browser.open()

    browser.values["description"] = NEW_DESCRIPTION
    board_alone.restart_board()

    async with board_alone.client() as client:
        browser.client = client
        response = await browser.post(VERB_SAVE)

    assert response.status_code == httpx.codes.SEE_OTHER
    assert proc_registry.commit_count(tree) == commits + 1
    assert proc_registry.load_family(tree, FAMILY)["description"] == NEW_DESCRIPTION
