"""The schema-generated edit form.

Two properties carry the whole design. The controls come from the schema,
so a field added there appears with no edit to the view. A lock comes
from asking the validator, so a rule that changes changes the page.
"""

from __future__ import annotations

from agent_family import FamilyFile, Index, Kind, parse_family
from agent_view.familyform import Control, Form, document_of, form_of, locks_of, parse_posted
from view_helpers import CHAT_FAMILY_YAML, SCRUM_FAMILY_YAML


def family(text: str = CHAT_FAMILY_YAML) -> FamilyFile:
    parsed, issues = parse_family(text)
    assert issues == [], issues
    assert parsed is not None
    return parsed


CHAT_INDEX = Index(kinds={"chat": "attended"})
SCRUM_INDEX = Index(kinds={"chat": "attended", "scrum-lead": "autonomous"})


def control_of(form: Form, name: str) -> Control:
    found = form.field(name)
    assert found is not None, name
    return found.control


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


def test_every_schema_field_gets_a_control() -> None:
    form = form_of(family(), CHAT_INDEX)
    names = {one.name for one in form.fields}

    assert "description" in names
    assert "model.router" in names
    assert "model.budget_usd_per_day" in names
    assert "sandbox.cpus" in names
    assert "shell" in names
    assert "egress" in names
    assert "files" in names


def test_the_annotation_picks_the_control() -> None:
    form = form_of(family(), CHAT_INDEX)

    assert control_of(form, "shell") is Control.CHECKBOX
    assert control_of(form, "model.budget_usd_per_day") is Control.NUMBER
    assert control_of(form, "description") is Control.TEXT
    assert control_of(form, "egress") is Control.LINES
    assert control_of(form, "files") is Control.BLOCK


def test_a_fence_block_is_never_flattened() -> None:
    """`verbs` holds fences. A text box would drop one on save."""
    form = form_of(family(), CHAT_INDEX)

    assert control_of(form, "verbs") is Control.BLOCK
    assert form.field("verbs.ha_call") is None


def test_the_name_is_locked_with_the_rule_as_its_label() -> None:
    rules = locks_of(family(), CHAT_INDEX)

    assert "name" in rules
    assert "new family" in rules["name"]


def test_the_kind_is_locked_with_the_rule_as_its_label() -> None:
    rules = locks_of(family(), CHAT_INDEX)

    assert "kind" in rules
    assert "refused" in rules["kind"]


def test_an_attended_family_cannot_take_triggers() -> None:
    """The lock text is the validator's own sentence, not a copy of it."""
    rules = locks_of(family(), CHAT_INDEX)

    assert rules["triggers"] == "'triggers' is autonomous only; this family is 'attended'"
    assert "autonomous only" in rules["max_running_turns"]
    assert "thin only" in rules["job"]


def test_an_autonomous_family_may_take_its_own_fields() -> None:
    rules = locks_of(family(SCRUM_FAMILY_YAML), SCRUM_INDEX)

    assert "triggers" not in rules
    assert "max_running_turns" not in rules
    assert "job" in rules


def test_a_locked_parent_locks_its_children_with_the_same_sentence() -> None:
    form = form_of(family(), CHAT_INDEX)
    timeout = form.field("job.timeout")

    assert timeout is not None
    assert "thin only" in timeout.locked


def test_the_form_round_trips_a_family_unchanged() -> None:
    before = family()
    form = form_of(before, CHAT_INDEX)

    after, issues = parse_posted(form, posted_from(form))

    assert issues == []
    assert after == before


def test_an_edit_reaches_the_document() -> None:
    form = form_of(family(), CHAT_INDEX)
    posted = posted_from(form)
    posted["description"] = "the house assistant, rewritten"

    after, issues = parse_posted(form, posted)

    assert issues == []
    assert after is not None
    assert after.description == "the house assistant, rewritten"


def test_a_locked_field_is_dropped_from_the_saved_document() -> None:
    """A disabled input posts nothing. Keeping the old value would fail
    the save on the rule the page is showing."""
    form = form_of(family(), CHAT_INDEX)
    text, problem = document_of(form, posted_from(form))

    assert problem == ""
    assert "triggers:" not in text
    assert "job:" not in text


def test_a_forged_value_for_a_locked_field_is_ignored() -> None:
    """The lock is recomputed on the server, so a hand-built POST cannot
    smuggle a value past it."""
    form = form_of(family(), CHAT_INDEX)
    posted = posted_from(form)
    posted["max_running_turns"] = "4"

    text, _ = document_of(form, posted)

    assert "max_running_turns" not in text


def test_a_checkbox_that_posts_nothing_reads_false() -> None:
    form = form_of(family(), CHAT_INDEX)
    posted = posted_from(form)
    posted.pop("shell", None)

    after, issues = parse_posted(form, posted)

    assert issues == []
    assert after is not None
    assert after.shell is False


def test_a_lines_control_reads_one_name_per_line() -> None:
    form = form_of(family(), CHAT_INDEX)
    posted = posted_from(form)
    posted["egress"] = "api.kagi.com\n\n  files.example.org  \n"

    after, issues = parse_posted(form, posted)

    assert issues == []
    assert after is not None
    assert after.egress == ["api.kagi.com", "files.example.org"]


def test_a_block_that_names_another_key_is_refused() -> None:
    form = form_of(family(), CHAT_INDEX)
    posted = posted_from(form)
    posted["files"] = 'name: "stolen"\n'

    text, problem = document_of(form, posted)

    assert text == ""
    assert "must start with 'files:'" in problem


def test_a_broken_block_becomes_a_report_not_an_exception() -> None:
    form = form_of(family(), CHAT_INDEX)
    posted = posted_from(form)
    posted["files"] = "files:\n  - path: [unclosed\n"

    after, issues = parse_posted(form, posted)

    assert after is None
    assert issues


def test_the_document_keeps_the_schema_field_order() -> None:
    form = form_of(family(), CHAT_INDEX)
    text, _ = document_of(form, posted_from(form))

    assert text.index("name:") < text.index("kind:") < text.index("description:")


def test_the_kinds_the_form_knows_come_from_the_grammar() -> None:
    """A kind added to the grammar must not need an edit here."""
    assert {one.value for one in Kind} == {"attended", "thin", "autonomous"}
