"""The two Python TOML readers hold equal bytes.

`handover.tomlfile` is a copy of the whole module `agent_family.tomlfile`,
because `handover` imports no module of `agent_family`. The test holds the
two files equal, byte for byte."""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

#: The two copies of the Python module: the reader of `agent_family` and the
#: reader of the release tool.
PYTHON_FILES = (
    REPO / "family" / "src" / "agent_family" / "tomlfile.py",
    REPO / "handover" / "src" / "handover" / "tomlfile.py",
)


def test_the_two_python_readers_hold_equal_bytes() -> None:
    family, handover = (path.read_bytes() for path in PYTHON_FILES)

    assert family == handover
