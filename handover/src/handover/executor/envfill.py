"""The env fill: root adds the absent keys of a unit's env file.

A unit such as `creche-vmm.service` reads one env file alone (decisions
CH59 rule 1, CH77 rule 1), and `bin/fleet-up up` writes that file from the
catalog of `creche-env` and the site file. fleet-up and the catalog reach a
host through `creche-deploy`, and never through a release. So a release
that adds a key to the program would start a program that stops on its
first line. The manifest names the file and the keys in its `env` table,
and the release adds each key that the file does not hold:

```
 step 4  read the file (rule 5a), find each absent key, resolve each one
         from the catalog at the release commit and the site file, log the
         plan. A fault refuses the release before the push.
 step 8  read the file again through one descriptor, and append a line
         for each key that is still absent. Change no line.
```

Six rules hold the fill:

1. **Root reads the file as rule 5a says** (`handover/AGENTS.md`). The open
   has `O_NOFOLLOW`, and the checks run on the open descriptor: a regular
   file, the uid of the operator, no bit for group or other, and the size
   limit of `creche-env`.
2. **A key is absent** when the file has no line for it. A line with an
   empty value is a refusal, because the fill changes no line, and a second
   line of one key makes the reader of the vmm refuse the file.
3. **The value comes from the catalog at the release commit**, and never
   from the installed tree. Root reads `default.env` and `example.env` out
   of its own clone of the commit that step 3 verified, with no link
   followed. So a key that lands in the same set as the program resolves.
   `creche-env` resolves the value against the site file, as fleet-up does,
   so the two writers give one value.
4. **Root appends and changes nothing else.** It appends through the
   descriptor that it checked and read, and it checks that descriptor
   again before the write. The new text must still parse.
5. **A restore keeps the added keys.** The previous program reads only the
   keys that it names, so a key that it does not name costs nothing.
6. **No value is a secret.** The catalog holds no secret (`env/AGENTS.md`,
   rule 5 of the files), and the site file holds no secret. A log line
   names each key, and never a value.
"""

from __future__ import annotations

import errno
import os
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from creche_env.envfile import (
    ASSIGN,
    DEFAULT_FILE,
    EXAMPLE_FILE,
    LINE_END,
    MAX_BYTES,
    EnvError,
    Fault,
    FileKind,
    Pair,
    ReadError,
    Text,
    load,
    pair_in,
    parse,
    read_trusted,
)

from ..errors import Refusal, RefusalCode
from ..manifest import EnvDecl
from ..site import site_file
from .quiet import CHECKED_READ_FLAGS, GROUP_OTHER_BITS

#: Where the catalog of `creche-env` sits in a clone of the `creche`
#: repository: the package data of `env/`.
CATALOG_PARTS: Final = ("env", "src", "creche_env")

#: The command that writes each env file of the fleet. It runs as the
#: operator, and a missing file is its job.
FLEET_UP: Final = "/opt/creche/bin/fleet-up up"

#: The open of step 8: the flags of rule 5a, with read and write, and each
#: write at the end of the file.
APPEND_FLAGS: Final = (CHECKED_READ_FLAGS & ~os.O_ACCMODE) | os.O_RDWR | os.O_APPEND

#: §2.6's closed list for the env fill. Each detail names the component,
#: the file and, where one is at fault, the key.
NO_FILE: Final = "{component}: {file} does not exist. As the operator, run {fix}"
LINK: Final = "{component}: {file} is a link"
NOT_OPENED: Final = "{component}: root cannot open {file}"
NOT_REGULAR: Final = "{component}: {file} is not a regular file"
NOT_OPERATORS: Final = "{component}: {file} is not the operator's file"
OPEN_TO_OTHERS: Final = "{component}: {file} is open to group or other"
TOO_LARGE: Final = "{component}: {file} is larger than {limit} bytes"
NOT_READ: Final = "{component}: root cannot read {file}"
NOT_PARSED: Final = "{component}: {file} does not parse: {problem}"
EMPTY_LINE: Final = "{component}: {file} sets {key} to no value, and root changes no line"
UNRESOLVED: Final = (
    "{component}: {file} lacks {key}, and the catalog and the site file give it no value"
)
NO_CATALOG: Final = "{component}: the catalog at the release commit is absent or is a link"
BAD_CATALOG: Final = "{component}: the catalog at the release commit does not read: {problem}"
BAD_SITE: Final = "{component}: the site file does not read with that catalog: {problem}"
CHANGED: Final = "{component}: {file} changed while root read it"
NOT_WRITTEN: Final = "{component}: root could not append to {file}"


