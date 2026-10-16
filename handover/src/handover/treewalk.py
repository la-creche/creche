"""The walk of a tree of compiled programs (contract 06 §8.2).

A `kind: binary` tree holds its own code. It reaches outside itself in
three ways:

```
bin/caregiver -> /var/lib/creche-handover/work/<id>/caregiver/rust/target/release/caregiver
bin/caregiver    a script, or a program, that holds the path of the build
bin/caregiver    not there, while the unit starts it
```

`binary_escapes` finds all three. Two callers give it the paths of the place
where the build ran, as needles:

1. Root, at the stage of a release: `executor/selfcontained.py` gives the
   fetched work tree.
2. CI, after its build: `handover check-tree` gives the paths of the runner.

Pure: it reads the files of the tree that the caller names, and writes
nothing. It starts no child and opens no socket.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from .errors import safe_token

#: The staged tree is a link, is no directory or is not there. Step 9
#: renames `<install.to>.new`, and a rename moves a link and not the
#: directory behind it.
NOT_A_TREE: Final = "is not a directory of its own"
NOT_STAGED: Final = "is not in the staged tree"
NOT_A_PROGRAM: Final = "is not a regular file"
NOT_EXECUTABLE: Final = "is not executable"
PROGRAM_OUTSIDE: Final = "is reached through a link out of the tree"
ABSOLUTE_LINK: Final = "is a link with an absolute target"
LINK_OUTSIDE: Final = "is a link to a path outside the tree"
LINK_LOOP: Final = "is a link in a loop"
#: CONTRACT-QUESTION: contract 06 §8.2 says a tree reads no code from
#: outside itself, and names no test for a compiled program. The reading
#: taken refuses every file that holds the work tree's path. The walk
#: cannot tell a path that a program opens from a path that it only prints:
#: code that a build script generates carries its own path into a panic
#: message. A build that makes such a program must remap the path
#: (`--remap-path-prefix`), and the path holds the request id, so a fixed
#: cargo configuration cannot name it. The looser reading costs the case
#: this check is for: a program that reads a template out of the work tree
#: passes its verify hook, because the work tree is still on disk.
NAMES_WORK_TREE: Final = "holds the path of the fetched work tree"
SHARED_FILE: Final = "is a file with a second name"
UNREADABLE_FILE: Final = "is a file root cannot read"
UNLISTED_DIRECTORY: Final = "is a directory root cannot list"
NOT_A_FILE: Final = "is not a file, a directory or a link"

#: The bit `install.normalize_modes` reads: a file that its owner can run
#: gains read and execute for everyone, and no other file does.
OWNER_EXECUTE: Final = stat.S_IXUSR

#: How much of a file is read at once. A compiled program is tens of
#: megabytes, and the walk holds one chunk of it.
SCAN_CHUNK_BYTES: Final = 1024 * 1024

#: Opens a file of the staged tree without following a link, and without
#: blocking on a FIFO that the entry became after the walk listed it.
_SCAN_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def binary_escapes(
    tree: Path, programs: Sequence[Path], needles: Sequence[bytes]
) -> tuple[str, ...]:
    """Every way a staged `kind: binary` tree is not self-contained.

    `programs` are the files under `tree` that a unit or the verify command
    starts. `needles` are the paths of the place where the build ran, as
    bytes: no file of the tree may hold one. The tree itself is a directory
    first: a link in its place is the one fault reported, because every
    entry the walk would list is the link target's and not the tree's. Then
    three properties are proved, and an empty tuple means all three hold.

    1. Each program is in the tree, is a regular file and is executable. A
       link is refused whatever it names: the program must be the tree's
       own bytes.
    2. No link in the tree has an absolute target, and none resolves
       outside the tree. An absolute link into the staged tree itself is
       refused too: step 9 renames the tree, and the link then names a
       directory that is gone.
    3. No file in the tree holds a needle. A record of cargo's, a wrapper
       script and a library search path all hold the path of the build,
       and each one makes the tree read from a directory that no release
       swaps. No file has a second name either: a hard link shares its
       bytes with a path this walk cannot see, and a write through that
       path changes the tree with no release.

    Nothing here raises. A file root cannot read is a fault, because a
    file root cannot read is a file root cannot say is clean. The same
    holds for a directory root cannot list and a link root cannot resolve.
    """
    if not is_own_directory(tree):
        return (f"{safe_token(tree.name)} {NOT_A_TREE}",)

    root = tree.resolve()
    found = [fault for program in programs if (fault := _program_fault(tree, root, program))]
    found.extend(_walk_faults(tree, root, tuple(needles)))

    return tuple(found)


def is_own_directory(tree: Path) -> bool:
    """Whether `tree` is a directory by `lstat`, so a link to one is not.
    `Path.is_dir` and `os.walk` both follow a link at this place."""
    try:
        return stat.S_ISDIR(os.lstat(tree).st_mode)
    except OSError:
        return False


def _name(tree: Path, path: Path) -> str:
    """`path` as the ledger names it: relative to the tree, and only when
    it is a plain word."""
    try:
        return safe_token(path.relative_to(tree).as_posix())
    except ValueError:
        return safe_token(path.name)


def _program_fault(tree: Path, root: Path, program: Path) -> str | None:
    name = _name(tree, program)
    try:
        facts = os.lstat(program)
    except OSError:
        return f"{name} {NOT_STAGED}"

    if not stat.S_ISREG(facts.st_mode):
        return f"{name} {NOT_A_PROGRAM}"

    if not facts.st_mode & OWNER_EXECUTE:
        return f"{name} {NOT_EXECUTABLE}"

    # `lstat` follows a link in a DIRECTORY above the program. The walk
    # refuses that link too, and this says which program it carried away.
    if root not in program.resolve().parents:
        return f"{name} {PROGRAM_OUTSIDE}"

    return None


def _walk_faults(tree: Path, root: Path, needles: tuple[bytes, ...]) -> list[str]:
    """Properties 2 and 3 of `binary_escapes`, over every entry of the
    tree, in name order. No link is followed. `os.walk` skips a directory
    that it cannot list, so `unlisted` makes that directory a fault."""
    faults: list[str] = []

    def unlisted(error: OSError) -> None:
        where = error.filename
        name = _name(tree, Path(where)) if isinstance(where, str) else safe_token(where)
        faults.append(f"{name} {UNLISTED_DIRECTORY}")

    for current, directories, files in os.walk(tree, onerror=unlisted, followlinks=False):
        directories.sort()
        here = Path(current)
        for entry in sorted([*directories, *files]):
            fault = _entry_fault(tree, root, here / entry, needles)
            if fault is not None:
                faults.append(fault)

    return faults


def _entry_fault(tree: Path, root: Path, path: Path, needles: tuple[bytes, ...]) -> str | None:
    name = _name(tree, path)
    try:
        facts = os.lstat(path)
    except OSError:
        return f"{name} {UNREADABLE_FILE}"

    if stat.S_ISDIR(facts.st_mode):
        return None

    if stat.S_ISLNK(facts.st_mode):
        return _link_fault(name, root, path)

    if not stat.S_ISREG(facts.st_mode):
        return f"{name} {NOT_A_FILE}"

    if facts.st_nlink > 1:
        return f"{name} {SHARED_FILE}"

    held = _holds(path, needles)
    if held is None:
        return f"{name} {UNREADABLE_FILE}"

    return f"{name} {NAMES_WORK_TREE}" if held else None


def _link_fault(name: str, root: Path, path: Path) -> str | None:
    try:
        target = os.readlink(path)
        if os.path.isabs(target):
            return f"{name} {ABSOLUTE_LINK}"

        real = (path.parent / target).resolve()
    except (OSError, ValueError):
        return f"{name} {UNREADABLE_FILE}"
    except RuntimeError:
        # Python 3.12 raises this for a link loop, and it is no `OSError`.
        return f"{name} {LINK_LOOP}"

    # Python 3.13 raises nothing for a loop. It gives back the link at
    # which it stopped, and a path that resolved in full is never a link.
    if real.is_symlink():
        return f"{name} {LINK_LOOP}"

    if real != root and root not in real.parents:
        return f"{name} {LINK_OUTSIDE}"

    return None


def _holds(path: Path, needles: tuple[bytes, ...]) -> bool | None:
    """Whether the file holds one of `needles`. None for a file that
    cannot be read, or that is no longer a regular file. With no needle,
    the file is still read to its end, and it holds none.

    The file is read a chunk at a time. The end of each chunk is kept and
    joined to the next one, so a path that lies across two chunks is found.
    """
    keep = max((len(needle) for needle in needles), default=1) - 1
    try:
        fd = os.open(path, _SCAN_FLAGS)
    except OSError:
        return None

    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None

        tail = b""
        while chunk := os.read(fd, SCAN_CHUNK_BYTES):
            window = tail + chunk
            if any(needle in window for needle in needles):
                return True

            tail = window[-keep:] if keep else b""
    except OSError:
        return None
    finally:
        os.close(fd)

    return False
