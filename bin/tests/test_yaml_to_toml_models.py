"""A converted family file reads to the model of its YAML text.

`bin/yaml-to-toml.py` converts each YAML family file of the fixture registry
of the package `family`, in a child process. The TOML path of
`agent_family.parse_family` reads the TOML text in the strict mode, and the
YAML path reads the YAML text in the lax mode. Both give one model.

The test converts at test time and writes no file. The migration removes the
YAML fixtures, so a walk that finds no YAML file passes."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from agent_family import Markup, parse_family

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "bin" / "yaml-to-toml.py"
FAMILIES = REPO / "family" / "tests" / "fixtures" / "registry" / "families"

#: Each YAML family file of the fixture registry, by its directory name.
YAML_FAMILIES = sorted(path.parent.name for path in FAMILIES.glob("*/family.yaml"))

#: What the walk gets when the list is empty. pytest reports a parametrized
#: test with no value as skipped, and an empty walk must pass.
NO_FILE = "(no YAML family file is left)"


def _converted(path: Path) -> str:
    """The TOML text that the converter prints for one YAML file."""
    done = subprocess.run(
        [sys.executable, str(SCRIPT), str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert done.returncode == 0, done.stderr
    return done.stdout


@pytest.mark.parametrize("name", YAML_FAMILIES or [NO_FILE])
def test_the_toml_text_reads_to_the_model_of_the_yaml_text(name: str) -> None:
    if name == NO_FILE:
        assert YAML_FAMILIES == []
        return

    path = FAMILIES / name / "family.yaml"

    from_yaml, yaml_issues = parse_family(path.read_text(encoding="utf-8"))
    from_toml, toml_issues = parse_family(_converted(path), Markup.TOML)

    assert yaml_issues == []
    assert toml_issues == []
    assert from_yaml is not None
    assert from_toml == from_yaml


def test_the_walk_finds_the_ten_family_files_of_today() -> None:
    """The fixture registry holds 10 YAML family files until the migration
    removes them. Then the list is empty, and that passes too."""
    assert len(YAML_FAMILIES) in (0, 10)
