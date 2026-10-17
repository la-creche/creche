"""The unpack of a tar archive, as root.

`mcpbuild.py` unpacks the release asset of an MCP server here, after it
checked the pinned hash. The module also reads the archive of a binary
component, which CI builds and signs (`artifacts.py` of the pure half). It
is the one place of the executor that opens a tar archive.

Each member is checked before a byte of it is written. Five shapes are
refused in each archive:

1. An absolute name.
2. A name that holds a `..` segment.
3. A symbolic link or a hard link. A link leaves the tree after the unpack,
   so it is refused by its kind and not by its target.
4. A member that is no regular file and no directory: a device, a FIFO or
   a socket.
5. A member with the set-user-ID bit or the set-group-ID bit.

A member whose path resolves outside the target directory is refused too.

The members are counted, and their sizes are added, against two limits. A
hash or a signature proves where an archive came from. It does not prove
that the archive is small.

The archive of a binary component has more rules. The caller names the
component:

1. Each member is a regular file named `<component>/bin/<program>`, and
   `<program>` is one plain path segment.
2. A directory member is refused, and so is each other name.
3. A second member with a name that the unpack already took is refused.
4. The archive is a gzip of a tar, and nothing else.
5. The limits are `catalog.ARCHIVE_MAX_MEMBERS` and
   `catalog.ARCHIVE_MAX_UNPACKED_BYTES`. The limits of an MCP asset are
   the two constants of this module.

A refusal raises `UnpackRefused`. Its detail is a member of the closed set
`UnpackFault`, so no byte of an archive reaches a refusal. Each caller turns
it into a refusal of its own.
"""

from __future__ import annotations

import tarfile
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, Literal

from ..catalog import ARCHIVE_MAX_MEMBERS, ARCHIVE_MAX_UNPACKED_BYTES
from ..manifest import PATH_SEGMENT_RE

#: Each read of a file in the executor goes in blocks of this size, so a
#: large file never arrives in memory whole.
BLOCK_BYTES: Final = 1024 * 1024

#: The two limits of the release asset of an MCP server.
ASSET_MAX_MEMBERS: Final = 20_000
ASSET_MAX_UNPACKED_BYTES: Final = 512 * 1024 * 1024

#: The directory of the programs, in a member name of a component archive
#: and in the unpacked tree.
PROGRAM_DIR: Final = "bin"

#: The mode of each program of a component archive, whatever the member says.
PROGRAM_MODE: Final = 0o755

#: The bits that a file of an MCP asset keeps from its member.
ASSET_MODE_BITS: Final = 0o755

#: The set-user-ID bit and the set-group-ID bit.
SET_ID_BITS: Final = 0o4000 | 0o2000

#: The parts of a member name of a component archive:
#: `<component>/bin/<program>`.
MEMBER_PARTS: Final = 3

#: The two path segments that a plain segment never is.
DOT_SEGMENTS: Final = frozenset({".", ".."})

#: How each kind of archive is opened. An MCP asset can have each
#: compression that `tarfile` reads. A component archive is a gzip of a tar.
type ReadMode = Literal["r:*", "r:gz"]
ASSET_MODE: Final[ReadMode] = "r:*"
COMPONENT_MODE: Final[ReadMode] = "r:gz"


class UnpackFault(StrEnum):
    """Each refusal of an unpack, with its fixed detail text."""

    LEAVES_TREE = "the asset holds a member that leaves its tree"
    LINK = "the asset holds a link"
    NOT_FILE_OR_DIRECTORY = "the asset holds a member that is not a file or a directory"
    SET_ID = "the asset holds a setuid or setgid member"
    OUTSIDE_TREE = "the asset holds a member that resolves outside its tree"
    NO_CONTENT = "the asset holds a member with no content"
    NOT_READABLE = "the asset is not a readable archive"
    ASSET_MEMBERS = f"the asset holds more than {ASSET_MAX_MEMBERS} members"
    ASSET_BYTES = f"the asset unpacks to more than {ASSET_MAX_UNPACKED_BYTES}"
    DIRECTORY = "the archive holds a directory member"
    NOT_A_PROGRAM = "the archive holds a member that is not a program of its component"
    SAME_NAME = "the archive holds two members with one name"
    ARCHIVE_MEMBERS = f"the archive holds more than {ARCHIVE_MAX_MEMBERS} members"
    ARCHIVE_BYTES = f"the archive unpacks to more than {ARCHIVE_MAX_UNPACKED_BYTES} bytes"


class UnpackRefused(Exception):
    """An archive that the unpack refuses. `detail` is a fixed text."""

    def __init__(self, fault: UnpackFault) -> None:
        super().__init__(str(fault))
        self.fault = fault

    @property
    def detail(self) -> str:
        return str(self.fault)


@dataclass(frozen=True)
class _Rules:
    """What one kind of archive may hold."""

    mode: ReadMode
    max_members: int
    max_bytes: int
    too_many: UnpackFault
    too_large: UnpackFault
    #: The component of a component archive, or None for an MCP asset.
    component: str | None


