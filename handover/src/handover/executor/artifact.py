"""Root's copy of the signed assets of a component, at step 2.

A `kind: binary` component has two assets: its archive and the bundle of
its signature. A `kind: disk-image` component has each file of contract 06
§8.4, each before its own bundle (CH35 (4), CH76 rule 5). The operator side
fetches each asset into the store below the home of the operator
(`corpus/artifacts.py`):

    <operator home>/.local/state/creche/artifacts/<catalog repo>/<tag>/<asset>

The operator owns each directory and each file there. So root reads the
store one time, at step 2, and copies each file into the work tree of the
request. Each later read is a read of that copy:

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
5. **The digest covers each signed byte that root wrote**, as
   `sha256:<64 hex>`. For a binary component it is the SHA-256 of the
   archive. For a disk-image component it is `artifacts.sum_lines_digest`
   of its files (contract 06 §9, CH76 rule 5). No bundle changes it. It
   goes into `artifact_digest` of the resolved manifest, so the gate id
   and the tap bind those bytes.
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
from ..artifacts import AssetLimit, asset_limits, sum_lines_digest
from ..catalog import BUNDLE_SUFFIX, CATALOG_BY_NAME, Kind
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
NO_ASSETS: Final = "{component}: the release tool names no signed file and bundle for it"
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
    """The two assets of a binary component, as a note names them. A note
    names an asset of a disk-image component by its file name."""

    ARCHIVE = "archive"
    BUNDLE = "bundle"


@dataclass(frozen=True)
class _Part:
    """One asset: how a note names it, its file name and its limit in
    bytes."""

    label: str
    name: str
    max_bytes: int


#: One signed file, then the bundle of its signature.
type _Pair = tuple[_Part, _Part]


@dataclass(frozen=True)
class Copied:
    """Root's copy of the two assets of one binary component, and the
    digest of the archive bytes that root wrote: `sha256:<64 hex>`."""

    archive: Path
    bundle: Path
    digest: str


@dataclass(frozen=True)
class SignedCopy:
    """Root's copy of one file of a disk-image component, and of the
    bundle of its signature. `name` is the file name of the Release."""

    name: str
    file: Path
    bundle: Path


@dataclass(frozen=True)
class CopiedFiles:
    """Root's copy of each file of one disk-image component, in the order
    of `artifacts.asset_limits`, and the digest of the bytes that root
    wrote: `sha256:` and `artifacts.sum_lines_digest` (CH76 rule 5)."""

    files: tuple[SignedCopy, ...]
    digest: str


#: What a copy gives: a binary copy, a disk-image copy, or no copy.
type CopyResult = Copied | CopiedFiles | NoCopy


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
    kind: Kind,
    tag: str,
    work: Path,
    operator_uid: int,
    earlier: CopyResult | None = None,
) -> CopyResult:
    """Root's copy of each asset of `component` at `tag`, in `work`, the
    work directory of the request. `kind` is the kind of the manifest of
    the new tag, root's one source for it. `operator_uid` is the uid of the
    operator, who owns the store.

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

    parts = _parts(component, kind)
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
            return _result(kind, target, parts, _copy(component, tag, parts, target, operator_uid))

        return _result(kind, target, parts, _hash_copy(component, parts, target))
    except _Fault as fault:
        return NoCopy(fault.note)
    except OSError:
        return NoCopy(NO_READ.format(component=component))


def _parts(component: str, kind: Kind) -> tuple[_Pair, ...] | None:
    """Each signed file of `component` and its bundle, each with its limit,
    as `artifacts.asset_limits` gives them. A binary component has one
    pair, the archive first. A disk-image component has one pair for each
    file, and each bundle is `<file>.sigstore.json`, so no file is checked
    with the bundle of another file. None for each other answer."""
    limits = asset_limits(component, kind)
    if kind is Kind.BINARY:
        match limits:
            case (archive, bundle):
                return (
                    (
                        _Part(str(Asset.ARCHIVE), archive.name, archive.max_bytes),
                        _Part(str(Asset.BUNDLE), bundle.name, bundle.max_bytes),
                    ),
                )
            case _:
                return None

    if kind is Kind.DISK_IMAGE:
        return _disk_pairs(limits)

    return None


def _disk_pairs(limits: tuple[AssetLimit, ...]) -> tuple[_Pair, ...] | None:
    """The pairs of a disk-image component: each file, then its own
    bundle. None when the list has no file, or a bundle that is not the
    bundle of the file before it."""
    files, bundles = limits[::2], limits[1::2]
    if not files or len(files) != len(bundles):
        return None

    pairs = tuple(zip(files, bundles, strict=True))
    if any(bundle.name != f"{file.name}{BUNDLE_SUFFIX}" for file, bundle in pairs):
        return None

    return tuple((_named(file), _named(bundle)) for file, bundle in pairs)


