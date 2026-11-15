"""The flavors of the disks row are the flavors of the wire.

`catalog.DISK_FLAVORS` names one root disk `<flavor>.ext4.zst` for each
flavor. The vmm refuses a `digests.json` with a key that is no value of the
wire enum `SandboxFlavor` (CH58 rule 4). The enum is the one source, so this
test holds the two equal (CH76 rule 7). The PR that adds a flavor to the
enum adds its disk to the row.

`catalog.SERVICE_IMAGES` names one root disk `<image>.ext4.zst` for each
value of the wire enum `ServiceImage` (browser design 10.5.2). The second
test holds the two equal through the shared vector of `digests.json`.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

from handover.catalog import DISK_FLAVORS, SERVICE_IMAGES

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


#: The shared vector of `digests.json` (`rust/crates/wire/vectors`). The
#: vmm parses it, so its keys are `kernel`, each value of `SandboxFlavor`
#: and each value of the wire enum `ServiceImage` (CH58 rule 4).
DIGESTS_VECTOR = REPO_ROOT / "rust" / "crates" / "wire" / "vectors" / "digests.json"

#: The key of the kernel entry of `digests.json`.
KERNEL_KEY = "kernel"


def test_service_images_are_the_other_keys_of_the_vector() -> None:
    """`ServiceImage` has no schema entry, as no message carries it. The
    vector holds its values: each key that is no `kernel` and no flavor.
    A service image is no flavor, so the two sets share no name."""
    keys = json.loads(DIGESTS_VECTOR.read_text(encoding="utf-8")).keys()
    others = sorted(set(keys) - {KERNEL_KEY, *DISK_FLAVORS})

    assert others == sorted(SERVICE_IMAGES)
    assert not set(SERVICE_IMAGES) & set(DISK_FLAVORS)
