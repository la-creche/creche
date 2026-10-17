"""Root's copy of the two assets of a binary component, at step 2.

The operator side fetches the archive and the bundle of its signature into
the store below the home of the operator (`corpus/artifacts.py`):

    <operator home>/.local/state/creche/artifacts/<catalog repo>/<tag>/<asset>

The operator owns each directory and each file there. So root reads the
store one time, at step 2, and copies the two files into the work tree of
the request. Each later read is a read of that copy:

    <work root>/<request id>/artifacts/<component>/<asset>

Seven rules hold the copy:

1. **Root is not the deputy of the operator.** Root opens each directory of
   the store path below the operator home with no link followed, and the
   operator must own each one. Root opens each file with no link followed
   and checks the open descriptor: a regular file, the uid of the operator,
   no bit for group or other, one name only, and one byte at least and the
   limit of the asset at most. So root copies only a file that the operator
   can read (`handover/AGENTS.md`, rule 5a).
2. **A FIFO at a path does not block root.** The open has `O_NONBLOCK`, and
   the check of the descriptor comes before the first read.
3. **The read stops past the limit, whatever the size said.** Root reads
   through the descriptor in blocks. A file that grows past its limit while
   root reads it gives no copy.
4. **Root sets each mode by hand.** Each directory from `<request id>` down
   gets 0755, and each file 0644. The release unit runs with the umask
   0027, and the verifier reads the copy later as a dynamic user.
5. **The digest is the SHA-256 of the archive bytes that root wrote**, as
   `sha256:<64 hex>`. It goes into `artifact_digest` of the resolved
   manifest, so the gate id and the tap bind those bytes.
6. **Root reads the store one time in a release.** The release keeps the
   result of the first call and gives it to each later call. A later call
   never opens the store: it gives the `NoCopy` of the first call again,
   or it reads root's copy and hashes it again. The first call makes the
   directory of the component in the work tree, and it copies nothing when
   that directory is there already.
7. **Nothing here raises.** Each fault gives no copy and one fixed note
   line. A component with no copy keeps a null `artifact_digest`.

`Release._remove_fetched` removes the whole work tree of the request at the
end of the release, and the copy with it.
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from ..allocate import TAG_RE
from ..artifacts import asset_limits
from ..catalog import CATALOG_BY_NAME, Kind
from ..errors import Refusal
from ..site import operator_home
from .install import DIR_MODE
from .quiet import CHECKED_READ_FLAGS, GROUP_OTHER_BITS
from .source import DIGEST_PREFIX, artifact_store
from .unpack import BLOCK_BYTES, NEW_FILE_FLAGS

#: The directory of the copies in the work tree of a request. A component is
#: never named so: its clone is a directory of the same work tree.
COPY_DIR: Final = "artifacts"

#: The mode of each file of the copy.
FILE_MODE: Final = 0o644

#: The open of a directory of the store path. No link is followed.
_DIR_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC

#: The open of the operator home. The site file names it, and its parent is
#: not the operator's.
_HOME_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC

NOT_ITS_TAG: Final = "{component}: the tag is not a tag of this component, so root copies nothing"
NO_ASSETS: Final = "{component}: the release tool names no archive and bundle for this component"
NO_COPY_DIR: Final = "{component}: root could not make the directory of its copy"
NO_HOME: Final = "{component}: the site file gives no home of the operator, so root copies nothing"
NO_STORE_DIR: Final = "{component}: a directory of the store path is absent or is a link"
NOT_OPERATORS_DIR: Final = "{component}: a directory of the store path is not the operator's"
NO_FILE: Final = "{component}: the {asset} is not in the store, or it is a link"
NOT_REGULAR: Final = "{component}: the {asset} in the store is not a regular file"
NOT_OPERATORS_FILE: Final = "{component}: the {asset} in the store is not the operator's file"
OPEN_TO_OTHERS: Final = "{component}: the {asset} in the store is open to group or other"
SECOND_NAME: Final = "{component}: the {asset} in the store has a second name"
OUTSIDE_LIMIT: Final = "{component}: the {asset} in the store is empty or larger than its limit"
GREW: Final = "{component}: the {asset} in the store grew past its limit while root read it"
NOT_WRITTEN: Final = "{component}: root could not write its copy of the {asset}"
NOT_READ: Final = "{component}: root could not read the {asset} in the store"
COPY_GONE: Final = "{component}: the copy of the {asset} is gone or is not a regular file"
COPY_CHANGED: Final = "{component}: the copy of the {asset} is not the file that root wrote"
NO_READ: Final = "{component}: a call of the system failed while root read the store or its copy"


class Asset(StrEnum):
    """The two assets of a binary component, as a note names them."""

    ARCHIVE = "archive"
    BUNDLE = "bundle"


@dataclass(frozen=True)
class _Part:
    """One asset: its role, its file name and its limit in bytes."""

    asset: Asset
    name: str
    max_bytes: int


#: The two parts of a component: the archive, then the bundle.
type _Pair = tuple[_Part, _Part]


@dataclass(frozen=True)
class Copied:
    """Root's copy of the two assets of one component, and the digest of
    the archive bytes that root wrote: `sha256:<64 hex>`."""

    archive: Path
    bundle: Path
    digest: str


@dataclass(frozen=True)
class NoCopy:
    """No copy, and the one fixed line that says why."""

    note: str


class _Fault(Exception):
    """One fault of a copy. `note` is a fixed line of this module."""

    def __init__(self, note: str) -> None:
        super().__init__(note)
        self.note = note


def copy_from_store(
    component: str,
    tag: str,
    work: Path,
    operator_uid: int,
    earlier: Copied | NoCopy | None = None,
) -> Copied | NoCopy:
    """Root's copy of the archive and the bundle of `component` at `tag`, in
    `work`, the work directory of the request. `operator_uid` is the uid of
    the operator, who owns the store.

    `earlier` is the result of the first call for `component` in this
    release, and None for the first call itself. The first call copies from
    the store. A later call never opens the store: it gives the `NoCopy` of
    the first call again, or it reads root's copy and hashes it again.
    Never raises.
    """
    matched = TAG_RE.fullmatch(tag)
    if component not in CATALOG_BY_NAME or matched is None or matched.group(1) != component:
        return NoCopy(NOT_ITS_TAG.format(component=component))

    if isinstance(earlier, NoCopy):
        return earlier

    parts = _parts(component)
    if parts is None:
        return NoCopy(NO_ASSETS.format(component=component))

    target = work / COPY_DIR / component
    if earlier is None:
        try:
            _make_copy_dir(work, target)
        except OSError:
            return NoCopy(NO_COPY_DIR.format(component=component))

    try:
        if earlier is None:
            return _copy(component, tag, parts, target, operator_uid)

        return _hash_copy(component, parts, target)
    except _Fault as fault:
        return NoCopy(fault.note)
    except OSError:
        return NoCopy(NO_READ.format(component=component))


def _parts(component: str) -> _Pair | None:
    """The archive and the bundle of `component`, each with its limit, as
    `artifacts.asset_limits` gives them: the archive first. None when it
    does not give two assets."""
    match asset_limits(component, Kind.BINARY):
        case (archive, bundle):
            return (
                _Part(Asset.ARCHIVE, archive.name, archive.max_bytes),
                _Part(Asset.BUNDLE, bundle.name, bundle.max_bytes),
            )
        case _:
            return None


def _make_copy_dir(work: Path, target: Path) -> None:
    """Make each directory from `work` down to `target`, each with the mode
    0755. Raises `OSError` for a link or a file in the place of a
    directory, and for a `target` that is there already."""
    work.mkdir(exist_ok=True)
    _own_dir(work)
    (work / COPY_DIR).mkdir(exist_ok=True)
    _own_dir(work / COPY_DIR)
    target.mkdir()
    _own_dir(target)


def _own_dir(path: Path) -> None:
    """A directory of root's work tree, not a link, with the mode 0755."""
    if not stat.S_ISDIR(os.lstat(path).st_mode):
        raise NotADirectoryError(path)

    os.chmod(path, DIR_MODE)