class Verb(StrEnum):
    """What a log line of the fill says: the plan of step 4, or the write
    of step 8."""

    ADDS = "adds"
    WROTE = "wrote"


@dataclass(frozen=True)
class Fill:
    """The keys that the fill adds to one env file, each with its value, in
    the order of `env.keys`. Empty when the file holds each key."""

    component: str
    file: str
    adds: tuple[tuple[str, str], ...]

    def line(self, verb: Verb) -> str:
        """The log line, for example `env vmm: adds VMM_GIT_SPOOL_ROOT to
        /srv/agents/state/creche/vmm.env`. It names no value."""
        if not self.adds:
            return f"env {self.component}: nothing to add to {self.file}"

        keys = ", ".join(key for key, _ in self.adds)

        return f"env {self.component}: {verb} {keys} to {self.file}"


def plan_fill(component: str, decl: EnvDecl, clone: Path, operator_uid: int) -> Fill:
    """Step 4: what step 8 will add, or a refusal before the push."""
    fd = _open_checked(component, decl.file, CHECKED_READ_FLAGS, operator_uid)
    try:
        data = _read_all(component, decl.file, fd)
    finally:
        os.close(fd)

    adds = _adds(component, decl, clone, data)
    _check_after(component, decl.file, data, adds)

    return Fill(component, decl.file, adds)


def append_fill(component: str, decl: EnvDecl, clone: Path, operator_uid: int) -> Fill:
    """Step 8: append each key that is still absent, through the one
    descriptor that root checked and read. Answers what root wrote."""
    fd = _open_checked(component, decl.file, APPEND_FLAGS, operator_uid)
    try:
        before = os.fstat(fd)
        data = _read_all(component, decl.file, fd)
        adds = _adds(component, decl, clone, data)
        if not adds:
            return Fill(component, decl.file, ())

        payload = _check_after(component, decl.file, data, adds)
        _check_unchanged(component, decl.file, fd, before, len(data), operator_uid)
        _write_all(component, decl.file, fd, payload)
    finally:
        os.close(fd)

    return Fill(component, decl.file, adds)


def _words(component: str, file: str, **more: str) -> dict[str, str]:
    return {"component": component, "file": file, "fix": FLEET_UP} | more


def _refuse(template: str, component: str, file: str, **more: str) -> Refusal:
    return Refusal(RefusalCode.ENV, component, template.format_map(_words(component, file, **more)))


def _open_checked(component: str, file: str, flags: int, operator_uid: int) -> int:
    """The open descriptor of the env file, after the checks of rule 5a."""
    try:
        fd = os.open(file, flags)
    except FileNotFoundError:
        raise _refuse(NO_FILE, component, file) from None
    except OSError as error:
        template = LINK if error.errno == errno.ELOOP else NOT_OPENED
        raise _refuse(template, component, file) from None

    try:
        _check_facts(component, file, os.fstat(fd), operator_uid)
    except BaseException:
        os.close(fd)
        raise

    return fd


def _check_facts(component: str, file: str, facts: os.stat_result, operator_uid: int) -> None:
    """Rule 5a on one descriptor: a regular file of the operator, closed to
    group and other, at most the size limit of `creche-env`."""
    if not stat.S_ISREG(facts.st_mode):
        raise _refuse(NOT_REGULAR, component, file)

    if facts.st_uid != operator_uid:
        raise _refuse(NOT_OPERATORS, component, file)

    if stat.S_IMODE(facts.st_mode) & GROUP_OTHER_BITS:
        raise _refuse(OPEN_TO_OTHERS, component, file)

    if facts.st_size > MAX_BYTES:
        raise _refuse(TOO_LARGE, component, file, limit=str(MAX_BYTES))


def _read_all(component: str, file: str, fd: int) -> bytes:
    """The bytes of the file, from its start. The read stops one byte past
    the limit, whatever the size said."""
    chunks: list[bytes] = []
    total = 0
    while total <= MAX_BYTES:
        try:
            chunk = os.pread(fd, MAX_BYTES + 1 - total, total)
        except OSError:
            raise _refuse(NOT_READ, component, file) from None

        if not chunk:
            break

        chunks.append(chunk)
        total += len(chunk)

    if total > MAX_BYTES:
        raise _refuse(TOO_LARGE, component, file, limit=str(MAX_BYTES))

    return b"".join(chunks)


