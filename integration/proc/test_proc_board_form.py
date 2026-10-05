"""The controls of the edit form, and what a save leaves in the registry.

`docs/rework/spec.md` §8.1 and §8.2, and contract 01. A test plays a browser
behind the reverse proxy, as in `test_proc_board_edit.py`:

1. It opens the edit form of one family.
2. It changes one control, and posts every field that a browser posts.
3. It reads the family file and the commit back through `proc_registry.py`.

No scenario here reads the text of a family file for a value. Each one reads
the mapping of `proc_registry.load_family`, so the markup of the file has
one reader.

CONTRACT-QUESTION: §8.1 says that the form comes from the family schema. No
contract gives the control of a field. Reading taken: the form of the
noticeboard as it is. A boolean is a box. A list of names is one name on
each line. A number is a text. Each other shape is a block that starts with
the key line of its field. A field that the kind forbids is a disabled
control with its rule beside it. A change costs the scenario of that control
here.
"""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx
import proc_registry
import pytest
from proc_board import (
    LATER_DESCRIPTION,
    NEW_DESCRIPTION,
    REVIEW,
    VERB_PREVIEW,
    VERB_SAVE,
    BoardStack,
    Browser,
    html_of,
    page_of,
)
from proc_html import CHECKED_VALUE, Element, form_values, table_rows
from proc_registry import FILE_COMMENT, family_path
from proc_tree import ATTENDED, AUTONOMOUS, FAMILY, GROUP_READ_MODE, SECRET_MODE

#: Contract 01 §2: the fields of a thin family or of an autonomous family.
#: The first name is the one control of the mapping `job`.
NOT_FOR_ATTENDED = ("job.timeout", "triggers", "max_running_turns", "quiet")

#: Contract 01 §3.7: a host name, and a host name with a port.
EGRESS = ("one.example.org", "two.example.org:8443")

#: Contract 01 §3.9: `cpus` is an integer from 1 to 8.
NEW_CPUS = 4

#: Contract 01 §3.2: the budget is a number of dollars.
NEW_BUDGET = 20.5

#: Contract 01 §3.5: one verb with no fence, and one Home Assistant call.
NEW_VERBS = {
    "embed": {},
    "ha_call": {"allow": [{"domain": "light", "service": "turn_on", "entity_id": "light.desk"}]},
}

#: Who the noticeboard says made a commit, and the trailer of that commit.
COMMIT_AUTHOR = "noticeboard"
COMMIT_TRAILER = "Via: noticeboard"


async def saved_edit(stack: BoardStack, family: str, **edits: str) -> httpx.Response:
    """Open the form of one family, change the named controls and click save."""
    async with stack.client() as client:
        browser = Browser(stack, client, family)
        await browser.open()
        browser.values.update(edits)

        return await browser.post(VERB_SAVE)


def field_of(form: Element, name: str) -> Element:
    """The one element of the form that holds the control with this name."""
    found = [
        field
        for field in form.all("div", "field")
        if any(control.attrs.get("name") == name for control in controls_of(field))
    ]

    assert len(found) == 1, f"the form holds {len(found)} fields named {name}, not 1"

    return found[0]


def controls_of(element: Element) -> list[Element]:
    return [*element.all("input"), *element.all("textarea")]


async def test_a_save_that_changes_nothing_makes_no_commit(board_alone: BoardStack) -> None:
    """A browser posts the form as it opened it.

    CONTRACT-QUESTION: §8.2 says what a save writes. No section gives the
    answer to a save that changes nothing. Reading taken: the answer of the
    noticeboard as it is, which is the 303 of a save to the page of the
    family, and no commit. The scenario does not read `saved`. A change
    costs the first two assertions here.
    """
    tree = board_alone.tree
    before = proc_registry.read_family(tree, FAMILY)
    commits = proc_registry.commit_count(tree)

    response = await saved_edit(board_alone, FAMILY)

    assert response.status_code == httpx.codes.SEE_OTHER
    assert urlsplit(response.headers["location"]).path == f"/families/{FAMILY}"
    assert proc_registry.commit_count(tree) == commits
    assert proc_registry.read_family(tree, FAMILY) == before
    assert proc_registry.uncommitted(tree) == ""


