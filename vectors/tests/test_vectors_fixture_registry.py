"""The TOML fixture registry of the vectors is a copy of the fixture registry
of the package `family`.

`vectors/fixtures/registry-toml` holds one twin of each file of
`family/tests/fixtures/registry`. The twin of `family.yaml` is `family.toml`
in the same directory, and `bin/yaml-to-toml.py --check` proves the pair: the
TOML text holds the values and the count of comments of the YAML text. The
twin of each other file has the same path and the same bytes.

The proof runs the converter in a child process, as a person runs it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from agent_family.registry import FAMILY_FILE, FAMILY_FILE_YAML

from vectors.surfaces.family_file import (
    FAMILY_REGISTRY,
    REPO_ROOT,
    TOML_REGISTRY,
    registry_files,
)

SCRIPT = REPO_ROOT / "bin" / "yaml-to-toml.py"
YAML_ROOT = REPO_ROOT / FAMILY_REGISTRY
TOML_ROOT = REPO_ROOT / TOML_REGISTRY

#: The count of family files of the registry today.
FAMILY_FILES = 10


def _twin_path(path: Path) -> Path:
    """The path in the TOML registry of a file of the YAML registry."""
    twin = TOML_ROOT / path.relative_to(YAML_ROOT)
    if twin.name == FAMILY_FILE_YAML:
        return twin.with_name(FAMILY_FILE)

    return twin


YAML_FILES = registry_files(YAML_ROOT)
FAMILY_PAIRS = [path for path in YAML_FILES if path.name == FAMILY_FILE_YAML]
OTHER_FILES = [path for path in YAML_FILES if path.name != FAMILY_FILE_YAML]


def _label(path: Path) -> str:
    return path.relative_to(YAML_ROOT).as_posix()


def test_each_file_has_one_twin_and_no_twin_is_left_over() -> None:
    assert sorted(_twin_path(path) for path in YAML_FILES) == registry_files(TOML_ROOT)


def test_the_registry_holds_ten_family_files() -> None:
    assert len(FAMILY_PAIRS) == FAMILY_FILES


@pytest.mark.parametrize("path", OTHER_FILES, ids=_label)
def test_each_other_file_has_the_bytes_of_its_twin(path: Path) -> None:
    assert _twin_path(path).read_bytes() == path.read_bytes()


@pytest.mark.parametrize("path", FAMILY_PAIRS, ids=_label)
def test_each_family_toml_holds_the_values_of_its_twin(path: Path) -> None:
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "--check", str(path), str(_twin_path(path))],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("yaml-to-toml: equal: "), done.stdout
