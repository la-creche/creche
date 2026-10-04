"""A name or a value that a codec refuses never raises out of the package.

The reader answers a report, and the program ends with an exit code."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from agent_family import registry


def test_revision_reads_a_name_that_is_not_utf8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The revision holds the bytes of the file name. Not each file system
    can make such a name, so the test gives the path to the function."""
    named = tmp_path / registry.FAMILIES_DIR / "bad\udce9name" / registry.FAMILY_FILE
    monkeypatch.setattr(registry, "_registry_files", lambda _root: [named])
    monkeypatch.setattr(registry, "_read", lambda _path: ("text", None))

    revision = registry.revision_of(tmp_path)

    digest = hashlib.sha256(b"families/bad\xe9name/family.yaml\0text\0").hexdigest()
    assert revision == registry.REVISION_PREFIX + digest[: registry.REVISION_CHARS]
