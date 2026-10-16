"""The validator holds the grammar of root for a TOML server file.

Root reads each server file again at a release, with the patterns and the
caps of `handover.mcpserver`. The TOML path of the validator applies the
same rules, so a TOML server file that passes the validator passes root.
`agent_family.grammar` holds a copy of each pattern and each cap that only
root held, with the name of its source. A copy of a pattern adds the anchors
`^` and `\\Z`. This test imports both modules and holds each pair equal. It
also holds equal each value that the two readers hold under two names."""

from __future__ import annotations

import re

import pytest
from agent_family import grammar

from handover import mcpserver

#: Each copy, by the name that it has in both modules.
COPIES = (
    "PACKAGE_RE",
    "REPO_RE",
    "ASSET_RE",
    "ENTRYPOINT_RE",
    "SECRET_NAME_RE",
    "PYTHONS",
    "DEFAULT_PYTHON",
    "MAX_TOOLS",
    "MAX_ARGS",
    "MAX_ENV",
    "MAX_FENCES",
    "MAX_FENCE_VALUES",
    "MAX_SHARED",
    "MAX_ARG_CHARS",
    "MAX_VALUE_CHARS",
)

#: Each value that the validator holds under another name: the name in
#: `agent_family.grammar`, then the name in `handover.mcpserver`.
SAME_VALUE = (
    ("DESCRIPTION_MAX", "MAX_DESCRIPTION_CHARS"),
    ("SECRET_NAME_MARKERS", "SECRET_WORDS"),
    ("SECRET_PREFIX", "SECRET_PREFIX"),
    ("ALL_TOOLS", "ALL_TOOLS"),
)

#: Each form of the validator that is a pattern of root: the name of the
#: form in `agent_family.grammar`, then the name of the pattern.
SAME_FORM = (
    ("TOOL_NAME_FORM", "TOOL_NAME_RE"),
    ("ENV_VAR_NAME_FORM", "ENV_NAME_RE"),
    ("EXACT_VERSION_FORM", "VERSION_RE"),
    ("PACKAGE_FORM", "PACKAGE_RE"),
    ("REPO_FORM", "REPO_RE"),
    ("ASSET_FORM", "ASSET_RE"),
    ("ENTRYPOINT_FORM", "ENTRYPOINT_RE"),
    ("SECRET_NAME_FORM", "SECRET_NAME_RE"),
)

#: Each pattern of the validator that root holds under another name.
SAME_ANCHORED = (
    ("SERVER_NAME", "SERVER_NAME_RE"),
    ("SHA256_HEX", "SHA256_RE"),
)


@pytest.mark.parametrize("name", COPIES)
def test_each_copy_equals_its_source_in_root(name: str) -> None:
    copied: object = getattr(grammar, name)
    source: object = getattr(mcpserver, name)

    if isinstance(source, re.Pattern):
        assert isinstance(copied, re.Pattern)
        assert copied.pattern == f"^{source.pattern}\\Z"
        assert copied.flags == source.flags
        return

    assert copied == source
    assert type(copied) is type(source)


@pytest.mark.parametrize(("ours", "root"), SAME_VALUE)
def test_each_value_with_two_names_is_equal(ours: str, root: str) -> None:
    assert getattr(grammar, ours) == getattr(mcpserver, root)


@pytest.mark.parametrize(("form", "pattern"), SAME_FORM)
def test_each_form_of_the_validator_is_the_pattern_of_root(form: str, pattern: str) -> None:
    source: re.Pattern[str] = getattr(mcpserver, pattern)

    assert getattr(grammar, form) == source.pattern


@pytest.mark.parametrize(("ours", "root"), SAME_ANCHORED)
def test_each_anchored_pattern_is_the_pattern_of_root(ours: str, root: str) -> None:
    anchored: re.Pattern[str] = getattr(grammar, ours)
    source: re.Pattern[str] = getattr(mcpserver, root)

    assert anchored.pattern == f"^{source.pattern}\\Z"


def test_the_identity_cap_of_the_validator_is_not_above_the_cap_of_root() -> None:
    """Contract 01b caps an identity at 300 characters. Root caps it at
    1000. The validator counts each character of a TOML identity, so a TOML
    identity that passes the validator passes root."""
    assert grammar.IDENTITY_MAX <= mcpserver.MAX_IDENTITY_CHARS


def test_the_default_of_the_model_is_the_default_of_root() -> None:
    from agent_family.server import InstallBlock

    assert InstallBlock.model_fields["python"].default == mcpserver.DEFAULT_PYTHON
