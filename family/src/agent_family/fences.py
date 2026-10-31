"""The two fences of the host (contract 01 §5.4, §5.5 rule 1).

A fence that the fenced thing can edit is not a fence. The registry applies
with no tap, so these two values are host config, not registry files: two
keys of the site file `/etc/creche/site.env`, which root owns. `caregiver`
reads them at start and passes them to `load_registry`. A reader with no
fences (registry CI, the `agent-family` command) gets one downgraded warning
for each fence check instead (`validate.py`).

1. `AGENT_MOUNT_ROOTS`: the roots a family file may mount a path under,
   separated by commas. Required.
2. `AGENT_PLATFORM_FAMILIES`: the families that may hold the platform server
   and the `release` verb, separated by commas. Absent or empty: none.

A fault names the key and the position of the entry, never the value."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from .grammar import FAMILY_NAME, MOUNT_PATH, SESSION_ROOT, is_under

MOUNT_ROOTS_KEY: Final = "AGENT_MOUNT_ROOTS"
PLATFORM_FAMILIES_KEY: Final = "AGENT_PLATFORM_FAMILIES"
SEPARATOR: Final = ","


@dataclass(frozen=True)
class Fences:
    """The allowed mount roots and the platform families of one host."""

    mount_roots: tuple[str, ...]
    platform_families: frozenset[str]


class FenceError(ValueError):
    """A fence key of the host is absent or holds an entry of the wrong
    shape. The message names the key and the position."""


def read_fences(environ: Mapping[str, str]) -> Fences:
    """The two fences of `environ`. Raises `FenceError` on the first fault."""
    return Fences(
        mount_roots=_mount_roots(environ.get(MOUNT_ROOTS_KEY, "")),
        platform_families=_platform_families(environ.get(PLATFORM_FAMILIES_KEY, "")),
    )


def _mount_roots(value: str) -> tuple[str, ...]:
    if not value:
        raise FenceError(f"{MOUNT_ROOTS_KEY} is not set; no family may mount a path without it")

    roots = tuple(value.split(SEPARATOR))
    for position, root in enumerate(roots):
        if not _is_root(root):
            raise FenceError(
                f"{MOUNT_ROOTS_KEY} entry {position} is not an absolute path of segments "
                "[A-Za-z0-9._-] with no '.', '..' or empty segment"
            )

        if is_under(root, SESSION_ROOT) or is_under(SESSION_ROOT, root):
            raise FenceError(f"{MOUNT_ROOTS_KEY} entry {position} holds the session store")

    if len(set(roots)) != len(roots):
        raise FenceError(f"{MOUNT_ROOTS_KEY} names one root twice")

    return roots


def _is_root(root: str) -> bool:
    """Absolute, at least one segment, and no `.`, `..` or empty segment."""
    if not root.startswith("/") or MOUNT_PATH.fullmatch(root) is None:
        return False

    return all(part not in ("", ".", "..") for part in root.split("/")[1:])


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