def _rules(component: str | None) -> _Rules:
    if component is None:
        return _Rules(
            mode=ASSET_MODE,
            max_members=ASSET_MAX_MEMBERS,
            max_bytes=ASSET_MAX_UNPACKED_BYTES,
            too_many=UnpackFault.ASSET_MEMBERS,
            too_large=UnpackFault.ASSET_BYTES,
            component=None,
        )

    return _Rules(
        mode=COMPONENT_MODE,
        max_members=ARCHIVE_MAX_MEMBERS,
        max_bytes=ARCHIVE_MAX_UNPACKED_BYTES,
        too_many=UnpackFault.ARCHIVE_MEMBERS,
        too_large=UnpackFault.ARCHIVE_BYTES,
        component=component,
    )


@dataclass(frozen=True)
class _Placed:
    """One member that passed each check, and its path below the target."""

    member: tarfile.TarInfo
    relative: str


def extract_tar(archive: Path, into: Path, component: str | None = None) -> None:
    """Unpack `archive` into `into`. Each member is checked before a byte of
    it is written.

    With no `component`, `archive` is the release asset of an MCP server:
    each member goes to its own name below `into`, and a file keeps its mode
    bits inside 0755. With `component`, `archive` is the archive of that
    binary component: each member goes to `<into>/bin/<program>` with the
    mode 0755.

    Raises `UnpackRefused` for each refused shape and for each limit.
    """
    into.mkdir(parents=True, exist_ok=True)
    root = into.resolve()
    rules = _rules(component)
    try:
        with tarfile.open(archive, rules.mode) as tar:
            for placed in _walk(tar, rules):
                _write_member(tar, placed, root, rules)
    except tarfile.TarError:
        raise UnpackRefused(UnpackFault.NOT_READABLE) from None


def list_component_archive(archive: Path, component: str) -> tuple[str, ...]:
    """The program names of the archive of `component`, in the order of the
    archive. Each member header gets the checks of the unpack, and no file
    is written.

    Raises `UnpackRefused` for each shape that `extract_tar` refuses for that
    component, the two limits included.
    """
    rules = _rules(component)
    try:
        with tarfile.open(archive, rules.mode) as tar:
            return tuple(Path(placed.relative).name for placed in _walk(tar, rules))
    except tarfile.TarError:
        raise UnpackRefused(UnpackFault.NOT_READABLE) from None


def _walk(tar: tarfile.TarFile, rules: _Rules) -> Iterator[_Placed]:
    """Each member in the order of the archive, after its checks and the two
    limits."""
    written = 0
    taken: set[str] = set()
    for count, member in enumerate(tar, start=1):
        _check_member(member)
        relative = _place(member, rules, taken)
        if count > rules.max_members:
            raise UnpackRefused(rules.too_many)

        written += member.size
        if written > rules.max_bytes:
            raise UnpackRefused(rules.too_large)

        yield _Placed(member, relative)


def _check_member(member: tarfile.TarInfo) -> None:
    """The refused shapes that do not need the target directory."""
    path = Path(member.name)
    if path.is_absolute() or ".." in path.parts:
        raise UnpackRefused(UnpackFault.LEAVES_TREE)

    if member.issym() or member.islnk():
        raise UnpackRefused(UnpackFault.LINK)

    if not member.isfile() and not member.isdir():
        raise UnpackRefused(UnpackFault.NOT_FILE_OR_DIRECTORY)

    if member.mode & SET_ID_BITS:
        raise UnpackRefused(UnpackFault.SET_ID)


def _place(member: tarfile.TarInfo, rules: _Rules, taken: set[str]) -> str:
    """The path of `member` below the target directory. For a component
    archive it is `bin/<program>`, and each other member is refused."""
    if rules.component is None:
        return member.name

    if member.isdir():
        raise UnpackRefused(UnpackFault.DIRECTORY)

    parts = member.name.split("/")
    if len(parts) != MEMBER_PARTS:
        raise UnpackRefused(UnpackFault.NOT_A_PROGRAM)

    owner, directory, program = parts
    plain = program not in DOT_SEGMENTS and PATH_SEGMENT_RE.fullmatch(program) is not None
    if owner != rules.component or directory != PROGRAM_DIR or not plain:
        raise UnpackRefused(UnpackFault.NOT_A_PROGRAM)

    if program in taken:
        raise UnpackRefused(UnpackFault.SAME_NAME)

    taken.add(program)

    return f"{PROGRAM_DIR}/{program}"


def _write_member(tar: tarfile.TarFile, placed: _Placed, root: Path, rules: _Rules) -> None:
    member = placed.member
    target = (root / placed.relative).resolve()
    if target != root and root not in target.parents:
        raise UnpackRefused(UnpackFault.OUTSIDE_TREE)

    if member.isdir():
        target.mkdir(parents=True, exist_ok=True)

        return

    target.parent.mkdir(parents=True, exist_ok=True)
    source = tar.extractfile(member)
    if source is None:
        raise UnpackRefused(UnpackFault.NO_CONTENT)

    with target.open("wb") as handle:
        for block in iter(lambda: source.read(BLOCK_BYTES), b""):
            handle.write(block)

    mode = member.mode & ASSET_MODE_BITS if rules.component is None else PROGRAM_MODE
    target.chmod(mode)


__all__ = [
    "ASSET_MAX_MEMBERS",
    "ASSET_MAX_UNPACKED_BYTES",
    "BLOCK_BYTES",
    "UnpackFault",
    "UnpackRefused",
    "extract_tar",
    "list_component_archive",
]
