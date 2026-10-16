"""The edit form of a TOML family.

The form holds the markup of the file that the registry read. A block
control shows TOML text, and the form writes a TOML document. A TOML text
is not the join of its parts, so the form reads each block with the one
reader, puts each value into one mapping, and `tomlout` writes the mapping.
"""

from __future__ import annotations

from agent_family import FamilyFile, Index, Markup, parse_family
from noticeboard.familyform import (
    Control,
    Form,
    document_of,
    form_of,
    markup_of,
    parse_posted,
    with_posted,
)
from noticeboard_helpers import CHAT_COMMENT, CHAT_FAMILY_TOML

CHAT_INDEX = Index(kinds={"chat": "attended"})


def family(text: str = CHAT_FAMILY_TOML) -> FamilyFile:
    parsed, issues = parse_family(text, Markup.TOML)
    assert issues == [], issues
    assert parsed is not None
    return parsed


def toml_form() -> Form:
    return form_of(family(), CHAT_INDEX, Markup.TOML)


def posted_from(form: Form) -> dict[str, str]:
    """Every free control, echoed back as a browser would post it."""
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


def value_of(form: Form, name: str) -> str:
    found = form.field(name)
    assert found is not None, name
    return found.value


def test_the_markup_of_a_file_comes_from_its_name() -> None:
    assert markup_of("families/chat/family.toml") is Markup.TOML
    assert markup_of("families/chat/family.yaml") is Markup.YAML


def test_the_form_of_a_toml_family_holds_its_markup() -> None:
    assert toml_form().markup is Markup.TOML
    assert form_of(family(), CHAT_INDEX).markup is Markup.YAML


def test_a_block_shows_toml_text_with_its_key() -> None:
    form = toml_form()

    assert value_of(form, "files") == (
        'files = [\n    { path = "/srv/agents/vault", mode = "ro" },\n]\n'
    )
    assert value_of(form, "tools") == "tools = {}\n"


def test_a_block_says_that_it_is_toml() -> None:
    found = toml_form().field("files")

    assert found is not None
    assert found.note == "TOML, including the key line"


def test_a_block_with_no_value_shows_no_text() -> None:
    """TOML has no null. A field with no value has no key."""
    assert value_of(toml_form(), "triggers") == ""
    assert value_of(toml_form(), "quiet") == ""


def test_the_form_round_trips_a_toml_family_unchanged() -> None:
    before = family()
    form = toml_form()

    after, issues = parse_posted(form, posted_from(form))

    assert issues == []
    assert after == before


def test_an_edit_reaches_the_toml_document() -> None:
    form = toml_form()
    posted = posted_from(form)
    posted["description"] = "the house assistant, rewritten"
    posted["sandbox.cpus"] = "6"

    after, issues = parse_posted(form, posted)

    assert issues == []
    assert after is not None
    assert after.description == "the house assistant, rewritten"
    assert after.sandbox.cpus == 6


def test_the_toml_document_has_no_table_line_and_keeps_the_schema_order() -> None:
    form = toml_form()
    text, problem = document_of(form, posted_from(form))

    assert problem == ""
    assert [line for line in text.splitlines() if line.startswith("[")] == []
    assert text.index("name =") < text.index("kind =") < text.index("description =")


def test_a_block_with_a_table_line_takes_no_later_field() -> None:
    """A join of the texts would put each later field into the table
    `tools`. The form reads the block alone."""
    form = toml_form()
    posted = posted_from(form)
    posted["tools"] = '[tools]\nkagi = "all"\n'
    posted["egress"] = "example.test\n"

    after, issues = parse_posted(form, posted)

    assert issues == []
    assert after is not None
    assert after.tools == {"kagi": "all"}
    assert after.egress == ["example.test"]
    assert after.skills == []


def test_a_block_with_a_key_two_times_is_a_problem_of_the_form() -> None:
    form = toml_form()
    posted = posted_from(form)
    posted["tools"] = 'tools.kagi = "all"\ntools.kagi = "all"\n'

    text, problem = document_of(form, posted)
    after, issues = parse_posted(form, posted)

    assert text == ""
    assert problem == (
        "the tools block: the text is not TOML 1.0, or it nests deeper than 8 levels"
    )
    assert after is None
    assert [(one.loc, one.msg) for one in issues] == [("form", problem)]


def test_a_block_that_the_reader_refuses_is_a_problem_of_the_form() -> None:
    form = toml_form()
    posted = posted_from(form)
    posted["files"] = 'files = [ { path = "/srv/agents/vault"\n'

    _, problem = document_of(form, posted)

    assert problem.startswith("the files block: ")


def test_a_block_that_names_another_key_is_refused() -> None:
    form = toml_form()
    posted = posted_from(form)
    posted["files"] = 'name = "stolen"\n'

    text, problem = document_of(form, posted)

    assert text == ""
    assert problem == "the files block must hold the key 'files' and no other"


def test_a_block_with_a_second_key_is_refused() -> None:
    form = toml_form()
    posted = posted_from(form)
    posted["files"] = "files = []\nshell = true\n"

    _, problem = document_of(form, posted)

    assert problem == "the files block must hold the key 'files' and no other"


def test_an_empty_block_gives_no_key() -> None:
    form = toml_form()
    posted = posted_from(form)
    posted["files"] = "# no mounts\n\n"

    after, issues = parse_posted(form, posted)

    assert issues == []
    assert after is not None
    assert after.files == []


def test_the_form_of_an_answer_gives_the_document_of_the_post() -> None:
    form = toml_form()
    posted = posted_from(form)
    posted["description"] = "  the house assistant, rewritten  "
    posted["files"] = 'files = [{ path = "/srv/agents/vault", mode = "rw" }]'

    again = posted_from(with_posted(form, posted))

    assert document_of(form, again) == document_of(form, posted)
    assert again["files"] == posted["files"]


def test_a_comment_of_a_block_does_not_reach_the_document() -> None:
    """The document of the form is the fallback of a save. `tomlkeep`
    keeps the comments of the file."""
    form = toml_form()
    posted = posted_from(form)
    posted["files"] = f'files = [\n    {CHAT_COMMENT}\n    {{ path = "/srv", mode = "ro" }},\n]\n'

    text, problem = document_of(form, posted)

    assert problem == ""
    assert CHAT_COMMENT not in text
    assert family(text).files[0].path == "/srv"
