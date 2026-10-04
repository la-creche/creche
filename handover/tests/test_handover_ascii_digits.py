"""A number of a version, of a contract version or of a tag has the digits
0 to 9 (contract 06 \u00a72 and \u00a73).

In a Python pattern over text, `\\d` also matches each other decimal digit
of Unicode, and `int` reads such a digit. Each pattern of this package
names the digits 0 to 9, so a text with another digit is no number.
"""

from __future__ import annotations

import json
import re

import pytest
from handover.allocate import TagOutcome, plan_tags
from handover.catalog import Repo
from handover.errors import Refusal, RefusalCode
from handover.executor import provenance
from handover.executor import request as executor_request
from handover.executor.provenance import check_monotonic
from handover.executor.request import parse_request
from handover.executor.source import newest_tagged_version
from handover.manifest import parse_manifest
from handover.state import parse_state
from handover_executor_fixtures import REQUEST_ID, request_body
from handover_fixtures import FIXTURE_MANIFEST_VERSION, manifest_text

from handover import allocate, manifest, state

#: Two decimal digits of Unicode that are not ASCII. Python reads each as 1.
ARABIC_ONE = "\u0661"
FULLWIDTH_ONE = "\uff11"
DIGITS = (ARABIC_ONE, FULLWIDTH_ONE)

VERSION_PATTERNS = (
    manifest.VERSION_RE,
    state.VERSION_RE,
    executor_request.VERSION_RE,
    provenance.VERSION_RE,
)
CONTRACT_VERSION_PATTERNS = (manifest.CONTRACT_VERSION_RE, state.CONTRACT_VERSION_RE)


# -- each pattern --------------------------------------------------------------


@pytest.mark.parametrize("digit", DIGITS)
@pytest.mark.parametrize("pattern", VERSION_PATTERNS)
def test_a_version_pattern_takes_ascii_digits_only(pattern: re.Pattern[str], digit: str) -> None:
    assert pattern.fullmatch("1.2.1")
    for text in (f"{digit}.2.1", f"1.{digit}.1", f"1.2.{digit}", f"1.2.1{digit}"):
        assert pattern.fullmatch(text) is None


@pytest.mark.parametrize("digit", DIGITS)
@pytest.mark.parametrize("pattern", CONTRACT_VERSION_PATTERNS)
def test_a_contract_version_pattern_takes_ascii_digits_only(
    pattern: re.Pattern[str], digit: str
) -> None:
    assert pattern.fullmatch("0.6")
    for text in (f"{digit}.6", f"0.{digit}", f"0.6{digit}"):
        assert pattern.fullmatch(text) is None


@pytest.mark.parametrize("digit", DIGITS)
def test_the_tag_pattern_takes_ascii_digits_only(digit: str) -> None:
    assert allocate.TAG_RE.fullmatch("chaperone-v1.2.1")
    for version in (f"{digit}.2.1", f"1.{digit}.1", f"1.2.{digit}"):
        assert allocate.TAG_RE.fullmatch(f"chaperone-v{version}") is None


# -- a tag ---------------------------------------------------------------------


@pytest.mark.parametrize("digit", DIGITS)
def test_a_tag_with_another_digit_is_no_version(digit: str) -> None:
    """The reader of the newest version gave `1.2.1` for such a tag."""
    assert newest_tagged_version([f"chaperone-v1.2.{digit}"], "chaperone") is None


def test_a_tag_with_another_digit_is_not_the_newest_version() -> None:
    """Python reads ARABIC-INDIC DIGIT NINE as 9. The reader gave `9.0.0`."""
    names = ["chaperone-v0.1.0", "chaperone-v\u0669.0.0"]

    assert newest_tagged_version(names, "chaperone") == "0.1.0"


def test_the_allocator_does_not_count_from_such_a_tag() -> None:
    """The next tag follows the newest tag that is a version."""
    tags = ("chaperone-v0.1.0", "chaperone-v\u0669.0.0")
    plans = plan_tags(("chaperone/src/x.py",), (), tags, (), Repo.AGENT_CONTROL)
    plan = next(one for one in plans if one.component == "chaperone")

    assert plan.outcome is TagOutcome.CREATE
    assert plan.tag == "chaperone-v0.1.1"
    assert plan.from_version == "0.1.0"


def test_such_a_tag_alone_leaves_the_component_untagged() -> None:
    assert "chaperone" in allocate.untagged((f"chaperone-v{ARABIC_ONE}.0.0",), Repo.AGENT_CONTROL)


# -- each parser ---------------------------------------------------------------


@pytest.mark.parametrize("digit", DIGITS)
def test_a_manifest_version_with_another_digit_is_refused(digit: str) -> None:
    text = manifest_text("chaperone").replace(
        f'manifest_version: "{FIXTURE_MANIFEST_VERSION}"', f'manifest_version: "0.{digit}"'
    )
    assert digit in text
    with pytest.raises(Refusal) as caught:
        parse_manifest(text, "chaperone/component.yaml")

    assert caught.value.code is RefusalCode.MANIFEST
    assert "manifest_version" in caught.value.detail


@pytest.mark.parametrize("digit", DIGITS)
@pytest.mark.parametrize(
    "document",
    [
        {"live": {"chaperone": "1.2.{digit}"}},
        {"latest": {"chaperone": "{digit}.2.1"}},
        {"provided": {"pep-grant": "0.{digit}"}},
        {"provided": {"pep-grant": "{digit}.4"}},
    ],
)
def test_a_state_number_with_another_digit_is_refused(
    document: dict[str, dict[str, str]], digit: str
) -> None:
    text = json.dumps(document, ensure_ascii=False).replace("{digit}", digit)
    with pytest.raises(Refusal) as caught:
        parse_state(text, "live-state.json")

    assert caught.value.code is RefusalCode.STATE


@pytest.mark.parametrize("digit", DIGITS)
def test_a_requested_version_with_another_digit_is_refused(digit: str) -> None:
    body = request_body({"chaperone": f"1.2.{digit}"})
    with pytest.raises(Refusal) as caught:
        parse_request(json.dumps(body).encode("utf-8"), REQUEST_ID)

    assert caught.value.code is RefusalCode.REQUEST


@pytest.mark.parametrize("digit", DIGITS)
def test_the_monotonic_rule_does_not_compare_such_a_version(digit: str) -> None:
    """The rule read `1.2.<digit>` as `1.2.1` and passed it over `1.2.0`."""
    with pytest.raises(Refusal) as caught:
        check_monotonic("chaperone", f"1.2.{digit}", "1.2.0")

    assert caught.value.code is RefusalCode.MONOTONIC
