"""The fence of the host (contract 01 §5.5 rule 1).

A fence that the fenced thing can edit is not a fence. The registry applies
with no tap, so this value is host config, not a registry file: a key of the
site file `/etc/creche/site.env`, which root owns. `caregiver` reads it at
start and passes it to `load_registry`. A reader with no fences (registry
CI, the `agent-family` command) gets one downgraded warning for each fence
check instead (`validate.py`).

`AGENT_PLATFORM_FAMILIES`: the families that may hold the platform server
and the `release` verb, separated by commas. Absent or empty: none.

Contract 01 minor 18 drops `files`, the allowed mount roots and the key
`AGENT_MOUNT_ROOTS` that held them (decision CH10-10). A VM mounts no host
folder.

A fault names the key and the position of the entry, never the value."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from .grammar import FAMILY_NAME

PLATFORM_FAMILIES_KEY: Final = "AGENT_PLATFORM_FAMILIES"
SEPARATOR: Final = ","


@dataclass(frozen=True)
class Fences:
    """The platform families of one host."""

    platform_families: frozenset[str]


class FenceError(ValueError):
    """The fence key of the host holds an entry of the wrong shape. The
    message names the key and the position."""


def read_fences(environ: Mapping[str, str]) -> Fences:
    """The fence of `environ`. Raises `FenceError` on the first fault."""
    return Fences(platform_families=_platform_families(environ.get(PLATFORM_FAMILIES_KEY, "")))


def _platform_families(value: str) -> frozenset[str]:
    if not value:
        return frozenset()

    families = value.split(SEPARATOR)
    for position, family in enumerate(families):
        if FAMILY_NAME.fullmatch(family) is None:
            raise FenceError(f"{PLATFORM_FAMILIES_KEY} entry {position} is not a family name")

    if len(set(families)) != len(families):
        raise FenceError(f"{PLATFORM_FAMILIES_KEY} names one family twice")

    return frozenset(families)
