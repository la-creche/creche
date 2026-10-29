"""The TOML fixture registry of the vectors is a copy of the fixture registry
of the package `family`.

`vectors/fixtures/registry-toml` holds one twin of each file of
`family/tests/fixtures/registry`. The twin has the same path and the same
bytes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest
from agent_family.registry import FAMILY_FILE, SERVER_FILE

from vectors.surfaces.family_file import (
    FAMILY_REGISTRY,
    REPO_ROOT,
    TOML_REGISTRY,
    registry_files,
)

FAMILY_ROOT = REPO_ROOT / FAMILY_REGISTRY
TOML_ROOT = REPO_ROOT / TOML_REGISTRY

#: The count of family files and of server files of the registry today.
FAMILY_FILES: Final = 9
SERVER_FILES: Final = 11

FAMILY_ROOT_FILES = registry_files(FAMILY_ROOT)


def _twin_path(path: Path) -> Path:
    """The path in the TOML registry of a file of the fixture registry."""
    return TOML_ROOT / path.relative_to(FAMILY_ROOT)


def _label(path: Path) -> str:
    return path.relative_to(FAMILY_ROOT).as_posix()


def test_each_file_has_one_twin_and_no_twin_is_left_over() -> None:
    assert [_twin_path(path) for path in FAMILY_ROOT_FILES] == registry_files(TOML_ROOT)


def test_the_registry_holds_nine_family_files_and_eleven_server_files() -> None:
    names = [path.name for path in FAMILY_ROOT_FILES]

    assert names.count(FAMILY_FILE) == FAMILY_FILES
    assert names.count(SERVER_FILE) == SERVER_FILES


@pytest.mark.parametrize("path", FAMILY_ROOT_FILES, ids=_label)
def test_each_file_has_the_bytes_of_its_twin(path: Path) -> None:
    assert _twin_path(path).read_bytes() == path.read_bytes()
