"""Fetch the two assets of the Release of a binary component into the store.

Root installs a binary component from an archive that CI built and signed
(`artifacts.py` of the pure half). Root fetches nothing. The operator side
fetches the archive and the bundle of its signature into the store, and root
copies the two files later and checks the signature. A SHA-256 names bytes,
so the way that the bytes took cannot change them.

    <operator home>/.local/state/creche/artifacts/<catalog repo>/<tag>/<asset>

`executor/source.artifact_store` gives the first part, so root and the
operator compute one path. Each directory of the store has the mode 0700 and
each file the mode 0600, whatever the umask. Root trusts no name and no mode
of the store, and it checks both.

Six rules hold its shape:

1. **It refuses to run as root.** The store is the operator's directory, and
   root writes no file there. A run as root is one line and no child.
2. **It never raises.** A fetch that fails costs a wait of the follower, and
   the next run fetches again. Each fault is one fixed line. No text of curl
   reaches a line.
3. **It acts only for a component that ships an archive** (`ships_archive`).
4. **It writes a temporary name in the directory of the asset, and renames
   it after status 0.** It removes the temporary file of an older run
   first, and it resumes no partial file.
5. **Each child has a time limit and a size limit.** The time limit is the
   time that is left before the deadline, and `CHILD_MAX_S` at most. The
   size limit of an asset is a flag of curl and a limit of the process too,
   because curl reads its flag against a length that a server need not
   send.
6. **One fetch at a time writes in the directory of a tag.** The fetch
   holds an exclusive `flock` on that directory while it writes there. A
   second fetch of the same tag, for example a run of the follower beside a
   `handover request`, gets one line and starts no child.

A Release gets its assets some seconds after its tag. So an answer of the
server with an error is the normal first answer for a new tag.
"""

from __future__ import annotations

import contextlib
import fcntl
import math
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from time import monotonic
from typing import Final

from ..allocate import TAG_FORMAT, TAG_RE
from ..artifacts import AssetLimit, archive_expected, asset_limits, download_url
from ..catalog import CATALOG_BY_NAME, Kind, Repo
from ..errors import Refusal
from ..executor.source import CURL, CURL_PROTOCOL_FLAGS, artifact_store
from ..site import github_owner, github_repo
from . import CappedRunFn, run_capped

#: The largest time limit of one child, in seconds.
CHILD_MAX_S: Final = 600

#: The mode of each directory and of each file of the store.
DIR_MODE: Final = 0o700
FILE_MODE: Final = 0o600

#: The temporary name of an asset is `.<asset>.part`, beside the asset.
TEMP_PREFIX: Final = "."
TEMP_SUFFIX: Final = ".part"

#: The first word after the program: curl then reads no config file of the
#: account. The words of the call are the words of this module only.
NO_CONFIG_FILE: Final = "--disable"

#: The flags of each download before the two limits.
FETCH_FLAGS: Final = (NO_CONFIG_FILE, "-fsSL", *CURL_PROTOCOL_FLAGS)

#: The exit status of `curl -f` when the server answers with an error, as it
#: does before the Release holds the asset.
SERVER_ERROR_EXIT: Final = 22

#: The open of a fetched file. No link is followed.
_READ_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC

#: The open of the directory of one tag, for its lock (rule 6).
_LOCK_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC

ROOT_REFUSAL: Final = "refusing to fetch an artifact as root: the store is the operator's directory"
PRUNE_ROOT_REFUSAL: Final = "refusing to prune the store as root: it is the operator's directory"
NO_STORE: Final = "the site file gives no home of the operator, so the store has no path"
NO_TIME: Final = "no time is left in this run to fetch an artifact"
NOT_ITS_TAG: Final = "{component}: the tag is not a tag of this component, so nothing is fetched"
NO_URL: Final = "{component}: the site file gives no download URL, so nothing is fetched"
NO_DIRECTORY: Final = "{tag}: the store directory could not be made"
LOCKED: Final = "{tag}: another fetch holds the store directory"
NOT_LOCKED: Final = "{tag}: the store directory could not be locked"
FETCHED: Final = "fetched {asset} of {tag}"
NO_START: Final = "{asset} of {tag}: the fetch could not start"
TOO_LATE: Final = "{asset} of {tag}: the fetch passed its time limit"
SERVER_ERROR: Final = "{asset} of {tag}: the server answered with an error"
EXITED: Final = "{asset} of {tag}: the fetch exited {code}"
NOT_KEPT: Final = "{asset} of {tag}: the fetched file is not a regular file inside its limit"
NOT_MOVED: Final = "{asset} of {tag}: the fetched file could not get its name"
PRUNED: Final = "removed the artifacts of {tag} from the store"
NOT_PRUNED: Final = "{tag}: the store directory could not be removed"