def _with_end(data: bytes) -> bytes:
    """`data`, with a line end after its last line. A file with no line
    end after its last line gets one before the first added line."""
    if not data or data.endswith(LINE_END.encode()):
        return data

    return data + LINE_END.encode()


def _present(component: str, file: str, data: bytes) -> frozenset[str]:
    """Each key that the file sets, in the grammar of a `.env`."""
    try:
        lines = parse(Text(file, _with_end(data)), FileKind.ENV)
    except EnvError as error:
        empty = [one for one in error.problems if one.fault is Fault.EMPTY_VALUE]
        if empty:
            raise _refuse(EMPTY_LINE, component, file, key=empty[0].key) from None

        raise _refuse(NOT_PARSED, component, file, problem=str(error)) from None

    return frozenset(one.key for one in lines)


def _adds(component: str, decl: EnvDecl, clone: Path, data: bytes) -> tuple[tuple[str, str], ...]:
    """Each absent key of `decl`, with its value. The catalog is read only
    when a key is absent."""
    present = _present(component, decl.file, data)
    absent = [key for key in decl.keys if key not in present]
    if not absent:
        return ()

    values = _values(component, clone)
    found: list[tuple[str, str]] = []
    for key in absent:
        value = values.get(key, "")
        if not value:
            raise _refuse(UNRESOLVED, component, decl.file, key=key)

        found.append((key, value))

    return tuple(found)


def _values(component: str, clone: Path) -> dict[str, str]:
    """Each catalog key with a value: the catalog of the clone, resolved
    against the site file, as `creche-env get` resolves it for fleet-up."""
    pair = _catalog(component, clone)
    try:
        site = read_trusted(site_file())
        settings = load(pair, site)
    except (EnvError, ReadError) as error:
        raise _refuse(BAD_SITE, component, str(site_file()), problem=str(error)) from None

    return dict(settings.values)


def _catalog(component: str, clone: Path) -> Pair:
    """The pair of `CATALOG_PARTS` in root's clone at the release commit.

    Root made the clone and nobody else writes it, so a check of each part
    with `lstat` holds until the read. A link can come out of the commit
    itself, and the read follows none."""
    directory = clone
    try:
        for part in CATALOG_PARTS:
            directory = directory / part
            if not stat.S_ISDIR(os.lstat(directory).st_mode):
                raise _refuse(NO_CATALOG, component, str(directory))

        for name in (DEFAULT_FILE, EXAMPLE_FILE):
            if not stat.S_ISREG(os.lstat(directory / name).st_mode):
                raise _refuse(NO_CATALOG, component, str(directory))
    except OSError:
        raise _refuse(NO_CATALOG, component, str(directory)) from None

    try:
        return pair_in(directory)
    except (EnvError, ReadError) as error:
        raise _refuse(BAD_CATALOG, component, str(directory), problem=str(error)) from None


def _check_after(
    component: str, file: str, data: bytes, adds: tuple[tuple[str, str], ...]
) -> bytes:
    """The bytes that step 8 appends. The file with them must still parse:
    one line for each key, at most the size limit."""
    if not adds:
        return b""

    lines = "".join(f"{key}{ASSIGN}{value}{LINE_END}" for key, value in adds)
    payload = _with_end(data)[len(data) :] + lines.encode("ascii")
    try:
        parse(Text(file, data + payload), FileKind.ENV)
    except EnvError as error:
        raise _refuse(NOT_PARSED, component, file, problem=str(error)) from None

    return payload


def _check_unchanged(
    component: str, file: str, fd: int, before: os.stat_result, read: int, operator_uid: int
) -> None:
    """Rule 5a again on the descriptor, just before the write. The file is
    the one that root read, with the bytes that root read."""
    now = os.fstat(fd)
    _check_facts(component, file, now, operator_uid)
    if (now.st_dev, now.st_ino, now.st_size) != (before.st_dev, before.st_ino, read):
        raise _refuse(CHANGED, component, file)


def _write_all(component: str, file: str, fd: int, payload: bytes) -> None:
    """Every byte of `payload` at the end of the file, then a sync."""
    view = memoryview(payload)
    try:
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise _refuse(NOT_WRITTEN, component, file)

            view = view[written:]

        os.fsync(fd)
    except OSError:
        raise _refuse(NOT_WRITTEN, component, file) from None
