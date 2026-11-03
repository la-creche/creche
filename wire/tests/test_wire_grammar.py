"""Each wire name type has the pattern of its name in `family/grammar.py`, and
each wire set of names has the members of its set there.

The grammar stays the authority for the family file (decision CH23 rule 5).
Registry CI cannot install `creche-wire`, so the grammar cannot import the
wire types, and this test holds the two equal instead. attendance writes the
`auto` origin, so the test also holds its family pattern and its chain cap
equal to the wire types.

The grammar ends a pattern with `\\Z` for Python's `re`. The schema ends it
with `$`, which the Rust `regex` crate of pydantic-core reads as the end of
the text, so the two match the same texts."""

import re
from pathlib import Path
from typing import Any, cast

import pytest
from agent_family import grammar
from creche_wire import strictjson

from attendance import ids, models

WIRE = Path(__file__).resolve().parents[2] / "rust" / "crates" / "wire"
SCHEMA_FILE = WIRE / "schema" / "wire.json"


def _definitions() -> dict[str, dict[str, Any]]:
    schema = cast(dict[str, Any], strictjson.loads(SCHEMA_FILE.read_bytes(), cap=1 << 20))

    return cast(dict[str, dict[str, Any]], schema["$defs"])


@pytest.mark.parametrize(
    ("wire_name", "pattern"),
    [
        ("FamilyName", grammar.FAMILY_NAME),
        ("WebhookName", grammar.WEBHOOK_NAME),
        ("SkillName", grammar.SKILL_NAME),
        ("ModelAlias", grammar.MODEL_ALIAS),
    ],
)
def test_each_wire_name_has_the_pattern_of_the_grammar(
    wire_name: str, pattern: re.Pattern[str]
) -> None:
    assert _definitions()[wire_name]["pattern"] == _schema_form(pattern)


def test_the_sandbox_grant_has_the_tools_of_the_grammar() -> None:
    grant = _definitions()["SandboxGrant"]

    assert sorted(grant["properties"]) == sorted(grammar.SandboxTool)
    assert sorted(grant["required"]) == sorted(grammar.SandboxTool)


def test_the_system_prompt_has_the_modes_of_the_grammar() -> None:
    mode = _definitions()["FamilyConfig"]["properties"]["system_prompt"]

    assert mode["enum"] == list(grammar.SystemPrompt)


def test_the_family_name_has_the_pattern_of_attendance() -> None:
    assert _definitions()["FamilyName"]["pattern"] == _schema_form(ids._FAMILY_RE)


def test_the_dispatch_chain_has_the_cap_of_attendance() -> None:
    assert _definitions()["DispatchChain"]["maxItems"] == models.CHAIN_MAX_FAMILIES


def _schema_form(pattern: re.Pattern[str]) -> str:
    assert pattern.pattern.endswith("\\Z")

    return pattern.pattern.removesuffix("\\Z") + "$"
