"""The committed vector files say what the Python code does today.

The generator runs in memory and the result is compared with
`vectors/data/`. A change to a product package that moves behavior fails
here until the vector moves in the same commit, and the Rust tests then see
the move too.

Fix a failure with `uv run python -m vectors.generate`, from the repository
root. Read the diff before you commit it: every changed line is a change in
what the platform accepts.
"""

from __future__ import annotations

import json
from typing import Any

from vectors import generate
from vectors.core import ACCEPTED, FORMAT, RAISED, REFUSED, Json, depth, has_surrogate

REGENERATE = "run `uv run python -m vectors.generate` and read the diff"
FIX_FIRST = "fix the defect in its package first: vectors/AGENTS.md rule 5"

#: The forms an input takes: text, bytes that are not UTF-8, a long text
#: written as repeated parts, the named arguments of a builder, or the chunks
#: of a byte stream.
INPUT_FORMS = frozenset({"text", "base64", "repeat", "args", "chunks"})

#: The deepest nesting a committed file may have. A reader in another
#: language stops at a limit of its own: serde_json refuses level 128.
MAX_FILE_DEPTH = 100


def _refuse_constant(name: str) -> Any:
    raise ValueError(f"{name} is not strict JSON")


def _first_difference(wanted: str, found: str) -> str:
    """The first line two texts differ on, cut short for a readable failure."""
    for number, (left, right) in enumerate(
        zip(wanted.splitlines(), found.splitlines(), strict=False), start=1
    ):
        if left != right:
            return f"line {number}: generated {left[:200]!r}, committed {right[:200]!r}"

    return "one file is a prefix of the other"


def test_committed_files_are_current() -> None:
    _, built = generate.build()
    on_disk = generate.committed()
    problems = generate.stale(built, on_disk, generate.strays())
    details = [
        f"{path}: {_first_difference(built[path], on_disk[path])}"
        for path in sorted(built.keys() & on_disk.keys())
        if built[path] != on_disk[path]
    ]

    assert not problems, f"{REGENERATE}: {problems} {details}"


# The tests below read the committed files. The test above holds them equal
# to what the generator writes, and it is the only one that pays for a build.


def test_every_file_is_strict_ascii_json() -> None:
    for path, text in generate.committed().items():
        assert text.isascii(), path
        assert text.endswith("}\n") and not text.endswith("\n\n"), path

        document = json.loads(text, parse_constant=_refuse_constant)

        assert document["format"] == FORMAT, path


def _strings(value: Json) -> list[str]:
    """Every key and every string value of one parsed document."""
    found: list[str] = []
    stack: list[Json] = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            found.append(item)
        elif isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, dict):
            found.extend(item)
            stack.extend(item.values())

    return found


def test_a_reader_with_limits_reads_every_file() -> None:
    """Python's reader takes any depth and a lone surrogate escape. Others do not."""
    for path, text in generate.committed().items():
        document: Json = json.loads(text)

        assert depth(document) <= MAX_FILE_DEPTH, path
        assert not any(has_surrogate(string) for string in _strings(document)), path


def _vector_files() -> dict[str, list[dict[str, Any]]]:
    """The vectors of every committed vector file, by path. The index is not one."""
    found: dict[str, list[dict[str, Any]]] = {}
    for path, text in generate.committed().items():
        document = json.loads(text)
        if isinstance(document.get("vectors"), list):
            found[path] = document["vectors"]

    return found


def test_every_vector_has_an_id_an_input_and_a_result() -> None:
    for path, vectors in _vector_files().items():
        seen: set[str] = set()
        for vector in vectors:
            assert vector["id"] not in seen, f"{path}: {vector['id']} twice"
            seen.add(vector["id"])

            assert vector["result"] in {ACCEPTED, REFUSED, RAISED}, path
            assert len(vector["input"]) == 1, path
            assert set(vector["input"]) <= INPUT_FORMS, path


def test_no_committed_vector_is_raised() -> None:
    """This repository is public. A `raised` vector names an input that makes a service raise."""
    for path, vectors in _vector_files().items():
        found = [vector["id"] for vector in vectors if vector["result"] == RAISED]

        assert not found, f"{path}: {found}: {FIX_FIRST}"


def test_the_index_names_every_vector_file() -> None:
    index = json.loads(generate.committed()[generate.INDEX_FILE])
    listed = {row["path"]: row["vectors"] for row in index["surfaces"]}
    counted = {path: len(vectors) for path, vectors in _vector_files().items()}

    assert listed == counted