def _copy(component: str, tag: str, parts: _Pair, target: Path, operator_uid: int) -> Copied:
    """The first call: copy each part from the store, then hash the archive
    bytes that root wrote."""
    words = {"component": component}
    try:
        home = operator_home()
        store = artifact_store()
    except Refusal:
        raise _Fault(NO_HOME.format_map(words)) from None

    repo = str(CATALOG_BY_NAME[component].repo)
    segments = (*store.relative_to(home).parts, repo, tag)
    digest = hashlib.sha256()
    with _tag_dir(Path(home), segments, operator_uid, words) as tag_fd:
        try:
            for part in parts:
                sink = digest.update if part.asset is Asset.ARCHIVE else None
                _copy_one(tag_fd, part, target / part.name, operator_uid, component, sink)
        except (_Fault, OSError):
            _discard(target, parts)
            raise

    return _copied(target, parts, digest.hexdigest())


@contextmanager
def _tag_dir(
    home: Path, segments: tuple[str, ...], operator_uid: int, words: dict[str, str]
) -> Generator[int]:
    """The open directory of the tag. Each directory below `home` is opened
    with no link followed, and the operator must own it."""
    try:
        fd = os.open(home, _HOME_FLAGS)
    except OSError:
        raise _Fault(NO_STORE_DIR.format_map(words)) from None

    try:
        for segment in segments:
            parent = fd
            fd = -1
            try:
                fd = os.open(segment, _DIR_FLAGS, dir_fd=parent)
            except OSError:
                raise _Fault(NO_STORE_DIR.format_map(words)) from None
            finally:
                os.close(parent)

            if os.fstat(fd).st_uid != operator_uid:
                raise _Fault(NOT_OPERATORS_DIR.format_map(words))

        yield fd
    finally:
        if fd >= 0:
            os.close(fd)