def ships_archive(component: str) -> bool:
    """Whether the operator side expects an archive for `component`, a name
    of the catalog.

    This is the one read of the kind of a catalog row on the operator side.
    Root reads the kind of the manifest of the new tag instead.
    """
    return archive_expected(CATALOG_BY_NAME[component].kind)


@dataclass(frozen=True)
class ArtifactReport:
    """What one fetch did. `ready` holds each component whose two files are
    in the store, and `lines` is what the caller prints."""

    ready: frozenset[str]
    lines: tuple[str, ...]


@dataclass(frozen=True)
class _Want:
    """One component that ships an archive, and where its two files go."""

    component: str
    repo: Repo
    tag: str
    directory: Path
    assets: tuple[AssetLimit, ...]


class _Step(Enum):
    """The end of the fetch of one asset."""

    DONE = "done"
    FAILED = "failed"
    NO_TIME = "no time"


def fetch_artifacts(
    tags: Mapping[str, str],
    store: Path,
    deadline: float,
    run: CappedRunFn | None = None,
    euid: int | None = None,
) -> ArtifactReport:
    """Fetch each asset that `store` does not hold, for each component of
    `tags` that ships an archive. `tags` maps a component to its tag, and
    `deadline` is a time of `time.monotonic`. Never raises."""
    if (os.geteuid() if euid is None else euid) == 0:
        return ArtifactReport(frozenset(), (ROOT_REFUSAL,))

    runner = run if run is not None else run_capped
    lines: list[str] = []
    found = (_want_of(name, tags[name], store, lines) for name in sorted(tags))
    wants = [want for want in found if want is not None]
    ready = {want.component for want in wants if _complete(store, want)}
    for want in wants:
        if want.component in ready:
            continue

        step = _fetch_one(store, want, deadline, runner, lines)
        if step is _Step.DONE:
            ready.add(want.component)

        if step is _Step.NO_TIME:
            lines.append(NO_TIME)
            break

    return ArtifactReport(frozenset(ready), tuple(lines))


def _want_of(component: str, tag: str, store: Path, lines: list[str]) -> _Want | None:
    """The want of one component, or None: for a component that ships no
    archive with no line, and for a tag of another shape with one line."""
    if component not in CATALOG_BY_NAME or not ships_archive(component):
        return None

    matched = TAG_RE.fullmatch(tag)
    if matched is None or matched.group(1) != component:
        lines.append(NOT_ITS_TAG.format(component=component))

        return None

    repo = CATALOG_BY_NAME[component].repo

    return _Want(
        component=component,
        repo=repo,
        tag=tag,
        directory=store / str(repo) / tag,
        assets=asset_limits(component, Kind.BINARY),
    )


def _complete(store: Path, want: _Want) -> bool:
    """Whether the store holds both files of `want`, each with its mode, in
    directories with their mode."""
    chain = (store, store / str(want.repo), want.directory)
    if not all(_is_private_dir(path) for path in chain):
        return False

    return all(_held(want.directory / asset.name, asset.max_bytes) for asset in want.assets)


def _fetch_one(
    store: Path, want: _Want, deadline: float, runner: CappedRunFn, lines: list[str]
) -> _Step:
    """Make the directories of `want`, lock the directory of the tag, then
    fetch each asset that is not held. The first fault stops this
    component."""
    if not _make_directories(store, want):
        lines.append(NO_DIRECTORY.format(tag=want.tag))

        return _Step.FAILED

    try:
        fd = os.open(want.directory, _LOCK_FLAGS)
    except OSError:
        lines.append(NOT_LOCKED.format(tag=want.tag))

        return _Step.FAILED

    try:
        fault = _lock_fault(fd)
        if fault is not None:
            lines.append(fault.format(tag=want.tag))

            return _Step.FAILED

        return _fetch_held(want, deadline, runner, lines)
    finally:
        os.close(fd)


