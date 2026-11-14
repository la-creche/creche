"""The unit files of a `kind: units` tree (contract 06 §1 rule 12, §8.3).

`host` is the one `kind: units` component. Its tree holds two unit
directories, and nothing else under `units/`.

    units/user/<unit>                    linked at <user unit dir>/<unit>
    units/system/<unit>.d/<name>.conf    linked at /etc/systemd/system/<unit>.d/<name>.conf

`unit_files` reads that layout from a STAGED tree, after `install.py` rule
10 copied it. The copy already refused each link and each entry that is not
a directory or a regular file, so this walk meets neither. It checks them
again, because a check that trusts its caller is one caller away from
gone.

The refusal code is `unit`. A refusal at step 8 leaves the host as it was:
nothing is swapped and nothing is linked.

`creche-release`'s own `bin/check-tree` runs the same layout rules in its
pull request gate. Only this reader knows the catalog, so only this reader
refuses a component's own unit.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Final, NoReturn

from ..errors import Refusal, RefusalCode, safe_token
from ..manifest import ComponentManifest

#: The directory of a `units` tree that holds the unit files.
UNITS_DIR: Final = "units"

#: The name of a unit that a `units` tree may hold or extend: one path
#: segment, then one of the four types that a timer, a service or a path
#: unit needs. The same pattern as `creche-release`'s `bin/check-tree`.
UNIT_NAME_RE: Final = re.compile(r"[A-Za-z0-9@._-]+\.(service|timer|path|socket)")

#: The name of a drop-in file in `units/system/<unit>.d/`.
DROP_IN_NAME_RE: Final = re.compile(r"[A-Za-z0-9._-]+\.conf")

#: The end of the name of a drop-in directory.
DROP_IN_SUFFIX: Final = ".d"

#: Contract 06 §1.1 and §1 rule 12 item 2: the units that install the
#: release tool itself. A release never holds one of them, and never
#: extends one.
HANDOVER_UNITS: Final = frozenset(
    {"creche-handover.path", "creche-handover.service", "creche-handover-intake.service"}
)

#: The most unit files one `units` tree may hold. creche-release holds 13.
#: More is a stage that fails, not a loop root runs over a tree a merge can
#: fill.
MAX_UNIT_FILES: Final = 64


class Scope(StrEnum):
    """Which systemd manager a unit file belongs to (contract 06 §8.3 item 1)."""

    #: `units/user/<unit>`: a unit of the operator's user manager.
    USER = "user"
    #: `units/system/<unit>.d/<name>.conf`: a drop-in of a system unit.
    SYSTEM = "system"


@dataclass(frozen=True)
class UnitFile:
    """One unit file of a `units` tree."""

    scope: Scope
    #: The unit that this file is (`USER`) or extends (`SYSTEM`).
    unit: str
    #: Where the file is in the tree, for example `units/user/x.timer`.
    relative: PurePosixPath

    def installed(self, user_unit_dir: Path, system_unit_dir: Path) -> Path:
        """Where the file is linked on the host (contract 06 §8.3 item 1)."""
        if self.scope is Scope.USER:
            return user_unit_dir / self.unit

        return system_unit_dir / f"{self.unit}{DROP_IN_SUFFIX}" / self.relative.name


def component_units(manifests: Iterable[ComponentManifest]) -> frozenset[str]:
    """The unit that each manifest names. Contract 06 §1 rule 8 binds such a
    unit to its component's tree, so a `units` tree may not hold one."""
    return frozenset(manifest.unit for manifest in manifests if manifest.unit is not None)


def unit_files(component: str, tree: Path, owned: frozenset[str]) -> tuple[UnitFile, ...]:
    """Each unit file of the `units` tree `tree`, sorted by its path.

    `owned` is the set of the units of the other components, from
    `component_units`. The walk refuses with code `unit`, and names the
    first fault, when one of these holds.

    1. An entry under `units/` is not `units/user/<unit>` or
       `units/system/<unit>.d/<name>.conf` (contract 06 §8.3 item 1).
    2. An entry is a link, or is not a directory or a regular file.
    3. A file is, or extends, a handover unit (§1 rule 12 item 2).
    4. A user unit has the name of a component's own unit (§1 rule 12
       item 1). A drop-in may extend such a unit: rule 12 lists the drop-ins
       that extend a component's unit among what `host` holds.
    5. The tree holds more than `MAX_UNIT_FILES` files.

    A tree with no `units/` directory holds no unit file.

    CONTRACT-QUESTION: contract 06 §1 rule 12 says "the reader refuses" such
    a tree, and names no step. The reading taken refuses at step 8, on the
    staged tree, before the swap. The resolver reads manifests only, and
    the tree is first on root's disk at step 8.

    CONTRACT-QUESTION: the units of rule 12 item 1 are known here only by
    the manifests of this release. A component's sibling units, and the
    unit of a component that has no install tree, are not in `owned`. For a
    sibling that is installed as a regular file, the adopt check of §8.3
    item 3 refuses a file whose bytes differ.
    """
    root = tree / UNITS_DIR
    if not os.path.lexists(root):
        return ()

    _require(component, root, PurePosixPath(UNITS_DIR), stat.S_ISDIR)
    found: list[UnitFile] = []
    for top, dirs, files in os.walk(root):
        here = Path(top)
        for name in dirs:
            relative = _relative(tree, here / name)
            _require(component, here / name, relative, stat.S_ISDIR)

        for name in files:
            path = here / name
            relative = _relative(tree, path)
            _require(component, path, relative, stat.S_ISREG)
            found.append(_unit_file(component, relative, owned))
            if len(found) > MAX_UNIT_FILES:
                _refuse(component, f"the tree holds more than {MAX_UNIT_FILES} unit files")

    return tuple(sorted(found, key=lambda one: one.relative))


