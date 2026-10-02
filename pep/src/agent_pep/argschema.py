"""A deterministic validator for the verb catalog's JSON Schemas.

Contract 04 §4.1 fixes six schemas and says the manifest serves them verbatim.
Verbatim rules out generating them from a model, so they are literal documents
and this module checks arguments against them.

Only the keywords those six schemas use are implemented. `assert_supported`
refuses anything else, and `verbs.py` runs it over the whole catalog at import,
so a keyword added to the contract fails loudly here instead of being ignored
quietly at a decision (invariant 11).

No dependency, no LLM, no clock: this is the same kind of pure code as
`family_decisions.py`, which calls it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from functools import lru_cache
from typing import Final, cast

#: Every keyword the six schemas in contract 04 §4.1 use. `default` is an
#: annotation the manifest carries for the model; it constrains nothing.
SUPPORTED_KEYWORDS: Final = frozenset(
    {
        "type",
        "enum",
        "default",
        "required",
        "properties",
        "additionalProperties",
        "minProperties",
        "maxProperties",
        "minLength",
        "maxLength",
        "pattern",
        "format",
        "minimum",
        "maximum",
        "items",
        "maxItems",
    }
)

SUPPORTED_TYPES: Final = frozenset({"object", "array", "string", "integer", "null"})

#: The one `format` the catalog uses (`job_status.since`).
SUPPORTED_FORMATS: Final = frozenset({"date-time"})


class SchemaUnsupported(RuntimeError):
    """A catalog schema uses something this validator does not implement."""


@lru_cache(maxsize=128)
def _compile(pattern: str) -> re.Pattern[str]:
    """Compile a JSON Schema `pattern` with ECMA anchor meaning.

    In ECMA `$` is the end of the input. In Python it also matches just before
    a trailing newline, so `"chat\\n"` would pass `^[a-z-]+$` and a family name
    could smuggle one. Translate the trailing anchor to `\\Z`.
    """
    if pattern.endswith("$") and not pattern.endswith("\\$"):
        pattern = pattern[:-1] + r"\Z"
    return re.compile(pattern)


def assert_supported(schema: Mapping[str, object], where: str = "") -> None:
    """Raise unless every keyword, type and format in `schema` is implemented."""
    for key in schema:
        if key not in SUPPORTED_KEYWORDS:
            raise SchemaUnsupported(f"{where or 'schema'}: keyword {key!r} is not implemented")

    declared = schema.get("type")
    for name in _as_names(declared):
        if name not in SUPPORTED_TYPES:
            raise SchemaUnsupported(f"{where or 'schema'}: type {name!r} is not implemented")

    fmt = schema.get("format")
    if isinstance(fmt, str) and fmt not in SUPPORTED_FORMATS:
        raise SchemaUnsupported(f"{where or 'schema'}: format {fmt!r} is not implemented")

    properties = schema.get("properties")
    if isinstance(properties, dict):
        for name, sub in cast("dict[str, object]", properties).items():
            if isinstance(sub, dict):
                assert_supported(cast("Mapping[str, object]", sub), f"{where}.{name}")

    for key in ("additionalProperties", "items"):
        sub = schema.get(key)
        if isinstance(sub, dict):
            assert_supported(cast("Mapping[str, object]", sub), f"{where}.{key}")


def _as_names(declared: object) -> list[str]:
    if isinstance(declared, str):
        return [declared]
    if isinstance(declared, list):
        return [n for n in cast("list[object]", declared) if isinstance(n, str)]
    return []


def _type_ok(name: str, value: object) -> bool:
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    if name == "string":
        return isinstance(value, str)
    if name == "integer":
        # JSON has no boolean-as-integer; Python does. Exclude it explicitly.
        return isinstance(value, int) and not isinstance(value, bool)
    return value is None


def _label(path: str) -> str:
    return path or "arguments"


def _check_type(schema: Mapping[str, object], value: object, path: str) -> str | None:
    declared = schema.get("type")
    if declared is None:
        return None
    for name in _as_names(declared):
        if _type_ok(name, value):
            return None
    return f"{_label(path)}: expected {declared}"


def _check_enum(schema: Mapping[str, object], value: object, path: str) -> str | None:
    allowed = schema.get("enum")
    if not isinstance(allowed, list):
        return None
    if value in cast("list[object]", allowed):
        return None
    return f"{_label(path)}: not one of {allowed}"


def _check_string(schema: Mapping[str, object], value: str, path: str) -> str | None:
    least = schema.get("minLength")
    if isinstance(least, int) and len(value) < least:
        return f"{_label(path)}: shorter than {least} characters"

    most = schema.get("maxLength")
    if isinstance(most, int) and len(value) > most:
        return f"{_label(path)}: longer than {most} characters"

    pattern = schema.get("pattern")
    if isinstance(pattern, str) and _compile(pattern).search(value) is None:
        return f"{_label(path)}: does not match {pattern}"

    if schema.get("format") == "date-time" and not _is_date_time(value):
        return f"{_label(path)}: not an RFC 3339 date-time"

    return None


def _is_date_time(value: str) -> bool:
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def _check_integer(schema: Mapping[str, object], value: int, path: str) -> str | None:
    least = schema.get("minimum")
    if isinstance(least, int) and value < least:
        return f"{_label(path)}: below {least}"

    most = schema.get("maximum")
    if isinstance(most, int) and value > most:
        return f"{_label(path)}: above {most}"

    return None


def _check_array(schema: Mapping[str, object], value: Sequence[object], path: str) -> str | None:
    most = schema.get("maxItems")
    if isinstance(most, int) and len(value) > most:
        return f"{_label(path)}: more than {most} items"

    item_schema = schema.get("items")
    if not isinstance(item_schema, dict):
        return None
    for index, item in enumerate(value):
        failed = validate(cast("Mapping[str, object]", item_schema), item, f"{path}[{index}]")
        if failed is not None:
            return failed
    return None


def _check_required(
    schema: Mapping[str, object], value: Mapping[str, object], path: str
) -> str | None:
    required = schema.get("required")
    if not isinstance(required, list):
        return None
    for name in cast("list[object]", required):
        if isinstance(name, str) and name not in value:
            return f"{_label(path)}: {name} is required"
    return None


def _check_counts(
    schema: Mapping[str, object], value: Mapping[str, object], path: str
) -> str | None:
    least = schema.get("minProperties")
    if isinstance(least, int) and len(value) < least:
        return f"{_label(path)}: fewer than {least} keys"

    most = schema.get("maxProperties")
    if isinstance(most, int) and len(value) > most:
        return f"{_label(path)}: more than {most} keys"

    return None


def _check_members(
    schema: Mapping[str, object], value: Mapping[str, object], path: str
) -> str | None:
    """Each declared property, then whatever `additionalProperties` says about
    the rest. `false` refuses an unknown key outright (contract 04 §4.1)."""
    properties = schema.get("properties")
    declared = cast("dict[str, object]", properties) if isinstance(properties, dict) else {}
    extra = schema.get("additionalProperties")

    for name, item in value.items():
        sub = declared.get(name)
        if isinstance(sub, dict):
            failed = validate(cast("Mapping[str, object]", sub), item, _join(path, name))
            if failed is not None:
                return failed
            continue
        if extra is False:
            return f"{_label(path)}: {name} is not a known argument"
        if isinstance(extra, dict):
            failed = validate(cast("Mapping[str, object]", extra), item, _join(path, name))
            if failed is not None:
                return failed

    return None


def _join(path: str, name: str) -> str:
    if not path:
        return name
    return f"{path}.{name}"


def _check_object(
    schema: Mapping[str, object], value: Mapping[str, object], path: str
) -> str | None:
    for check in (_check_required, _check_counts, _check_members):
        failed = check(schema, value, path)
        if failed is not None:
            return failed
    return None


def validate(schema: Mapping[str, object], value: object, path: str = "") -> str | None:
    """Return why `value` fails `schema`, or None when it passes."""
    failed = _check_enum(schema, value, path)
    if failed is not None:
        return failed

    failed = _check_type(schema, value, path)
    if failed is not None:
        return failed

    if isinstance(value, str):
        return _check_string(schema, value, path)

    if isinstance(value, bool):
        # Guarded before int, because bool is an int in Python and none of the
        # integer bounds mean anything for it.
        return None

    if isinstance(value, int):
        return _check_integer(schema, value, path)

    if isinstance(value, dict):
        return _check_object(schema, cast("dict[str, object]", value), path)

    if isinstance(value, list):
        return _check_array(schema, cast("list[object]", value), path)

    return None