async def test_the_form_locks_each_field_that_the_kind_forbids(board_alone: BoardStack) -> None:
    """Contract 01 §2: a field of another kind is an error on an attended family.

    §3.12 to §3.15 give the four fields. The form shows each one, so a
    reader sees the rule. A browser posts none of them.
    """
    async with board_alone.client() as client:
        form = (await page_of(client, f"/families/{FAMILY}/edit")).one("form")

    posted = form_values(form)

    for name in NOT_FOR_ATTENDED:
        field = field_of(form, name)
        (control,) = controls_of(field)

        assert "disabled" in control.attrs, name
        assert field.one("p", "rule").text != "", name
        assert name not in posted


async def test_a_post_cannot_change_a_locked_field(board_alone: BoardStack) -> None:
    """Contract 01 §3.1: `kind` never changes.

    A browser posts no disabled control. A post that names `kind` did not
    come from the form. The noticeboard drops the value, so the post is a
    save that changes nothing, with the answer of that scenario.
    """
    tree = board_alone.tree
    before = proc_registry.read_family(tree, FAMILY)
    commits = proc_registry.commit_count(tree)

    response = await saved_edit(board_alone, FAMILY, kind=AUTONOMOUS)

    assert response.status_code == httpx.codes.SEE_OTHER
    assert proc_registry.load_family(tree, FAMILY)["kind"] == ATTENDED
    assert proc_registry.read_family(tree, FAMILY) == before
    assert proc_registry.commit_count(tree) == commits
    assert proc_registry.uncommitted(tree) == ""


async def test_a_ticked_box_saves_true(board_alone: BoardStack) -> None:
    """Contract 01 §3.8: `shell` is a boolean. A browser posts a ticked box as `on`."""
    tree = board_alone.tree

    response = await saved_edit(board_alone, FAMILY, shell=CHECKED_VALUE)

    assert response.status_code == httpx.codes.SEE_OTHER
    assert proc_registry.load_family(tree, FAMILY)["shell"] is True


async def test_two_lines_save_a_list_of_two(board_alone: BoardStack) -> None:
    """Contract 01 §3.7: `egress` is a list of host names. The control holds one on each line.

    `Browser` sends each line end as CR LF, as a browser does. So a save
    that splits the text at LF alone writes a CR into each name.
    """
    tree = board_alone.tree
    typed = "".join(f"{host}\n" for host in EGRESS)

    response = await saved_edit(board_alone, FAMILY, egress=typed)

    assert response.status_code == httpx.codes.SEE_OTHER
    assert proc_registry.load_family(tree, FAMILY)["egress"] == list(EGRESS)


async def test_a_number_control_saves_a_number(board_alone: BoardStack) -> None:
    """Contract 01 §3.9 and §3.2: `cpus` is an integer, and the budget is a number.

    A browser posts each value as text. A file that holds the text does not
    validate, and a file that holds `4.0` is not what the reader typed.
    """
    tree = board_alone.tree
    edits = {"sandbox.cpus": str(NEW_CPUS), "model.budget_usd_per_day": str(NEW_BUDGET)}

    response = await saved_edit(board_alone, FAMILY, **edits)
    saved = proc_registry.load_family(tree, FAMILY)
    cpus = saved["sandbox"]["cpus"]

    assert response.status_code == httpx.codes.SEE_OTHER
    assert (type(cpus), cpus) == (int, NEW_CPUS)
    assert saved["model"]["budget_usd_per_day"] == NEW_BUDGET


async def test_an_edited_block_saves_and_reads_back(board_alone: BoardStack) -> None:
    """Contract 01 §3.5: `verbs` is a mapping of a verb to its fence."""
    tree = board_alone.tree
    typed = proc_registry.block_text("verbs", NEW_VERBS)

    response = await saved_edit(board_alone, FAMILY, verbs=typed)

    assert response.status_code == httpx.codes.SEE_OTHER
    assert proc_registry.load_family(tree, FAMILY)["verbs"] == NEW_VERBS


async def test_a_block_that_names_another_field_writes_nothing(board_alone: BoardStack) -> None:
    """A block holds its own field and no other.

    The control of `verbs` gets the block of `tools`. A document with that
    block holds `tools` two times, or it holds no `verbs`. The answer is a
    report that names the control.
    """
    tree = board_alone.tree
    before = proc_registry.read_family(tree, FAMILY)
    commits = proc_registry.commit_count(tree)
    typed = proc_registry.block_text("tools", {})

    response = await saved_edit(board_alone, FAMILY, verbs=typed)
    refused = [item.text for item in html_of(response).one("ul", "issues").all("li")]

    assert response.status_code == httpx.codes.OK
    assert any("verbs" in line for line in refused)
    assert proc_registry.read_family(tree, FAMILY) == before
    assert proc_registry.commit_count(tree) == commits
    assert proc_registry.uncommitted(tree) == ""


