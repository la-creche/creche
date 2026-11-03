"""The generated models do what the shared wire vectors say (decision CH9-1).

`rust/crates/wire/vectors/wire.json` holds, for each wire type, JSON texts and
what the Rust type does with each one. The Rust crate reads the same file
(`rust/crates/wire/src/vectors.rs`), so the generated models cannot drift from
the Rust parser. The Rust crate is the authority. A failure here is a defect
of the generation, never a reason to edit a case."""

from pathlib import Path
from typing import Any, cast

import creche_wire
import pytest
from creche_wire import strictjson
from pydantic import BaseModel, ValidationError

WIRE = Path(__file__).resolve().parents[2] / "rust" / "crates" / "wire"
VECTOR_FILE = WIRE / "vectors" / "wire.json"
SCHEMA_FILE = WIRE / "schema" / "wire.json"

#: A cap above each file and each case here. The wire has no cap yet: each
#: caller of `creche_wire.read` gives the cap of its own surface.
CAP = 1 << 20

#: The names of the package that are no wire type.
READER = {"NotStrictJson", "read"}


def _load(path: Path) -> dict[str, Any]:
    """serde_json in the Rust test keeps the last of two equal keys. A second
    block of one type would drop the first block's cases there, so strict JSON
    refuses the file here."""
    value = strictjson.loads(path.read_bytes(), cap=CAP)
    assert isinstance(value, dict)

    return cast(dict[str, Any], value)


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
        value = creche_wire.read(_model(type_name), text.encode("utf-8"), cap=CAP)
    except (creche_wire.NotStrictJson, ValidationError):
        return None

    return value.model_dump_json()


@pytest.mark.parametrize(("type_name", "case"), CASES)
def test_each_case_does_what_the_file_says(type_name: str, case: dict[str, Any]) -> None:
    assert set(case) == {"json", "written", "note"}
    assert _read_and_write(type_name, case["json"]) == case["written"]


def _variant_titles(schema: dict[str, Any]) -> list[str]:
    """The `title` of each `oneOf` member. datamodel-codegen names the class
    of a variant by it, and a caller needs that class to build the enum."""
    definitions = cast(dict[str, dict[str, Any]], schema["$defs"])

    return [
        member["title"]
        for definition in definitions.values()
        for member in definition.get("oneOf", [])
    ]


def test_each_wire_type_has_cases_and_a_model() -> None:
    schema = _load(SCHEMA_FILE)
    defined = sorted(schema["$defs"])

    assert sorted(VECTORS) == defined
    assert sorted(set(creche_wire.__all__) - READER) == sorted(defined + _variant_titles(schema))


def _nodes(value: object) -> list[dict[str, object]]:
    """Each JSON object in `value`, at any depth, `value` too."""
    if isinstance(value, list):
        return [node for item in cast(list[object], value) for node in _nodes(item)]
    if not isinstance(value, dict):
        return []
    node = cast(dict[str, object], value)

    return [node] + [found for item in node.values() for found in _nodes(item)]


def _unchecked(schema: object) -> list[str]:
    """Each node of the schema whose generated model is wider than the Rust
    type.

    datamodel-codegen drops `format` for an integer, so an integer without
    `minimum` and `maximum` reads a value past the width of the Rust type.
    pydantic reads a date in forms that RFC 3339 refuses, so a `date-time`
    needs a `pattern` of its form."""
    found: list[str] = []
    for node in _nodes(schema):
        if node.get("type") == "integer" and not {"minimum", "maximum"} <= node.keys():
            found.append(f"an integer without bounds: {node}")
        if node.get("format") == "date-time" and "pattern" not in node:
            found.append(f"a date-time without a pattern: {node}")

    return found


def test_each_integer_and_date_of_the_schema_is_checked() -> None:
    assert _unchecked(_load(SCHEMA_FILE)) == []


def test_an_unchecked_integer_or_date_is_found() -> None:
    schema = {
        "$defs": {
            "A": {"type": "integer", "format": "uint8", "minimum": 0},
            "B": {"type": "string", "format": "date-time"},
        }
    }

    assert len(_unchecked(schema)) == 2


#: The core schema types of a scalar that a lax model converts.
SCALARS = {"str", "bytes", "int", "float", "bool"}


@pytest.mark.parametrize("name", sorted(set(creche_wire.__all__) - READER))
def test_each_scalar_of_a_wire_model_is_strict_without_read(name: str) -> None:
    """`strict-types` in the codegen settings holds a direct `model_validate`
    strict too. A lax str takes bytes, so this check fails without it."""
    schema = getattr(creche_wire, name).__pydantic_core_schema__
    lax = [
        node["type"]
        for node in _nodes(schema)
        if node.get("type") in SCALARS and node.get("strict") is not True
    ]

    assert lax == []
