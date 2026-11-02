"""The follow policy of a host: `host.toml` in the live `host` tree.

`handover follow` with no names reads this file. `creche-release` holds it,
and a tapped `host` release installs it at `<install root>/host/host.toml`.
The follower reads that LIVE copy, never a clone, so a change to the policy
waits for a tap too (creche-release-design.md §5.2 item 3, §5.3).

    [follow]
    chaperone = "newest"     # follow each new tag
    attendance = "0.4.2"     # a ceiling: move up to 0.4.2, never above it

    [registry]
    mcp = "follow"           # or "hold"

Five rules hold the text. A text that breaks one is a `PolicyError`, and
the run files nothing.

1. The top table has exactly two keys: `follow` and `registry`. Each is a
   table.
2. Each key of `[follow]` is a releasable component of the catalog. A
   component that `[follow]` does not name is not followed.
3. Each value of `[follow]` is `newest` or a `MAJOR.MINOR.PATCH` version.
   A version is a ceiling: the follower moves the component up to it and
   never above it.
4. `[registry]` has exactly one key, `mcp`, and its value is `follow` or
   `hold`. `follow` runs the registry watch (`follow/` rule 8).
5. `mcp = "follow"` needs `mcp-servers` in `[follow]`: the watch files a
   redeploy of `mcp-servers`, and only a followed component gets one.

A text of the file is never in an error. An error names a key with
`safe_token`.

Stdlib only, like the rest of `follow/`.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, cast

from .. import tomlfile
from ..catalog import releasable_names
from ..errors import safe_token
from ..state import VERSION_RE
from . import REDEPLOYED, REGISTRY_KEY

#: The component whose live tree holds the policy.
HOST: Final = "host"

#: The name of the policy file in that tree.
POLICY_NAME: Final = "host.toml"

#: The two tables of rule 1.
FOLLOW_TABLE: Final = "follow"
REGISTRY_TABLE: Final = "registry"

#: The value of rule 3 that follows each new tag.
NEWEST: Final = "newest"

_READ_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


class Registry(StrEnum):
    """The value of `[registry] mcp` (rule 4)."""

    #: Run the registry watch.
    FOLLOW = "follow"
    #: File no redeploy for a registry merge.
    HOLD = "hold"


class PolicyError(ValueError):
    """The policy file is absent, unreadable, or breaks one of the five
    rules. The text names the fault and holds no byte of the file."""


@dataclass(frozen=True)
class Policy:
    """What the follower follows, read from the live `host.toml`."""

    #: Each followed component and its ceiling. None follows each new tag.
    follow: Mapping[str, str | None]
    #: Whether the registry watch runs.
    registry: Registry


def policy_file(roots: tuple[Path, ...]) -> Path | None:
    """`host.toml` of the first root that holds a `host` directory, or None
    when no root does. The same walk as the executor's `read_trees`: a
    component installs under exactly one root, and a link is no tree."""
    for root in roots:
        tree = root / HOST
        if tree.is_dir() and not tree.is_symlink():
            return tree / POLICY_NAME

    return None


def read_policy(roots: tuple[Path, ...]) -> Policy:
    """The policy of the live `host` tree. No link is followed, and a file
    above `tomlfile.FileKind.POLICY`'s byte limit is not read."""
    path = policy_file(roots)
    if path is None:
        raise PolicyError(
            f"no install root holds a {HOST} tree, so there is no {POLICY_NAME}: "
            f"name the components, or release {HOST} first"
        )

    return parse_policy(_read(path))


def _read(path: Path) -> bytes:
    limit = tomlfile.FileKind.POLICY.byte_limit
    try:
        fd = os.open(path, _READ_FLAGS)
    except OSError as error:
        raise PolicyError(f"cannot open {path}: {error.strerror or 'no reason'}") from None

    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise PolicyError(f"{path} is not a regular file")

        # One byte past the limit, so `tomlfile.loads` refuses the size.
        return os.read(fd, limit + 1)
    except OSError as error:
        raise PolicyError(f"cannot read {path}: {error.strerror or 'no reason'}") from None
    finally:
        os.close(fd)


def parse_policy(data: bytes) -> Policy:
    """The five rules, in their sequence. The first fault is the error."""
    try:
        top = tomlfile.loads(data, tomlfile.FileKind.POLICY)
    except tomlfile.TomlRefusal as refusal:
        raise PolicyError(f"{POLICY_NAME}: {refusal}") from None

    _exact_keys(top, (FOLLOW_TABLE, REGISTRY_TABLE), POLICY_NAME)
    follow = _table(top, FOLLOW_TABLE)
    registry = _table(top, REGISTRY_TABLE)
    followed = {name: _ceiling(name, value) for name, value in follow.items()}

    _exact_keys(registry, (REGISTRY_KEY,), f"[{REGISTRY_TABLE}]")
    word = registry[REGISTRY_KEY]
    if word not in tuple(Registry):
        raise PolicyError(
            f"[{REGISTRY_TABLE}] {REGISTRY_KEY} is {safe_token(word)}, "
            f"not {Registry.FOLLOW} or {Registry.HOLD}"
        )

    watch = Registry(word)
    if watch is Registry.FOLLOW and REDEPLOYED not in followed:
        raise PolicyError(
            f"[{REGISTRY_TABLE}] {REGISTRY_KEY} = {Registry.FOLLOW} needs {REDEPLOYED} "
            f"in [{FOLLOW_TABLE}]"
        )

    return Policy(follow=followed, registry=watch)


def _exact_keys(table: Mapping[str, object], keys: tuple[str, ...], where: str) -> None:
    """Rules 1 and 4: the table has exactly `keys`."""
    for key in sorted(table):
        if key not in keys:
            raise PolicyError(f"{where} has the key {safe_token(key)}, which no rule names")

    for key in keys:
        if key not in table:
            raise PolicyError(f"{where} has no key {key}")


def _table(top: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = top[key]
    if not isinstance(value, dict):
        raise PolicyError(f"{key} is not a table")

    # A TOML table has string keys only.
    return cast("dict[str, object]", value)


def _ceiling(name: str, value: object) -> str | None:
    """Rules 2 and 3 for one row of `[follow]`."""
    if name not in releasable_names():
        raise PolicyError(f"[{FOLLOW_TABLE}] names {safe_token(name)}, which cannot release")

    if value == NEWEST:
        return None

    if isinstance(value, str) and VERSION_RE.fullmatch(value):
        return value

    raise PolicyError(
        f"[{FOLLOW_TABLE}] {name} is {safe_token(value)}, not {NEWEST} or MAJOR.MINOR.PATCH"
    )
