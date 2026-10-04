"""The generator of the group `runtime`: what its output must not depend on.

A test here builds the group `runtime` alone, which takes about one second.
The build of each group is in `test_vectors_current.py`.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from vectors import generate
from vectors.core import render
from vectors.surfaces import runtime


def test_a_token_kind_ignores_the_scratch_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal text names a path. The name of the scratch directory is no part of the kind."""
    marks = dict.fromkeys(mark for reader in runtime.TOKEN_READERS for mark, _ in reader.kinds)
    scratch = tmp_path / " ".join(marks)
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))

    built = {surface.path: render(surface) for surface in runtime.surfaces()}
    on_disk = generate.committed()

    assert sorted(path for path, text in built.items() if on_disk.get(path) != text) == []
