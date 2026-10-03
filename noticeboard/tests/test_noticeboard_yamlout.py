"""The emitter that writes `family.yaml` back.

The real check is the round trip: whatever this writes, `agent_family`
must read back as the same family. A quoting bug shows up here, not in
the registry.
"""

from __future__ import annotations

from typing import Any

from agent_family import parse_family
from noticeboard.yamlout import to_yaml
from noticeboard_helpers import CHAT_FAMILY_YAML


def reparse(document: dict[str, Any]):
    family, issues = parse_family(to_yaml(document))
    assert issues == [], to_yaml(document)
    assert family is not None
    return family


def base(**extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "chat",
        "kind": "attended",
        "description": "the house assistant",
        "model": {"router": "agent-router", "budget_usd_per_day": 15.0},
    }
    body.update(extra)
    return body


def test_a_family_survives_the_round_trip() -> None:
    family = reparse(base(shell=False, egress=[], skills=[]))

    assert family.name == "chat"
    assert family.model.budget_usd_per_day == 15.0


def test_a_description_holding_a_colon_survives() -> None:
    """A plain scalar would split this into a mapping."""
    family = reparse(base(description="alerts: boiler, car, post"))

    assert family.description == "alerts: boiler, car, post"


def test_a_value_that_reads_as_a_number_stays_a_string() -> None:
    family = reparse(base(description="1.10"))

    assert family.description == "1.10"


def test_a_value_that_reads_as_a_boolean_stays_a_string() -> None:
    """YAML 1.1 turns a bare `no` into false. A quoted one stays text."""
    family = reparse(base(description="no"))

    assert family.description == "no"


def test_a_path_holding_a_hash_is_not_a_comment() -> None:
    mounts = [{"path": "/srv/agents/vault/notes#1", "mode": "ro"}]
    family = reparse(base(files=mounts))

    assert family.files[0].path == "/srv/agents/vault/notes#1"


def test_a_list_of_mappings_reads_back_in_order() -> None:
    mounts = [
        {"path": "/srv/agents/vault", "mode": "ro"},
        {"path": "/srv/agents/work/code-sandbox", "mode": "rw"},
    ]
    family = reparse(base(files=mounts))

    assert [one.path for one in family.files] == [one["path"] for one in mounts]
    assert [one.mode for one in family.files] == ["ro", "rw"]


def test_a_list_item_opens_on_its_dash() -> None:
    """The idiomatic block form, so a person can still read the file."""
    text = to_yaml(base(files=[{"path": "/srv/agents/vault", "mode": "ro"}]))

    assert '  - path: "/srv/agents/vault"\n    mode: "ro"\n' in text


def test_an_empty_container_is_not_null() -> None:
    """`egress: []` grants nothing. `egress:` with no body is null, which
    is a different document."""
    text = to_yaml(base(egress=[], tools={}))

    assert "egress: []" in text
    assert "tools: {}" in text


def test_null_and_the_booleans_are_plain() -> None:
    text = to_yaml(base(shell=True, triggers=None))

    assert "shell: true" in text
    assert "triggers: null" in text


def test_a_nested_grant_block_round_trips() -> None:
    body = base(
        tools={"kagi": ["kagi_search_fetch"], "ha": "all"},
        verbs={"ha_call": {"allow": [{"domain": "light", "service": "turn_on"}]}},
    )
    family = reparse(body)

    assert family.tool_names("kagi") == ("kagi_search_fetch",)
    assert family.grants_all("ha")
    assert family.verbs.ha_call is not None
    assert family.verbs.ha_call.allow[0].domain == "light"


def test_the_fixture_family_re_emits_as_the_same_family() -> None:
    """The proof that matters: read a real file, write it, read it again."""
    first, issues = parse_family(CHAT_FAMILY_YAML)
    assert issues == []
    assert first is not None

    second, again = parse_family(to_yaml(first.model_dump(mode="json")))

    assert again == []
    assert second == first


def test_every_document_ends_in_one_newline() -> None:
    text = to_yaml(base())

    assert text.endswith("\n")
    assert not text.endswith("\n\n")
