"""Each wire name type has the pattern of its name in `family/grammar.py`, and
each wire set of names has the members of its set there. Each size of the
family VM shape has the range of its size there. The host name of the egress
allowlist takes the hosts of the grammar in lower case.

The grammar stays the authority for the family file (decision CH23 rule 5).
Registry CI cannot install `creche-wire`, so the grammar cannot import the
wire types, and this test holds the two equal instead. attendance writes the
`auto` origin, so the test also holds its family pattern and its chain cap
equal to the wire types. caregiver mints the family token, so the test holds
its form equal to the wire type too.

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
from caregiver import credentials

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


def test_the_family_shape_has_the_flavors_of_the_grammar() -> None:
    flavor = _definitions()["FamilyShape"]["properties"]["flavor"]

    assert flavor["enum"] == list(grammar.SandboxFlavor)


@pytest.mark.parametrize(
    ("wire_name", "low", "high"),
    [
        ("FamilyCpus", grammar.CPUS_MIN, grammar.CPUS_MAX),
        ("FamilyMemoryMib", grammar.MEMORY_MIN_MB, grammar.MEMORY_MAX_MB),
    ],
)
def test_each_shape_size_has_the_range_of_the_grammar(wire_name: str, low: int, high: int) -> None:
    size = _definitions()[wire_name]

    assert (size["minimum"], size["maximum"]) == (low, high)


def test_the_grammar_counts_memory_in_mib() -> None:
    """The wire counts the guest RAM in MiB. The grammar names its unit MB
    and counts `1g` as 1024 of it, so the two units are one."""
    assert grammar.memory_mb("1g") == 1024


#: Hosts of the family file, each good or bad to the grammar: each label
#: form, the IPv4 literal and its near misses, a wildcard, a trailing dot,
#: upper case and an IPv6 literal.
HOSTS = [
    "github.com",
    "a",
    "files.pythonhosted.org",
    "a-b--c.example",
    "GitHub.COM",
    "123",
    "1.2.3",
    "1.2.3.4",
    "999.0.0.1",
    "1.2.3.4.5",
    "1234.1.1.1",
    "1.2.3.4a",
    "1.2-3.4.5",
    "x.1.2.3.4",
    "0x7f.1",
    "github.com.",
    ".github.com",
    "github..com",
    "*.github.com",
    "*",
    "-github.com",
    "github-.com",
    "git_hub.com",
    "gith\u00fcb.com",
    "::1",
    "",
]


@pytest.mark.parametrize("host", HOSTS)
def test_egress_host_pattern_agrees_with_the_grammar(host: str) -> None:
    """A host that the grammar takes passes the wire pattern in lower case
    (decision CH69 rule 5: caregiver writes lower case). A host that the
    grammar refuses fails it. An upper-case host fails it as it is."""
    pattern = re.compile(_definitions()["HostName"]["pattern"])
    good = grammar.egress_problem(host) is None

    assert (pattern.fullmatch(host.lower()) is not None) == good
    if host != host.lower():
        assert pattern.fullmatch(host) is None


def test_the_family_token_has_the_form_that_caregiver_mints() -> None:
    assert _definitions()["PepToken"]["pattern"] == f"^{credentials._TOKEN.pattern}$"


def test_the_family_name_has_the_pattern_of_attendance() -> None:
    assert _definitions()["FamilyName"]["pattern"] == _schema_form(ids._FAMILY_RE)


def test_the_dispatch_chain_has_the_cap_of_attendance() -> None:
    assert _definitions()["DispatchChain"]["maxItems"] == models.CHAIN_MAX_FAMILIES


def _schema_form(pattern: re.Pattern[str]) -> str:
    assert pattern.pattern.endswith("\\Z")

    return pattern.pattern.removesuffix("\\Z") + "$"
