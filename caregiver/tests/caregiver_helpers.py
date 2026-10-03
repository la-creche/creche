"""Shared test helpers.

There is deliberately no `conftest.py`: every test directory is prepended
to `sys.path`, so a second module named `conftest` shadows another
package's (`family/tests/family_helpers.py`'s own docstring). Helpers live
in a module with a unique basename and are imported by name."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from agent_family import FamilyFile, parse_family

FAMILY_YAML: dict[str, Any] = {
    "name": "chat",
    "kind": "attended",
    "description": "Test family.",
    "model": {"router": "agent-router", "budget_usd_per_day": 15},
}


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