def _named(limit: AssetLimit) -> _Part:
    """A part that a note names by its file name."""
    return _Part(limit.name, limit.name, limit.max_bytes)


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


def _copy(
    component: str, tag: str, parts: tuple[_Pair, ...], target: Path, operator_uid: int
) -> dict[str, str]:
    """The first call: copy each part from the store. The answer maps each
    signed file to the SHA-256 of the bytes that root wrote, as 64 hex."""
    words = {"component": component}
    try:
        home = operator_home()
        store = artifact_store()
    except Refusal:
        raise _Fault(NO_HOME.format_map(words)) from None

    repo = str(CATALOG_BY_NAME[component].repo)
    segments = (*store.relative_to(home).parts, repo, tag)
    digests: dict[str, str] = {}
    with _tag_dir(Path(home), segments, operator_uid, words) as tag_fd:
        try:
            for signed, bundle in parts:
                digest = hashlib.sha256()
                sink = digest.update
                _copy_one(tag_fd, signed, target / signed.name, operator_uid, component, sink)
                _copy_one(tag_fd, bundle, target / bundle.name, operator_uid, component, None)
                digests[signed.name] = digest.hexdigest()
        except (_Fault, OSError):
            _discard(target, parts)
            raise

    return digests


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
    words = {"component": component, "asset": part.label}
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


def _hash_copy(component: str, parts: tuple[_Pair, ...], target: Path) -> dict[str, str]:
    """A later call: read root's own copy and hash each signed file again.
    The store is not opened."""
    digests: dict[str, str] = {}
    for signed, bundle in parts:
        digests[signed.name] = _hash_one(component, signed, target)
        _hash_one(component, bundle, target)

    return digests


def _hash_one(component: str, part: _Part, target: Path) -> str:
    """The SHA-256 of one file of root's copy, as 64 hex, after the checks
    of `_check_copy`."""
    words = {"component": component, "asset": part.label}
    digest = hashlib.sha256()
    try:
        source = os.open(target / part.name, CHECKED_READ_FLAGS)
    except OSError:
        raise _Fault(COPY_GONE.format_map(words)) from None

    try:
        _check_copy(os.fstat(source), part, words)
        for block in _blocks(source, part.max_bytes, words, (COPY_GONE, COPY_CHANGED)):
            digest.update(block)
    finally:
        os.close(source)

    return digest.hexdigest()


def _check_copy(facts: os.stat_result, part: _Part, words: dict[str, str]) -> None:
    """A file of the copy is a regular file of root, with one name, the
    mode 0644 and its limit at most."""
    if not stat.S_ISREG(facts.st_mode):
        raise _Fault(COPY_GONE.format_map(words))

    owner = facts.st_uid == os.geteuid()
    modes = stat.S_IMODE(facts.st_mode) == FILE_MODE
    if not owner or not modes or facts.st_nlink != 1 or facts.st_size > part.max_bytes:
        raise _Fault(COPY_CHANGED.format_map(words))


def _result(
    kind: Kind, target: Path, parts: tuple[_Pair, ...], digests: dict[str, str]
) -> Copied | CopiedFiles:
    """The copy of `kind`: a binary copy of its one pair with the digest of
    the archive, or a disk-image copy with the digest of the sum lines of
    its files (rule 5). `_parts` gave a binary component one pair."""
    if kind is Kind.BINARY:
        archive, bundle = parts[0]

        return Copied(
            archive=target / archive.name,
            bundle=target / bundle.name,
            digest=f"{DIGEST_PREFIX}{digests[archive.name]}",
        )

    return CopiedFiles(
        files=tuple(
            SignedCopy(name=signed.name, file=target / signed.name, bundle=target / bundle.name)
            for signed, bundle in parts
        ),
        digest=f"{DIGEST_PREFIX}{sum_lines_digest(digests)}",
    )


def _discard(target: Path, parts: tuple[_Pair, ...]) -> None:
    """Remove each file that a failed copy wrote. The directory stays, so
    a second call with no `earlier` copies nothing (`NO_COPY_DIR`)."""
    for pair in parts:
        for part in pair:
            try:
                os.unlink(target / part.name)
            except OSError:
                continue


__all__ = [
    "COPY_DIR",
    "Asset",
    "Copied",
    "CopiedFiles",
    "CopyResult",
    "NoCopy",
    "SignedCopy",
    "copy_from_store",
]
