"""Turning an edited family back into `family.yaml` text.

The view never imports a YAML library. It reads through `agent_family`,
which owns the reader, and it writes through this emitter. One layer talks
to the layer below it, and the registry keeps exactly one YAML parser.

The emitter covers the value space of contract 01's file and nothing else:
mappings, lists, strings, numbers, booleans and null. Two rules make that
safe.

1. **Every string is double-quoted, through `json.dumps`.** YAML 1.2 is a
   superset of JSON, and every escape `json.dumps` emits is a legal
   double-quoted YAML escape. So a description holding a colon, a path
   holding a `#`, and a value that would read as a number or as `yes` all
   come back as the string that went in. A plain scalar would not.
2. **The writer proves its own output.** `registrywrite.py` re-parses the
   text with `agent_family.parse_family` and refuses the save unless the
   model that comes back equals the model that went in (invariant 19). A
   quoting bug cannot reach the registry. It can only fail a save.

This emitter is the PREVIEW and the fallback, not the save. `yamlkeep.py`
patches the file that is there, so a save keeps its comments; this module
answers when there is no file to patch, or when one cannot be round-tripped.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Final, cast

#: Two spaces per level, as the registry is written today. The list writer
#: below relies on this being the width of `"- "`.
INDENT: Final = "  "

_DASH: Final = "- "

#: A key that needs no quoting. Every key in contract 01 is a field name,
#: and an MCP server name under `tools:` is `[a-z][a-z0-9-]*`. Anything
#: else is quoted rather than refused: refusing a key the schema accepted
#: would leave the form unable to save a file the reader accepted.
_PLAIN_KEY: Final = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.")

_EMPTY_MAP: Final = "{}"
_EMPTY_LIST: Final = "[]"


def to_yaml(document: Mapping[str, object]) -> str:
    """One YAML document, block style, ending in a newline."""
    lines: list[str] = []
    _write_map(document, 0, lines)

    return "".join(one + "\n" for one in lines)


def _write_map(body: Mapping[str, object], depth: int, lines: list[str]) -> None:
    pad = INDENT * depth

    for key, value in body.items():
        name = _key(str(key))

        if not _is_block(value):
            lines.append(f"{pad}{name}: {_scalar(value)}")
            continue

        # A container opens on its own line, with its body one level in.
        lines.append(f"{pad}{name}:")
        _write_any(value, depth + 1, lines)


def _write_list(body: Sequence[object], depth: int, lines: list[str]) -> None:
    pad = INDENT * depth

    for value in body:
        if not _is_block(value):
            lines.append(f"{pad}{_DASH}{_scalar(value)}")
            continue

        # Render the item one level in, then lift its first line onto the
        # dash. `"- "` is exactly one INDENT wide, so every following line
        # already sits under the lifted one:
        #     files:
        #       - path: "/srv/agents/vault"
        #         mode: "ro"
        inner: list[str] = []
        _write_any(value, depth + 1, inner)
        lines.append(f"{pad}{_DASH}{inner[0].lstrip()}")
        lines.extend(inner[1:])


def _write_any(value: object, depth: int, lines: list[str]) -> None:
    if isinstance(value, Mapping):
        _write_map(cast("Mapping[str, object]", value), depth, lines)
        return

    _write_list(cast("Sequence[object]", value), depth, lines)


def _is_block(value: object) -> bool:
    """True for a container with something in it.

    An EMPTY container is written as a scalar: `tools: {}` and `egress: []`
    say "nothing is granted", while an opened block with no body under it
    would read as null, which is a different document.
    """
    if isinstance(value, Mapping):
        return len(cast("Mapping[str, object]", value)) > 0

    if isinstance(value, list | tuple):
        return len(cast("Sequence[object]", value)) > 0

    return False


def _scalar(value: object) -> str:
    """One scalar, or the spelling of an empty container."""
    if value is None:
        return "null"

    if isinstance(value, bool):
        return "true" if value else "false"

    if isinstance(value, int | float):
        return json.dumps(value)

    if isinstance(value, str):
        return json.dumps(value)

    if isinstance(value, Mapping):
        return _EMPTY_MAP

    if isinstance(value, list | tuple):
        return _EMPTY_LIST

    # Nothing else comes out of a pydantic `model_dump(mode="json")`.
    # Quoting the repr keeps the document parseable, so the round-trip
    # check rejects it with a report instead of an exception.
    return json.dumps(repr(value))


def _key(name: str) -> str:
    if name and all(one in _PLAIN_KEY for one in name):
        return name

    return json.dumps(name)