@pytest.mark.parametrize(
    ("control", "typed", "notices"),
    [("description", NEW_DESCRIPTION, 0), ("sandbox.cpus", str(NEW_CPUS), 1)],
    ids=["description", "sandbox-cpus"],
)
async def test_a_preview_says_if_the_change_replaces_the_sandbox(
    board_alone: BoardStack, control: str, typed: str, notices: int
) -> None:
    """Contract 01 §6: a new `description` needs no new sandbox. A new `sandbox.cpus` needs one.

    `notices` is how many sentences about a new sandbox the preview holds.
    """
    async with board_alone.client() as client:
        browser = Browser(board_alone, client, FAMILY)
        await browser.open()
        browser.values[control] = typed
        response = await browser.post(VERB_PREVIEW)

    page = html_of(response)
    changed = [row["field"].text for row in table_rows(page.one("table"))]

    assert response.status_code == httpx.codes.OK
    assert changed == [control]
    assert len(page.all("p", "flag")) == notices


async def test_a_save_that_git_refuses_restores_the_file(board_alone: BoardStack) -> None:
    """§8.2 step 3: when a step of a save fails, the content of before comes back.

    Another program holds the lock of the index, so `git` can write no
    commit.

    CONTRACT-QUESTION: no section gives the answer to a save that `git`
    refuses. Reading taken: the answer of the noticeboard as it is, which is
    the edit page with status 200 and a report. A change costs the first two
    assertions here.
    """
    tree = board_alone.tree
    before = proc_registry.read_family(tree, FAMILY)
    commits = proc_registry.commit_count(tree)

    with proc_registry.index_locked(tree):
        response = await saved_edit(board_alone, FAMILY, description=NEW_DESCRIPTION)

    assert response.status_code == httpx.codes.OK
    assert html_of(response).all("p", "problem") != []
    assert proc_registry.read_family(tree, FAMILY) == before
    assert proc_registry.commit_count(tree) == commits
    assert proc_registry.uncommitted(tree) == ""


async def test_the_commit_of_a_save_names_the_noticeboard(board_alone: BoardStack) -> None:
    """A reader of the history can tell a save of the noticeboard from an edit of a person.

    CONTRACT-QUESTION: §8.2 gives the commit. No contract gives its author
    or a trailer. Reading taken: the commit of the noticeboard as it is,
    with the author name `noticeboard` and the trailer `Via: noticeboard`.
    `noticeboard/AGENTS.md`, Known gaps, lists the trailer. A change costs
    the last two assertions here.
    """
    tree = board_alone.tree

    response = await saved_edit(board_alone, FAMILY, description=NEW_DESCRIPTION)

    assert response.status_code == httpx.codes.SEE_OTHER
    assert proc_registry.head(tree).author == COMMIT_AUTHOR
    assert COMMIT_TRAILER in proc_registry.trailers(tree)


# CONTRACT-QUESTION: §8.2 says that a save writes one commit. No section says
# what a save does with a change that a person left in the checkout. Reading
# taken: the noticeboard as it is. The commit holds the family file alone. A
# file with no commit stays as it was, and so does a changed file and a staged
# file of another family. A change costs the last assertions of the next three
# scenarios.


async def test_a_save_leaves_an_untracked_file_untracked(board_alone: BoardStack) -> None:
    """§8.2: a save is one commit of one family. It takes no other file of the checkout."""
    tree = board_alone.tree
    stray = proc_registry.write_untracked(tree)

    response = await saved_edit(board_alone, FAMILY, description=NEW_DESCRIPTION)

    assert response.status_code == httpx.codes.SEE_OTHER
    assert proc_registry.head(tree).paths == (family_path(tree, FAMILY),)
    assert proc_registry.untracked(tree) == (stray,)


async def test_a_save_leaves_a_changed_file_of_another_family(board_alone: BoardStack) -> None:
    """§8.2: a save is one commit of one family.

    A person changed the instructions of the other family and made no
    commit. A save that commits each changed file of the checkout takes that
    change into its commit.
    """
    tree = board_alone.tree
    other = proc_registry.edit_family_prose(tree, REVIEW)

    response = await saved_edit(board_alone, FAMILY, description=NEW_DESCRIPTION)

    assert response.status_code == httpx.codes.SEE_OTHER
    assert proc_registry.head(tree).paths == (family_path(tree, FAMILY),)
    assert proc_registry.changed(tree) == (other,)
    assert proc_registry.staged(tree) == ()


