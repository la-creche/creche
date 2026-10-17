"""The TOML fixture registry of the vectors is a copy of the fixture registry
of the package `family`.

`vectors/fixtures/registry-toml` holds one twin of each file of
`family/tests/fixtures/registry`. The twin of `family.yaml` is `family.toml`
in the same directory, and the twin of `server.yaml` is `server.toml`.
`bin/yaml-to-toml.py --check` proves each such pair: the TOML text holds the
values and the count of comments of the YAML text. The twin of each other
file has the same path and the same bytes.

The proof runs the converter in a child process, as a person runs it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest
from agent_family.registry import FAMILY_FILE, FAMILY_FILE_YAML, SERVER_FILE, SERVER_FILE_YAML

from vectors.surfaces.family_file import (
    FAMILY_REGISTRY,
    REPO_ROOT,
    TOML_REGISTRY,
    registry_files,
)

SCRIPT = REPO_ROOT / "bin" / "yaml-to-toml.py"
YAML_ROOT = REPO_ROOT / FAMILY_REGISTRY
TOML_ROOT = REPO_ROOT / TOML_REGISTRY

#: The name of the twin of each YAML file that the converter writes.
CONVERTED: Final = {FAMILY_FILE_YAML: FAMILY_FILE, SERVER_FILE_YAML: SERVER_FILE}

#: The count of family files and of server files of the registry today.
FAMILY_FILES = 10
SERVER_FILES = 11


def _twin_path(path: Path) -> Path:
    """The path in the TOML registry of a file of the YAML registry."""
    twin = TOML_ROOT / path.relative_to(YAML_ROOT)
    if twin.name in CONVERTED:
        return twin.with_name(CONVERTED[twin.name])

    return twin


YAML_FILES = registry_files(YAML_ROOT)
CONVERTED_PAIRS = [path for path in YAML_FILES if path.name in CONVERTED]
OTHER_FILES = [path for path in YAML_FILES if path.name not in CONVERTED]


def _label(path: Path) -> str:
    return path.relative_to(YAML_ROOT).as_posix()


def test_each_file_has_one_twin_and_no_twin_is_left_over() -> None:
    assert sorted(_twin_path(path) for path in YAML_FILES) == registry_files(TOML_ROOT)


def test_the_registry_holds_ten_family_files_and_eleven_server_files() -> None:
    names = [path.name for path in CONVERTED_PAIRS]

    assert names.count(FAMILY_FILE_YAML) == FAMILY_FILES
    assert names.count(SERVER_FILE_YAML) == SERVER_FILES


@pytest.mark.parametrize("path", OTHER_FILES, ids=_label)
def test_each_other_file_has_the_bytes_of_its_twin(path: Path) -> None:
    assert _twin_path(path).read_bytes() == path.read_bytes()


@pytest.mark.parametrize("path", CONVERTED_PAIRS, ids=_label)
def test_each_toml_file_holds_the_values_of_its_twin(path: Path) -> None:
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "--check", str(path), str(_twin_path(path))],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("yaml-to-toml: equal: "), done.stdout