def _copy_one(
    tag_fd: int,
    part: _Part,
    destination: Path,
    operator_uid: int,
    component: str,
    sink: Callable[[bytes], None] | None,
) -> None:
    """Copy one file of the store to `destination`, with the mode 0644. Each
    check runs on the open descriptor, before the first read."""
    words = {"component": component, "asset": str(part.asset)}
    try:
        source = os.open(part.name, CHECKED_READ_FLAGS, dir_fd=tag_fd)
    except OSError:
        raise _Fault(NO_FILE.format_map(words)) from None

    try:
        _check_store_file(os.fstat(source), part, operator_uid, words)
        _write_copy(source, part, destination, sink, words)
    finally:
        os.close(source)


def _check_store_file(
    facts: os.stat_result, part: _Part, operator_uid: int, words: dict[str, str]
) -> None:
    """Rule 1, on the descriptor of one file of the store.

    CONTRACT-QUESTION: contract 06 §3.4 names three checks of a file of the
    store: a regular file, the owner is the operator, and the size is at
    most the cap. The reading taken also refuses a file with a bit for
    group or other (`handover/AGENTS.md`, rule 5a), a file with a second
    name, and an empty file, which the operator side never keeps. It also
    refuses a directory of the store path that is a link or that the
    operator does not own. A change costs one condition for each check.
    """
    if not stat.S_ISREG(facts.st_mode):
        raise _Fault(NOT_REGULAR.format_map(words))

    if facts.st_uid != operator_uid:
        raise _Fault(NOT_OPERATORS_FILE.format_map(words))

    if stat.S_IMODE(facts.st_mode) & GROUP_OTHER_BITS:
        raise _Fault(OPEN_TO_OTHERS.format_map(words))

    if facts.st_nlink != 1:
        raise _Fault(SECOND_NAME.format_map(words))

    if not 0 < facts.st_size <= part.max_bytes:
        raise _Fault(OUTSIDE_LIMIT.format_map(words))


