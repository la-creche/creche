"""Shared test helpers.

There is deliberately no `conftest.py`: every test directory is prepended
to `sys.path`, so a second module named `conftest` shadows another
package's (`family/tests/family_helpers.py`'s own docstring). Helpers live
in a module with a unique basename and are imported by name."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import yaml
from agent_family import FamilyFile, parse_family
from caregiver.credentials import read_creds, write_creds

from caregiver import paths

FAMILY_YAML: dict[str, Any] = {
    "name": "chat",
    "kind": "attended",
    "description": "Test family.",
    "model": {"router": "agent-router", "budget_usd_per_day": 15},
}

#: A grant the validator refuses, in a file that still parses: no
#: `mcp/ghost/server.yaml` declares the server. `registry.families` holds
#: the file all the same, beside a report that is not ok.
REFUSED_TOOLS: dict[str, list[str]] = {"ghost": ["merge_pull_request"]}

#: Before any clock this suite runs under.
LONG_AGO: str = "2020-01-01T00:00:00Z"


def family_yaml(**overrides: object) -> str:
    """The default `chat` family as YAML text, with any field replaced."""
    body = dict(FAMILY_YAML)
    body.update(overrides)
    return yaml.safe_dump(body)


def chat_family(**overrides: object) -> FamilyFile:
    """The same file, parsed. Raises on a shape the parser refuses, so a
    test that mistypes an override fails at the fixture, not three
    assertions later."""
    family, issues = parse_family(family_yaml(**overrides))
    if family is None:
        raise AssertionError(f"the test's own family file does not parse: {issues}")

    return family


def write_registry(root: Path, **overrides: object) -> Path:
    """One family in a fresh (or already-populated) registry root. The
    default shape is a minimal, valid `chat` family; pass `name=...` for a
    second family, or any other field to make it invalid on purpose."""
    body = dict(FAMILY_YAML)
    body.update(overrides)
    name = str(body["name"])
    family_dir = root / "families" / name
    family_dir.mkdir(parents=True, exist_ok=True)
    (family_dir / "family.yaml").write_text(yaml.safe_dump(body), encoding="utf-8")
    (family_dir / "instructions.md").write_text("Be helpful.\n", encoding="utf-8")
    return root


def expire_overlap(state_root: Path, family: str = "chat") -> None:
    """`creds.json` as a graceful rotation leaves it once the grace has run
    out: a previous token, and an overlap that is over. The next `settle`
    has a write to make."""
    path = paths.creds_path(state_root, family)
    creds = read_creds(path)
    if creds is None:
        raise AssertionError(f"{family} has no credentials to expire")

    write_creds(path, replace(creds, previous_pep_token="OLDTOKEN", previous_expires_at=LONG_AGO))