async def test_a_save_leaves_a_staged_file_of_another_family(board_alone: BoardStack) -> None:
    """§8.2: a save is one commit of one family.

    A person changed the instructions of the other family and put the change
    in the index, for a commit of their own. A save that commits the whole
    index takes that change into its commit.
    """
    tree = board_alone.tree
    other = proc_registry.stage_family_prose(tree, REVIEW)

    response = await saved_edit(board_alone, FAMILY, description=NEW_DESCRIPTION)

    assert response.status_code == httpx.codes.SEE_OTHER
    assert proc_registry.head(tree).paths == (family_path(tree, FAMILY),)
    assert proc_registry.staged(tree) == (other,)
    assert proc_registry.changed(tree) == ()


async def test_a_save_of_an_autonomous_family_keeps_its_triggers(board_alone: BoardStack) -> None:
    """§8.2 step 1: the edit is a patch on the file that is there.

    The form of an autonomous family holds `triggers` as a block
    (contract 01 §3.13), and a browser posts the block as the form shows it.
    The save changes one other field, so the list stays as the file had it.
    """
    tree = board_alone.tree
    before = proc_registry.load_family(tree, REVIEW)

    response = await saved_edit(board_alone, REVIEW, description=NEW_DESCRIPTION)
    after = proc_registry.load_family(tree, REVIEW)

    assert response.status_code == httpx.codes.SEE_OTHER
    assert after["description"] == NEW_DESCRIPTION
    assert after["triggers"] == before["triggers"]
    assert FILE_COMMENT in proc_registry.read_family(tree, REVIEW)


async def test_two_saves_in_a_row_are_two_commits(board_alone: BoardStack) -> None:
    """§8.2. The form after a save holds what the save wrote, and the next save is a new commit."""
    tree = board_alone.tree
    commits = proc_registry.commit_count(tree)

    async with board_alone.client() as client:
        first = Browser(board_alone, client, FAMILY)
        await first.open()
        first.values["description"] = NEW_DESCRIPTION
        saved = await first.post(VERB_SAVE)

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


async def test_a_save_removes_the_file_of_a_save_that_did_not_end(board_alone: BoardStack) -> None:
    """A process that ends inside a save can leave its temporary file.

    CONTRACT-QUESTION: §8.2 step 3 says "write". No contract gives the name
    of a temporary file, and none says who removes one that a save left.
    Reading taken: the noticeboard as it is. The name is a dot, the name of
    the family file, 16 hex digits and `.noticeboard-tmp`. The next save of
    the family removes each such file, and its commit holds the family file
    alone. A change costs the name in `proc_registry.py` and this scenario.
    """
    tree = board_alone.tree
    left = proc_registry.leave_temp_file(tree, FAMILY)

    response = await saved_edit(board_alone, FAMILY, description=NEW_DESCRIPTION)

    assert response.status_code == httpx.codes.SEE_OTHER
    assert not left.exists()
    assert proc_registry.head(tree).paths == (family_path(tree, FAMILY),)
    assert proc_registry.uncommitted(tree) == ""


async def test_a_save_keeps_the_mode_of_the_family_file(board_alone: BoardStack) -> None:
    """A save writes a new file and renames it. The new file has the mode of the old one.

    CONTRACT-QUESTION: no contract gives the mode of a family file after a
    save. Reading taken: the noticeboard as it is, which keeps the mode. On
    the host another account reads the registry, and a narrower mode can
    hide the file from it. A change costs the last assertion here.

    The mode of the scenario is 0640. When the run itself makes a file with
    that mode, the scenario takes 0600, so that a save which keeps no mode
    fails in each run.
    """
    tree = board_alone.tree
    found = proc_registry.family_mode(tree, FAMILY)
    mode = GROUP_READ_MODE if found != GROUP_READ_MODE else SECRET_MODE
    proc_registry.set_family_mode(tree, FAMILY, mode)

    response = await saved_edit(board_alone, FAMILY, description=NEW_DESCRIPTION)

    assert response.status_code == httpx.codes.SEE_OTHER
    assert proc_registry.family_mode(tree, FAMILY) == mode
