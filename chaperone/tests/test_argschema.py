"""The subset JSON Schema validator behind the verb catalog."""

from __future__ import annotations

import pytest
from chaperone.argschema import SchemaUnsupported, assert_supported, validate


def test_type_names() -> None:
    assert validate({"type": "string"}, "x") is None
    assert validate({"type": "string"}, 1) is not None
    assert validate({"type": "integer"}, 3) is None
    assert validate({"type": "integer"}, "3") is not None
    assert validate({"type": "object"}, {}) is None
    assert validate({"type": "array"}, []) is None
    assert validate({"type": "null"}, None) is None
    assert validate({"type": ["string", "null"]}, None) is None
    assert validate({"type": ["string", "null"]}, 5) is not None
    assert validate({}, object()) is None


def test_a_boolean_is_not_an_integer() -> None:
    """JSON has no boolean-as-integer. Python does, so it is excluded."""
    assert validate({"type": "integer"}, True) is not None
    assert validate({"type": "boolean"}, True) is not None


def test_string_bounds_and_pattern() -> None:
    schema = {"type": "string", "minLength": 2, "maxLength": 4, "pattern": "^[a-z]+$"}
    assert validate(schema, "abc") is None
    assert validate(schema, "a") is not None
    assert validate(schema, "abcde") is not None
    assert validate(schema, "AB") is not None


def test_a_trailing_newline_cannot_pass_an_anchor() -> None:
    """Python's `$` also matches before a trailing newline; ECMA's does not."""
    assert validate({"pattern": "^[a-z]+$"}, "chat\n") is not None
    assert validate({"pattern": "^[a-z]+$"}, "chat") is None


def test_only_a_trailing_anchor_is_translated() -> None:
    """A pattern with no trailing `$`, and one ending in a literal `$`, are
    both compiled as written."""
    assert validate({"pattern": "^ab"}, "abc") is None
    assert validate({"pattern": "^ab"}, "xab") is not None
    assert validate({"pattern": r"^[0-9]+\$"}, "5$") is None
    assert validate({"pattern": r"^[0-9]+\$"}, "5") is not None


def test_date_time_format() -> None:
    schema = {"type": "string", "format": "date-time"}
    assert validate(schema, "2026-09-18T19:41:07Z") is None
    assert validate(schema, "yesterday") is not None


def test_integer_bounds() -> None:
    schema = {"type": "integer", "minimum": 1, "maximum": 200}
    assert validate(schema, 1) is None
    assert validate(schema, 0) is not None
    assert validate(schema, 201) is not None


def test_enum() -> None:
    schema = {"enum": ["release", "rollback"]}
    assert validate(schema, "release") is None
    assert validate(schema, "delete") is not None


def test_object_required_and_unknown_keys() -> None:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["a"],
        "properties": {"a": {"type": "string"}},
    }
    assert validate(schema, {"a": "x"}) is None
    assert validate(schema, {}) is not None
    assert validate(schema, {"a": "x", "b": 1}) is not None
    assert validate(schema, {"a": 1}) is not None


def test_object_counts_and_value_schema() -> None:
    schema = {
        "type": "object",
        "minProperties": 1,
        "maxProperties": 2,
        "additionalProperties": {"type": "string", "pattern": "^v[0-9]$"},
    }
    assert validate(schema, {"one": "v1"}) is None
    assert validate(schema, {"one": "v1", "two": "v2"}) is None
    assert validate(schema, {}) is not None
    assert validate(schema, {"a": "v1", "b": "v2", "c": "v3"}) is not None
    assert validate(schema, {"a": "nope"}) is not None
    # No `additionalProperties` at all: every key is unconstrained.
    assert validate({"type": "object"}, {"a": 1, "b": 2}) is None


def test_array_items_and_cap() -> None:
    schema = {"type": "array", "maxItems": 2, "items": {"type": "string", "maxLength": 3}}
    assert validate(schema, ["ab", "cd"]) is None
    assert validate(schema, ["a", "b", "c"]) is not None
    assert validate(schema, ["abcd"]) is not None
    assert validate({"type": "array", "maxItems": 2}, [1, 2]) is None


def test_nested_paths_name_the_failing_argument() -> None:
    schema = {"type": "object", "properties": {"data": {"type": "object", "maxProperties": 1}}}
    failed = validate(schema, {"data": {"a": 1, "b": 2}})
    assert failed is not None
    assert "data" in failed


def test_unsupported_keywords_are_refused_up_front() -> None:
    with pytest.raises(SchemaUnsupported):
        assert_supported({"allOf": []})
    with pytest.raises(SchemaUnsupported):
        assert_supported({"type": "boolean"})
    with pytest.raises(SchemaUnsupported):
        assert_supported({"type": "string", "format": "uri"})
    with pytest.raises(SchemaUnsupported):
        assert_supported({"properties": {"a": {"oneOf": []}}})
    with pytest.raises(SchemaUnsupported):
        assert_supported({"items": {"$ref": "#/x"}})


def test_supported_schemas_pass_the_check() -> None:
    assert_supported({"type": "object", "properties": {"a": {"type": "string"}}})
    assert_supported({"type": ["string", "null"]})
    assert_supported({"additionalProperties": {"type": "string"}})


def test_a_property_that_is_not_a_schema_is_left_alone() -> None:
    """Nothing in the catalog writes one. `assert_supported` walks what it can
    and refuses to guess at the rest."""
    assert_supported({"properties": {"a": "not a schema"}})
    assert_supported({"items": "not a schema"})


def test_a_schema_with_no_type_constrains_nothing() -> None:
    """`kind` in `release` is an enum with no `type`, so the type check has to
    pass anything the enum allows — including a value no catalog schema uses."""
    assert validate({}, True) is None
    assert validate({}, 1.5) is None
