"""The generated models do what the shared wire vectors say (decision CH9-1).

`rust/crates/wire/vectors/wire.json` holds, for each wire type, JSON texts and
what the Rust type does with each one. The Rust crate reads the same file
(`rust/crates/wire/src/vectors.rs`), so the generated models cannot drift from
the Rust parser. The Rust crate is the authority. A failure here is a defect
of the generation, never a reason to edit a case."""

import json
from pathlib import Path
from typing import Any

import creche_wire
import pytest
from pydantic import BaseModel, ValidationError

WIRE = Path(__file__).resolve().parents[2] / "rust" / "crates" / "wire"
VECTOR_FILE = WIRE / "vectors" / "wire.json"
SCHEMA_FILE = WIRE / "schema" / "wire.json"


def _refuse_repeats(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """`json.loads` keeps the last of two equal keys, and so does serde_json in
    the Rust test. A second block of one type would drop the first block's
    cases from both tests, so a repeated key fails here."""
    keys = [key for key, _ in pairs]
    repeated = sorted({key for key in keys if keys.count(key) > 1})
    assert not repeated, f"repeated keys: {repeated}"

    return dict(pairs)


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_refuse_repeats)


VECTORS: dict[str, list[dict[str, Any]]] = _load(VECTOR_FILE)
CASES = [
    pytest.param(type_name, case, id=f"{type_name}: {case['note']}")
    for type_name, cases in VECTORS.items()
    for case in cases
]


def _model(type_name: str) -> type[BaseModel]:
    model = getattr(creche_wire, type_name)
    assert isinstance(model, type) and issubclass(model, BaseModel), type_name

    return model


def _read_and_write(type_name: str, text: str) -> str | None:
    try:
        value = _model(type_name).model_validate_json(text)
    except ValidationError:
        return None

    return value.model_dump_json()


@pytest.mark.parametrize(("type_name", "case"), CASES)
def test_each_case_does_what_the_file_says(type_name: str, case: dict[str, Any]) -> None:
    assert set(case) == {"json", "written", "note"}
    assert _read_and_write(type_name, case["json"]) == case["written"]


def test_each_wire_type_has_cases_and_a_model() -> None:
    defined = sorted(_load(SCHEMA_FILE)["$defs"])

    assert sorted(VECTORS) == defined
    assert sorted(creche_wire.__all__) == defined