def _lock_fault(fd: int) -> str | None:
    """None when this fetch holds the lock of the open directory, or the
    fault line. The lock does not wait, and `close` releases it."""
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return LOCKED
    except OSError:
        return NOT_LOCKED

    return None


def _fetch_held(want: _Want, deadline: float, runner: CappedRunFn, lines: list[str]) -> _Step:
    """Fetch each asset that is not held, under the lock of the directory."""
    for asset in want.assets:
        if _held(want.directory / asset.name, asset.max_bytes):
            continue

        url = _url_of(want, asset)
        if url is None:
            lines.append(NO_URL.format(component=want.component))

            return _Step.FAILED

        step = _fetch_asset(want, asset, url, deadline, runner, lines)
        if step is not _Step.DONE:
            return step

    return _Step.DONE


def _url_of(want: _Want, asset: AssetLimit) -> str | None:
    """The download URL of one asset, from the site file. The one function
    of `artifacts.py` makes it, so the code host has one place."""
    try:
        return download_url(github_owner(), github_repo(str(want.repo)), want.tag, asset.name)
    except Refusal:
        return None


def _fetch_asset(
    want: _Want,
    asset: AssetLimit,
    url: str,
    deadline: float,
    runner: CappedRunFn,
    lines: list[str],
) -> _Step:
    """One child for one asset: the temporary name first, the final name
    after status 0."""
    # A comparison and no `floor` first: a deadline that is not a finite
    # number is no time left, or `CHILD_MAX_S`, and never an error.
    left = deadline - monotonic()
    if not left >= 1:
        return _Step.NO_TIME

    seconds = CHILD_MAX_S if left >= CHILD_MAX_S else math.floor(left)

    final = want.directory / asset.name
    temp = want.directory / f"{TEMP_PREFIX}{asset.name}{TEMP_SUFFIX}"
    words = {"asset": asset.name, "tag": want.tag}
    _discard(temp)
    argv = [
        CURL,
        *FETCH_FLAGS,
        "--max-time",
        str(seconds),
        "--max-filesize",
        str(asset.max_bytes),
        "--output",
        str(temp),
        url,
    ]
    fault = _child_fault(runner, argv, seconds, asset.max_bytes, words)
    if fault is None and not _seal(temp, asset.max_bytes):
        fault = NOT_KEPT.format_map(words)

    if fault is None and not _rename(temp, final):
        fault = NOT_MOVED.format_map(words)

    if fault is not None:
        _discard(temp)
        lines.append(fault)

        return _Step.FAILED

    lines.append(FETCHED.format_map(words))

    return _Step.DONE


def _child_fault(
    runner: CappedRunFn, argv: list[str], seconds: int, max_bytes: int, words: Mapping[str, str]
) -> str | None:
    """None for status 0, or the fault line. The exit status is the one
    value of the child that a line holds."""
    try:
        code = runner(argv, float(seconds), max_bytes)
    except TimeoutError:
        return TOO_LATE.format_map(words)
    except OSError:
        return NO_START.format_map(words)

    if code == 0:
        return None

    if code == SERVER_ERROR_EXIT:
        return SERVER_ERROR.format_map(words)

    return EXITED.format(code=code, **words)


def _is_private_dir(path: Path) -> bool:
    """A directory, not a link, with the mode `DIR_MODE`."""
    try:
        info = os.lstat(path)
    except OSError:
        return False

    return stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == DIR_MODE


def _held(path: Path, max_bytes: int) -> bool:
    """A regular file, not a link, inside its limit, with the mode
    `FILE_MODE`. Each other entry is fetched again, and the rename replaces
    it."""
    try:
        info = os.lstat(path)
    except OSError:
        return False

    regular = stat.S_ISREG(info.st_mode) and info.st_size <= max_bytes

    return regular and stat.S_IMODE(info.st_mode) == FILE_MODE


