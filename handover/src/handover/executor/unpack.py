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

1. Each member is a plain regular file named `<component>/bin/<program>`,
   and `<program>` is one plain path segment. A sparse member is not a
   plain regular file.
2. A directory member is refused, and so is each other name.
3. A second member with a name that the unpack already took is refused.
4. The archive is a gzip of a tar, and nothing else. The module reads the
   gzip stream to its end, so the check of its CRC and of its length runs
   for each archive. A gzip stream that ends early, that does not
   decompress or that fails that check is not a readable archive. A member
   header that `tarfile` cannot convert is not a readable archive either.
5. The limits are `catalog.ARCHIVE_MAX_MEMBERS` and
   `catalog.ARCHIVE_MAX_UNPACKED_BYTES`. The limits of an MCP asset are
   the two constants of this module.
6. The tar stream holds the bytes of the members that the walk took, and
   `FRAME_BYTES` more at most. `tarfile` reads a whole extended header
   into memory before the walk sees its member, so this bound also holds
   the memory of that read.
7. The unpack makes the target directory itself. Each program is a new
   file, and no link is followed at its name.

A refusal raises `UnpackRefused`. Its detail is a member of the closed set
`UnpackFault`, so no byte of an archive reaches a refusal. Each caller turns
it into a refusal of its own.
"""

from __future__ import annotations

import gzip
import io
import os
import tarfile
import zlib
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import IO, Final

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

#: How an MCP asset is opened: with each compression that `tarfile` reads.
#: A component archive goes through the gzip reader of this module, and
#: `tarfile` reads the tar stream from it in order (`"r|"`).
ASSET_MODE: Final = "r:*"

#: The errors that make an archive not readable. For an MCP asset it is the
#: error of `tarfile`. The gzip stream of a component archive can also end
#: early (`EOFError`), hold a block that does not decompress (`zlib.error`),
#: or fail the check of its CRC or of its length (`gzip.BadGzipFile`). A
#: member header of a component archive with a value that `tarfile` cannot
#: convert, for example a sparse size that is no number, gives `ValueError`.
ASSET_UNREADABLE: Final = (tarfile.TarError,)
COMPONENT_UNREADABLE: Final = (
    tarfile.TarError,
    EOFError,
    zlib.error,
    gzip.BadGzipFile,
    ValueError,
)

#: The bytes of a component archive around the bytes of its members, at
#: most. Each member has a header, can have the header and the block of a
#: long name, and pads its last block: four blocks. The walk reads the
#: header of one member past the limit of members. The end of the archive
#: is two blocks and the pad of its last record, and `tarfile` reads one
#: record ahead.
#:
#: CONTRACT-QUESTION: contract 06 §8 says that the unpack counts members
#: and bytes. It names no bound for the bytes of a tar header or for the
#: bytes after the end of the tar. The reading taken: the tar stream holds
#: the bytes of its members and this frame at most, and each byte past
#: that refuses the archive. The size of a sparse member is not the count
#: of its bytes in the stream, so the unpack refuses a sparse member as a
#: member that is not a plain regular file. A change costs this constant.
FRAME_BYTES: Final = (
    (ARCHIVE_MAX_MEMBERS + 1) * 4 * tarfile.BLOCKSIZE
    + 2 * tarfile.BLOCKSIZE
    + 2 * tarfile.RECORDSIZE
)

#: The open of a new file as root: a name that is there already is an
#: error, and no link is followed at the name. Each program of a component
#: archive and each file of root's copy of an asset (`artifact.py`) is
#: opened so.
NEW_FILE_FLAGS: Final = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC


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
    EXTRA_BYTES = "the archive holds more bytes than its members and their headers"


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

    unreadable: tuple[type[Exception], ...]
    max_members: int
    max_bytes: int
    too_many: UnpackFault
    too_large: UnpackFault
    #: The component of a component archive, or None for an MCP asset.
    component: str | None


def _rules(component: str | None) -> _Rules:
    if component is None:
        return _Rules(
            unreadable=ASSET_UNREADABLE,
            max_members=ASSET_MAX_MEMBERS,
            max_bytes=ASSET_MAX_UNPACKED_BYTES,
            too_many=UnpackFault.ASSET_MEMBERS,
            too_large=UnpackFault.ASSET_BYTES,
            component=None,
        )

    return _Rules(
        unreadable=COMPONENT_UNREADABLE,
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


#: What the walk tells the reader of an archive about each member that it
#: took: the size of the member.
type _Took = Callable[[int], None]


def extract_tar(archive: Path, into: Path, component: str | None = None) -> None:
    """Unpack `archive` into `into`. Each member is checked before a byte of
    it is written.

    With no `component`, `archive` is the release asset of an MCP server:
    each member goes to its own name below `into`, and a file keeps its mode
    bits inside 0755. With `component`, `archive` is the archive of that
    binary component: each member goes to `<into>/bin/<program>` with the
    mode 0755. The unpack then makes `into` itself, so `into` must not be
    there, and its parent is a directory that only root can write.

    Raises `UnpackRefused` for each refused shape and for each limit, and
    `OSError` when the `into` of a component archive is there already.
    """
    if component is None:
        into.mkdir(parents=True, exist_ok=True)
    else:
        into.mkdir()

    root = into.resolve()
    rules = _rules(component)
    with _refusing(rules), _opened(archive, rules) as (tar, took):
        for placed in _walk(tar, rules, took):
            _write_member(tar, placed, root, rules)


def list_component_archive(archive: Path, component: str) -> tuple[str, ...]:
    """The program names of the archive of `component`, in the order of the
    archive. Each member header gets the checks of the unpack, and no file
    is written.

    Raises `UnpackRefused` for each shape that `extract_tar` refuses for that
    component, the two limits included.
    """
    rules = _rules(component)
    with _refusing(rules), _opened(archive, rules) as (tar, took):
        return tuple(Path(placed.relative).name for placed in _walk(tar, rules, took))


@contextmanager
def _refusing(rules: _Rules) -> Generator[None]:
    """Each error of `rules.unreadable` becomes the refusal `NOT_READABLE`."""
    try:
        yield
    except rules.unreadable:
        raise UnpackRefused(UnpackFault.NOT_READABLE) from None


@contextmanager
def _opened(archive: Path, rules: _Rules) -> Generator[tuple[tarfile.TarFile, _Took]]:
    """The open archive, and what the walk tells its reader. After the walk,
    the reader of a component archive reads the gzip stream to its end."""
    if rules.component is None:
        with tarfile.open(archive, ASSET_MODE) as tar:
            yield tar, _count_nothing

        return

    with archive.open("rb") as raw, gzip.GzipFile(fileobj=raw, mode="rb") as unzipped:
        stream = _Counted(unzipped)
        with tarfile.open(fileobj=stream, mode="r|") as tar:
            yield tar, stream.took

        stream.drain()


def _count_nothing(size: int) -> None:
    """An MCP asset has no bound of its tar stream."""
    del size


class _Counted(io.BufferedIOBase):
    """The tar stream of a component archive, as the gzip reader gives it.

    It gives no byte past the bytes of the members that the walk took and
    `FRAME_BYTES`. Each byte past that refuses the archive.
    """

    def __init__(self, source: gzip.GzipFile) -> None:
        super().__init__()
        self._source = source
        self._left = FRAME_BYTES

    def readable(self) -> bool:
        return True

    def took(self, size: int) -> None:
        """The walk took a member of `size` bytes."""
        self._left += size

    def read(self, size: int | None = -1, /) -> bytes:
        want = self._left + 1 if size is None or size < 0 else min(size, self._left + 1)
        block = self._source.read(want)
        self._left -= len(block)
        if self._left < 0:
            raise UnpackRefused(UnpackFault.EXTRA_BYTES)

        return block

    def drain(self) -> None:
        """Read the rest of the stream. The gzip reader checks the CRC and
        the length of the stream at its end."""
        while self.read(BLOCK_BYTES):
            continue


def _walk(tar: tarfile.TarFile, rules: _Rules, took: _Took) -> Iterator[_Placed]:
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

        took(member.size)
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

    if member.issparse():
        raise UnpackRefused(UnpackFault.NOT_A_PROGRAM)

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

    if rules.component is None:
        with target.open("wb") as handle:
            _copy_blocks(source, handle)

        target.chmod(member.mode & ASSET_MODE_BITS)

        return

    with os.fdopen(os.open(target, NEW_FILE_FLAGS, PROGRAM_MODE), "wb") as handle:
        os.fchmod(handle.fileno(), PROGRAM_MODE)
        _copy_blocks(source, handle)


def _copy_blocks(source: IO[bytes], handle: IO[bytes]) -> None:
    for block in iter(lambda: source.read(BLOCK_BYTES), b""):
        handle.write(block)


__all__ = [
    "ASSET_MAX_MEMBERS",
    "ASSET_MAX_UNPACKED_BYTES",
    "BLOCK_BYTES",
    "NEW_FILE_FLAGS",
    "UnpackFault",
    "UnpackRefused",
    "extract_tar",
    "list_component_archive",
]