def _write_copy(
    source: int,
    part: _Part,
    destination: Path,
    sink: Callable[[bytes], None] | None,
    words: dict[str, str],
) -> None:
    """Write the bytes of `source` to a new file `destination`, and give
    each written block to `sink`."""
    try:
        target = os.open(destination, NEW_FILE_FLAGS, FILE_MODE)
    except OSError:
        raise _Fault(NOT_WRITTEN.format_map(words)) from None

    try:
        os.fchmod(target, FILE_MODE)
        for block in _blocks(source, part.max_bytes, words, (NOT_READ, GREW)):
            _write_all(target, block, words)
            if sink is not None:
                sink(block)
    except OSError:
        raise _Fault(NOT_WRITTEN.format_map(words)) from None
    finally:
        os.close(target)


def _write_all(target: int, block: bytes, words: dict[str, str]) -> None:
    """One block, in as many writes as it takes."""
    view = memoryview(block)
    while view:
        written = os.write(target, view)
        if written <= 0:
            raise _Fault(NOT_WRITTEN.format_map(words))

        view = view[written:]


def _blocks(
    source: int, max_bytes: int, words: dict[str, str], notes: tuple[str, str]
) -> Iterator[bytes]:
    """The bytes of `source` in blocks, and one byte past `max_bytes` at
    most. `notes` holds the note of a read that fails, then the note of a
    file past its limit. The limit holds whatever the size of the file
    said."""
    unread, too_long = notes
    total = 0
    while True:
        want = min(BLOCK_BYTES, max_bytes + 1 - total)
        try:
            block = os.read(source, want)
        except OSError:
            raise _Fault(unread.format_map(words)) from None

        if not block:
            return

        total += len(block)
        if total > max_bytes:
            raise _Fault(too_long.format_map(words))

        yield block


def _hash_copy(component: str, parts: _Pair, target: Path) -> Copied:
    """A later call: read root's own copy and hash the archive again. The
    store is not opened."""
    digest = hashlib.sha256()
    for part in parts:
        words = {"component": component, "asset": str(part.asset)}
        try:
            source = os.open(target / part.name, CHECKED_READ_FLAGS)
        except OSError:
            raise _Fault(COPY_GONE.format_map(words)) from None

        try:
            _check_copy(os.fstat(source), part, words)
            for block in _blocks(source, part.max_bytes, words, (COPY_GONE, COPY_CHANGED)):
                if part.asset is Asset.ARCHIVE:
                    digest.update(block)
        finally:
            os.close(source)

    return _copied(target, parts, digest.hexdigest())


def _check_copy(facts: os.stat_result, part: _Part, words: dict[str, str]) -> None:
    """A file of the copy is a regular file of root, with one name, the
    mode 0644 and its limit at most."""
    if not stat.S_ISREG(facts.st_mode):
        raise _Fault(COPY_GONE.format_map(words))

    owner = facts.st_uid == os.geteuid()
    modes = stat.S_IMODE(facts.st_mode) == FILE_MODE
    if not owner or not modes or facts.st_nlink != 1 or facts.st_size > part.max_bytes:
        raise _Fault(COPY_CHANGED.format_map(words))


def _copied(target: Path, parts: _Pair, hex_digest: str) -> Copied:
    archive, bundle = parts

    return Copied(
        archive=target / archive.name,
        bundle=target / bundle.name,
        digest=f"{DIGEST_PREFIX}{hex_digest}",
    )


def _discard(target: Path, parts: _Pair) -> None:
    """Remove each file that a failed copy wrote. The directory stays, as
    the record of the first call."""
    for part in parts:
        try:
            os.unlink(target / part.name)
        except OSError:
            continue


__all__ = ["COPY_DIR", "Asset", "Copied", "NoCopy", "copy_from_store"]
