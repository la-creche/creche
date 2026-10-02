"""Shared test helpers.

There is deliberately no `conftest.py`: every test directory is prepended to
`sys.path`, so a second module named `conftest` shadows another package's.
Helpers live in a module with a unique basename and are imported by name."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from agent_family.model import FamilyFile
from agent_family.parse import parse_family, parse_server
from agent_family.registry import load_registry
from agent_family.report import Issue, Issues, Report
from agent_family.server import McpServerFile
from agent_family.serverrules import check_server
from agent_family.validate import Index, check_family

FIXTURES = Path(__file__).parent / "fixtures" / "registry"

#: The six files contract 01 §8 writes out. The fixture holds more, and
#: `fixtures/registry/families/finance-worker/family.yaml` says why.
CONTRACT_FAMILIES = (
    "chat",
    "code",
    "agent-control",
    "vault-oracle",
    "code-sandbox",
    "scrum-lead",
)


class FakeHost:
    """The host, as the two downgradable checks see it (contract 01 §5.1,
    §5.4). `links` maps a declared path to what it really resolves to."""

    def __init__(self, aliases: tuple[str, ...] = (), links: dict[str, str] | None = None) -> None:
        self._aliases = frozenset(aliases)
        self._links = links or {}

    def model_aliases(self) -> frozenset[str]:
        return self._aliases

    def real_path(self, path: str) -> str:
        return self._links.get(path, path)


LIVE_ALIASES = ("agent-router", "code-router", "fast")


def host() -> FakeHost:
    """A host that serves the three aliases contract 01 §8's files name."""
    return FakeHost(LIVE_ALIASES)


def family_text(fixture: str) -> str:
    return (FIXTURES / "families" / fixture / "family.yaml").read_text(encoding="utf-8")


def load(fixture: str) -> FamilyFile:
    """One fixture family, parsed. Raises in the test when it will not parse,
    which is the one place an exception is the right answer."""
    family, issues = parse_family(family_text(fixture))
    assert family is not None, issues
    return family


def edited(fixture: str, **changes: Any) -> FamilyFile:
    """A fixture family with top-level fields replaced, then re-parsed. Going
    through YAML keeps the test honest: it exercises the real parse.

    The parameter is `fixture`, not `name`, so a test can pass `name=...` in
    `changes` to edit the family's own `name` field without a keyword clash."""
    body: dict[str, Any] = yaml.safe_load(family_text(fixture))
    body.update(changes)
    family, issues = parse_family(yaml.safe_dump(body))
    assert family is not None, issues
    return family


def errors(report: Report) -> tuple[Issue, ...]:
    return tuple(issue for issue in report.issues if issue.severity == "error")


def messages(report: Report) -> str:
    return " | ".join(f"{issue.loc}: {issue.msg}" for issue in report.issues)


#: One read of the fixture registry, for the cross-reference checks a fence
#: test needs (a delegate that exists, a server that is declared). Module
#: level: the fixtures are read-only, so every test shares one parse.
_REGISTRY = load_registry(FIXTURES, host())


def full_index() -> Index:
    """The fixture registry's cross-reference facts, unedited. A fence test
    edits one family with `edited()` and checks it against everyone else."""
    return Index(
        kinds={name: family.kind for name, family in _REGISTRY.families.items()},
        servers=_REGISTRY.servers,
        skills=_REGISTRY.skills,
    )


def check(fixture: str, **changes: Any) -> Report:
    """One fixture family, edited, and checked against the rest of the
    fixture registry with a host present (contract 01 §5)."""
    family = edited(fixture, **changes)
    issues = Issues()
    check_family(family, fixture, full_index(), issues, host())
    return Report(family.name, f"families/{fixture}/family.yaml", issues.frozen())


def check_no_host(fixture: str, **changes: Any) -> Report:
    """Same as `check`, with no host context, for the checks that downgrade
    to a warning without one (contract 01 §5.1, §5.4)."""
    family = edited(fixture, **changes)
    issues = Issues()
    check_family(family, fixture, full_index(), issues, None)
    return Report(family.name, f"families/{fixture}/family.yaml", issues.frozen())


def server_text(fixture: str) -> str:
    return (FIXTURES / "mcp" / fixture / "server.yaml").read_text(encoding="utf-8")


def load_server(fixture: str) -> McpServerFile:
    server, issues = parse_server(server_text(fixture))
    assert server is not None, issues
    return server


def edited_server(fixture: str, **changes: Any) -> McpServerFile:
    """Same trade as `edited`: the parameter is `fixture`, not `name`, so a
    test can override the server's own `name` field."""
    body: dict[str, Any] = yaml.safe_load(server_text(fixture))
    body.update(changes)
    server, issues = parse_server(yaml.safe_dump(body))
    assert server is not None, issues
    return server


def check_server_rules(fixture: str, **changes: Any) -> Report:
    """One fixture server file, edited, and checked by itself (contract 01b's
    single-file rules need no registry context)."""
    server = edited_server(fixture, **changes)
    issues = Issues()
    check_server(server, fixture, issues)
    return Report(server.name, f"mcp/{fixture}/server.yaml", issues.frozen())
