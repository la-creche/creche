"""The flavors of the disks row are the flavors of the wire.

`catalog.DISK_FLAVORS` names one root disk `<flavor>.ext4.zst` for each
flavor. The vmm refuses a `digests.json` with a key that is no value of the
wire enum `SandboxFlavor` (CH58 rule 4). The enum is the one source, so this
test holds the two equal (CH76 rule 7). The PR that adds a flavor to the
enum adds its disk to the row.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

from handover.catalog import DISK_FLAVORS

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The JSON schema that the wire crate writes (`CRECHE_WIRE_SCHEMA=write`).
WIRE_SCHEMA = REPO_ROOT / "rust" / "crates" / "wire" / "schema" / "wire.json"

#: The title of the enum in the schema.
FLAVOR_TITLE = "SandboxFlavor"


def _nodes(value: Any) -> Iterator[dict[str, Any]]:
    """Each object of a JSON value, depth first."""
    if isinstance(value, dict):
        node = cast(dict[str, Any], value)
        yield node
        for item in node.values():
            yield from _nodes(item)
    elif isinstance(value, list):
        for item in cast(list[Any], value):
            yield from _nodes(item)


def _wire_flavors() -> tuple[str, ...]:
    """The values of the one enum of the schema with the flavor title."""
    schema = json.loads(WIRE_SCHEMA.read_text(encoding="utf-8"))
    found = [node["enum"] for node in _nodes(schema) if node.get("title") == FLAVOR_TITLE]
    assert found, f"{WIRE_SCHEMA} names no {FLAVOR_TITLE}"
    assert all(one == found[0] for one in found), found

    return tuple(cast(list[str], found[0]))


def test_disk_flavors_are_the_wire_flavors() -> None:
    assert _wire_flavors() == DISK_FLAVORS