def _unit_file(component: str, relative: PurePosixPath, owned: frozenset[str]) -> UnitFile:
    """Rules 1, 3 and 4 for one regular file under `units/`."""
    parts = relative.parts
    shown = safe_token(str(relative))
    if len(parts) == 3 and parts[1] == Scope.USER and UNIT_NAME_RE.fullmatch(parts[2]):
        found = UnitFile(Scope.USER, parts[2], relative)
    elif (
        len(parts) == 4
        and parts[1] == Scope.SYSTEM
        and parts[2].endswith(DROP_IN_SUFFIX)
        and UNIT_NAME_RE.fullmatch(parts[2].removesuffix(DROP_IN_SUFFIX))
        and DROP_IN_NAME_RE.fullmatch(parts[3])
    ):
        found = UnitFile(Scope.SYSTEM, parts[2].removesuffix(DROP_IN_SUFFIX), relative)
    else:
        _refuse(
            component,
            f"{shown} is not units/user/<unit> or units/system/<unit>.d/<name>.conf",
        )

    if found.unit in HANDOVER_UNITS:
        _refuse(component, f"{shown} is or extends a handover unit, which no release holds")

    if found.scope is Scope.USER and found.unit in owned:
        _refuse(component, f"{shown} is the unit of a component, which its own tree holds")

    return found


def _relative(tree: Path, path: Path) -> PurePosixPath:
    return PurePosixPath(path.relative_to(tree).as_posix())


def _require(
    component: str, path: Path, relative: PurePosixPath, kind: Callable[[int], bool]
) -> None:
    """Rule 2: `path`, NOT followed, is of `kind`."""
    if not kind(os.lstat(path).st_mode):
        _refuse(
            component,
            f"{safe_token(str(relative))} is a link or not a directory or a regular file",
        )


def _refuse(component: str, detail: str) -> NoReturn:
    raise Refusal(RefusalCode.UNIT, component, detail)


class Change(StrEnum):
    """How one unit file of the tree that goes live differs from the file of
    the same path in the tree that was live (contract 06 §8.3 item 6)."""

    #: The tree that was live has no such file, or there was no tree.
    NEW = "new"
    #: The two files hold other bytes.
    CHANGED = "changed"
    #: The two files hold the same bytes.
    SAME = "same"


#: The end of the name of a timer. §8.3 item 6 starts a new timer and
#: restarts a changed one. A service runs its new file at its next start.
TIMER_SUFFIX: Final = ".timer"

#: The mark of a template unit (`<name>@.timer`). A template has no
#: instance of its own, so root does not start, restart, stop or disable it.
#: `creche-release`'s `bin/host-verify` skips a template timer too.
TEMPLATE_MARK: Final = "@."


@dataclass(frozen=True)
class UnitsDiff:
    """What one swap of a `units` tree does to its unit files."""

    #: Each unit file of the tree that goes live, with its change.
    now: tuple[tuple[UnitFile, Change], ...]
    #: Each unit file of the tree that was live and not of the tree that
    #: goes live (§8.3 item 4).
    gone: tuple[UnitFile, ...]


def units_diff(component: str, before: Path | None, now: Path) -> UnitsDiff:
    """Compare the unit files of the two trees, before the swap.

    `before` is the tree that is live now, or None when there is none.
    `now` is the tree that goes live: the staged tree at the switch, the
    previous tree at a restore. Both are root's own trees, and step 8 read
    the layout of each one when it staged it.
    """
    now_files = unit_files(component, now, frozenset())
    before_files = unit_files(component, before, frozenset()) if before is not None else ()
    before_paths = {one.relative for one in before_files}
    now_paths = {one.relative for one in now_files}
    changes = tuple((one, _change(one.relative, before, now, before_paths)) for one in now_files)
    gone = tuple(one for one in before_files if one.relative not in now_paths)

    return UnitsDiff(changes, gone)


def _change(
    relative: PurePosixPath, before: Path | None, now: Path, before_paths: set[PurePosixPath]
) -> Change:
    if before is None or relative not in before_paths:
        return Change.NEW

    if (before / relative).read_bytes() != (now / relative).read_bytes():
        return Change.CHANGED

    return Change.SAME


def is_timer(one: UnitFile) -> bool:
    """A user timer that root starts, restarts, stops and disables: not a
    template, and not a drop-in."""
    return (
        one.scope is Scope.USER
        and one.unit.endswith(TIMER_SUFFIX)
        and TEMPLATE_MARK not in one.unit
    )


def is_stoppable(one: UnitFile) -> bool:
    """A user unit that root stops and disables when its file goes (§8.3
    item 4): not a template, and not a drop-in."""
    return one.scope is Scope.USER and TEMPLATE_MARK not in one.unit


def drop_in_restarts(diff: UnitsDiff) -> tuple[str, ...]:
    """Contract 06 §8.3 item 6: each unit whose drop-in the swap adds,
    changes or removes, sorted, each once. Step 5 names these in §2.5's
    `restarts` row before the tap, and step 9 restarts exactly these."""
    changed = {
        one.unit
        for one, change in diff.now
        if one.scope is Scope.SYSTEM and change is not Change.SAME
    }
    removed = {one.unit for one in diff.gone if one.scope is Scope.SYSTEM}

    return tuple(sorted(changed | removed))
