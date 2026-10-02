"""Every example in contract 01 §8 and contract 01b §8 validates.

If the contract's own files do not pass this validator, one of the two is
wrong."""

from __future__ import annotations

import pytest
from agent_family.registry import load_registry
from agent_family.report import FamilyState
from family_helpers import CONTRACT_FAMILIES, FIXTURES, host, messages

CONTRACT_SERVERS = ("kagi", "github-code", "github-platform", "vikunja-finance")


def test_registry_is_clean_on_host() -> None:
    registry = load_registry(FIXTURES, host())
    for report in registry.all_reports():
        assert report.ok, f"{report.family}: {messages(report)}"

    assert registry.ok


@pytest.mark.parametrize("name", CONTRACT_FAMILIES)
def test_contract_family_validates(name: str) -> None:
    registry = load_registry(FIXTURES, host())
    report = registry.reports[name]
    assert report.errors == 0, messages(report)
    assert report.warnings == 0, messages(report)
    assert registry.families[name].name == name


@pytest.mark.parametrize("name", CONTRACT_SERVERS)
def test_contract_server_validates(name: str) -> None:
    registry = load_registry(FIXTURES, host())
    assert registry.server_reports[name].ok, messages(registry.server_reports[name])
    assert registry.servers[name].name == name


def test_no_host_downgrades_two_checks() -> None:
    """§5.1 and §5.4: without host context each is a warning, and the report
    says which check was downgraded. Nothing else changes."""
    registry = load_registry(FIXTURES)
    report = registry.reports["chat"]
    assert report.errors == 0, messages(report)

    downgraded = {issue.loc for issue in report.issues if issue.downgraded}
    assert downgraded == {"model.router", "files"}
    assert report.state is FamilyState.RECONCILING


def test_unknown_alias_is_an_error_with_a_host() -> None:
    from family_helpers import FakeHost

    registry = load_registry(FIXTURES, FakeHost(("fast",)))
    report = registry.reports["chat"]
    assert "is not an alias LiteLLM serves" in messages(report)
    assert report.state is FamilyState.INVALID


def test_the_registry_carries_its_parts() -> None:
    registry = load_registry(FIXTURES, host())
    assert registry.skills == frozenset({"grill-me", "handoff", "memory-writing"})
    assert registry.instructions("chat").is_file()
    assert registry.skill_file("handoff").is_file()
    assert registry.revision.startswith("reg-")


def test_the_revision_follows_the_bytes(tmp_path: object) -> None:
    from agent_family.registry import revision_of

    assert revision_of(FIXTURES) == revision_of(FIXTURES)
