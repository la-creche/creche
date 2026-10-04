"""The committed files of the family and server surfaces hold what a replay needs.

These tests read the committed files. `test_vectors_current.py` holds them
equal to what the generator writes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from vectors import generate
from vectors.surfaces.family_cli import ARGPARSE_USAGE
from vectors.surfaces.family_file import REGISTRIES_FILE, copy_registry, registry_files

#: The surfaces whose vectors name a registry of this repository.
REGISTRY_SURFACES = ("family_file.json", "family_file.host.json", "server_file.json")
CLI_FILE = "family_file.cli.json"
CLASSIFY_FILE = "family_file.classify.json"

#: The keys of one change of a diff.
CHANGE_KEYS = {"field", "landing", "direction", "step", "detail"}


def _document(path: str) -> dict[str, Any]:
    return json.loads(generate.committed()[path])


def _outcome(vector: dict[str, Any]) -> dict[str, Any]:
    """What a command line ended with: `value` when accepted, else `refusal`."""
    return vector.get("value") or vector["refusal"]


def test_the_registry_file_holds_every_registry_a_vector_names() -> None:
    held = {row["registry"] for row in _document(REGISTRIES_FILE)["files"]}
    for path in (*REGISTRY_SURFACES, CLI_FILE):
        for vector in _document(path)["vectors"]:
            assert vector["params"]["registry"] in held, f"{path}: {vector['id']}"


def test_the_registry_file_holds_one_row_per_file() -> None:
    rows = _document(REGISTRIES_FILE)["files"]
    keys = [(row["registry"], row["path"]) for row in rows]

    assert len(keys) == len(set(keys))
    assert all(set(row) - {"registry", "path"} <= {"text", "base64"} for row in rows)
    assert all(not row["path"].startswith("/") and ".." not in row["path"] for row in rows)


def test_a_command_line_vector_holds_no_text_of_argparse() -> None:
    for vector in _document(CLI_FILE)["vectors"]:
        outcome = _outcome(vector)
        for stream in ("stdout", "stderr"):
            assert not outcome.get(stream, "").startswith(ARGPARSE_USAGE), vector["id"]

        assert (vector["result"] == "accepted") == (outcome["exit"] == 0), vector["id"]


def test_a_command_line_vector_holds_no_path_of_the_machine() -> None:
    text = generate.committed()[CLI_FILE]

    assert "/tmp" not in text and "/var/" not in text and "/Users/" not in text


def test_every_classify_vector_holds_a_whole_diff() -> None:
    for vector in _document(CLASSIFY_FILE)["vectors"]:
        value = vector["value"]

        assert set(vector["input"]["args"]) == {"old", "new"}, vector["id"]
        assert all(set(change) == CHANGE_KEYS for change in value["changes"]), vector["id"]
        assert value["changed"] == bool(value["changes"]), vector["id"]


def test_a_hidden_file_is_no_file_of_a_registry(tmp_path: Path) -> None:
    source = tmp_path / "source"
    kept = source / "families" / "one" / "family.yaml"
    kept.parent.mkdir(parents=True)
    kept.write_text("name: one\n", encoding="utf-8")
    (source / ".DS_Store").write_bytes(b"\x00")
    (source / "families" / "one" / ".hidden.yaml").write_text("x: 1\n", encoding="utf-8")
    (source / "families" / ".cache").mkdir()
    (source / "families" / ".cache" / "family.yaml").write_text("x: 1\n", encoding="utf-8")

    copy_registry(source, tmp_path / "copy")

    assert registry_files(source) == [kept]
    assert sorted(path for path in (tmp_path / "copy").rglob("*") if path.is_file()) == [
        tmp_path / "copy" / "families" / "one" / "family.yaml"
    ]