def _make_directories(store: Path, want: _Want) -> bool:
    """Each absent directory above the store, then the store, the directory
    of the repository and the directory of the tag. Each directory that
    this makes, and each of the last three, gets the mode `DIR_MODE`."""
    try:
        for parent in reversed(store.parents):
            if not os.path.lexists(parent):
                _make_private(parent)

        for path in (store, store / str(want.repo), want.directory):
            _make_private(path)
    except OSError:
        return False

    return True


def _make_private(path: Path) -> None:
    """`path` as a directory with the mode `DIR_MODE`, made when it is
    absent. Raises `OSError` for a link or a file in its place."""
    with contextlib.suppress(FileExistsError):
        os.mkdir(path, DIR_MODE)

    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode):
        raise NotADirectoryError(path)

    if stat.S_IMODE(info.st_mode) != DIR_MODE:
        os.chmod(path, DIR_MODE)


def _seal(path: Path, max_bytes: int) -> bool:
    """Give a fetched file the mode `FILE_MODE`. False for a link, for a file
    that is not regular and for a file past its limit."""
    try:
        fd = os.open(path, _READ_FLAGS)
    except OSError:
        return False

    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
            return False

        os.fchmod(fd, FILE_MODE)
    except OSError:
        return False
    finally:
        os.close(fd)

    return True


def _rename(temp: Path, final: Path) -> bool:
    try:
        os.replace(temp, final)
    except OSError:
        return False

    return True


def _discard(path: Path) -> None:
    """Remove a file or a link. Nothing is followed, and a fault is quiet."""
    with contextlib.suppress(OSError):
        os.unlink(path)


# -- the prune --------------------------------------------------------------


def prune_artifacts(store: Path, live: Mapping[str, str]) -> tuple[str, ...]:
    """Remove the directory of each tag whose version is below the live
    version of its component. `live` maps a component to its live version.

    It reads only a name that matches the tag pattern and that names a
    component of the catalog in its own repository. It follows no link, and
    it never raises. The answer is one line for each directory. A run as
    root is one line and removes nothing.
    """
    if os.geteuid() == 0:
        return (PRUNE_ROOT_REFUSAL,)

    if not _is_directory(store):
        return ()

    lines: list[str] = []
    for repo in Repo:
        directory = store / str(repo)
        if not _is_directory(directory):
            continue

        for name in _names_in(directory):
            if not _is_old(name, repo, live) or not _is_directory(directory / name):
                continue

            removed = _remove_tag(directory / name)
            lines.append((PRUNED if removed else NOT_PRUNED).format(tag=name))

    return tuple(lines)


def _is_old(name: str, repo: Repo, live: Mapping[str, str]) -> bool:
    """Whether `name` is a tag of a component of `repo` below its live
    version."""
    matched = TAG_RE.fullmatch(name)
    if matched is None:
        return False

    component = matched.group(1)
    row = CATALOG_BY_NAME.get(component)
    version = live.get(component)
    if row is None or row.repo is not repo or version is None:
        return False

    current = TAG_RE.fullmatch(TAG_FORMAT.format(component=component, version=version))
    if current is None:
        return False

    return _numbers(matched) < _numbers(current)


def _numbers(tag: re.Match[str]) -> tuple[int, int, int]:
    """The three numbers of a tag, after its component."""
    return int(tag.group(2)), int(tag.group(3)), int(tag.group(4))


def _is_directory(path: Path) -> bool:
    """A directory, not a link."""
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _names_in(directory: Path) -> tuple[str, ...]:
    try:
        with os.scandir(directory) as entries:
            return tuple(sorted(entry.name for entry in entries))
    except OSError:
        return ()


def _remove_tag(directory: Path) -> bool:
    """Remove each file and each link of the directory of one tag, then the
    directory. A directory inside it stays, and so does the tag directory."""
    for name in _names_in(directory):
        path = directory / name
        if not _is_directory(path):
            _discard(path)

    try:
        os.rmdir(directory)
    except OSError:
        return False

    return True


__all__ = [
    "CHILD_MAX_S",
    "DIR_MODE",
    "FETCH_FLAGS",
    "FILE_MODE",
    "NO_STORE",
    "PRUNE_ROOT_REFUSAL",
    "ROOT_REFUSAL",
    "ArtifactReport",
    "artifact_store",
    "fetch_artifacts",
    "prune_artifacts",
    "ships_archive",
]
