"""The wire types do what `memory/vectors/wire.json` says.

The models of `creche_memory.wire` are the authority. The `memory` upstream of
the `mcp` repository holds the same request texts as its accepted cases. A
failure here is a change in what the socket accepts: say why in the commit."""

import json
from pathlib import Path
from typing import Any

import creche_memory
import pytest
from creche_memory.wire import MAX_BODY_BYTES
from pydantic import TypeAdapter, ValidationError

VECTOR_FILE = Path(__file__).resolve().parents[1] / "vectors" / "wire.json"


def _refuse_repeats(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """`json.loads` keeps the last of two equal keys. A second block of one
    type would drop the first block's cases, so a repeated key fails here."""
    keys = [key for key, _ in pairs]
    repeated = sorted({key for key in keys if keys.count(key) > 1})
    assert not repeated, f"repeated keys: {repeated}"

    return dict(pairs)


VECTORS: dict[str, list[dict[str, Any]]] = json.loads(
    VECTOR_FILE.read_text(encoding="utf-8"), object_pairs_hook=_refuse_repeats
)
CASES = [
    pytest.param(type_name, case, id=f"{type_name}: {case['note']}")
    for type_name, cases in VECTORS.items()
    for case in cases
]


def _read_and_write(type_name: str, text: str) -> str | None:
    adapter: TypeAdapter[Any] = TypeAdapter(getattr(creche_memory, type_name))
    try:
        value = adapter.validate_json(text)
    except ValidationError:
        return None

    return adapter.dump_json(value).decode()


@pytest.mark.parametrize(("type_name", "case"), CASES)
def test_each_case_does_what_the_file_says(type_name: str, case: dict[str, Any]) -> None:
    assert set(case) == {"json", "written", "note"}
    assert _read_and_write(type_name, case["json"]) == case["written"]


def test_each_wire_type_has_both_kinds_of_case() -> None:
    assert sorted(VECTORS) == sorted(creche_memory.__all__)

    for type_name, cases in VECTORS.items():
        written = [case["written"] for case in cases]
        assert any(text is not None for text in written), type_name
        assert any(text is None for text in written), type_name


BODY_CASES = [(MAX_BODY_BYTES, "accepted"), (MAX_BODY_BYTES + 1, "refused")]


@pytest.mark.parametrize(("size", "outcome"), BODY_CASES)
def test_the_body_cap_counts_bytes(size: int, outcome: str) -> None:
    body = "é" * (size // 2) + "x" * (size % 2)
    line = json.dumps(
        {"v": 1, "op": "create", "caller": "chat", "slug": "a", "title": "A", "body": body}
    )
    written = _read_and_write("Request", line)

    assert len(body.encode()) == size
    assert ("accepted" if written is not None else "refused") == outcome
