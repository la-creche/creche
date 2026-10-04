"""An installed tree holds its own code (contract 06 §8.2).

`uv sync` installs a workspace member EDITABLE. A tree built without
`--no-editable` therefore holds the third-party packages and reads every
first-party module out of the repository it was built from:

```
lib/python3.12/site-packages/_editable_impl_caregiver.pth
    -> /opt/creche/caregiver/src
```

Three consequences, each against the design.

1. `sudo creche-deploy <branch>` changes the code every rework service
   loads at its NEXT restart, with no release, no tap and no `up`.
2. A rollback swaps back to `<install.to>.prev`, which holds the same `.pth`
   lines. The old third-party packages come back and the NEW first-party code
   stays, and the ledger calls that a success.
3. What the operator's phone tap binds to — the commit and the built tree
   (`stage7-releases.md` §2.5) — is not what runs.

The manifests carry `--no-editable`. This module is the FENCE behind the
flag: the executor walks every staged `kind: venv` tree before it swaps
anything, and refuses the release when the tree reads code from outside
itself. `bin/rework-cutover.sh` calls `check_tree` through this same code, so
a cutover and the first release cannot disagree about what an installed tree
is.

A fake `uv` cannot show this fault, which is why
`handover/tests/test_handover_ced_uv_guard.py` runs the real one.

**A `kind: binary` tree has the same rule and another walk.** It holds
compiled programs and no `site-packages`, so nothing in it is a `.pth`. It
reaches outside itself in three other ways, and each has the consequences
above:

```
bin/caregiver -> /var/lib/creche-handover/work/<id>/caregiver/rust/target/release/caregiver
bin/caregiver    a script, or a program, that holds the work tree's path
bin/caregiver    not there, while the unit starts it
```

`binary_escapes` finds all three, and `check_binary_tree` refuses with the
same code. The verify hook cannot find the first two: it runs while the
work tree is still on disk, so a program that reads from it passes.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Sequence
from pathlib import Path
from typing import Final, cast

from ..errors import Refusal, RefusalCode, safe_token

#: Where a venv keeps its packages. `uv venv` writes exactly one of these,
#: and the glob rather than a fixed `python3.12` is what keeps this working
#: across the interpreter the operator's host and the operator's Mac each choose.
SITE_GLOB: Final = "lib/*/site-packages"

#: PEP 610's file. `uv sync` writes `{"dir_info": {"editable": true}}` into
#: it for a workspace member installed editable.
DIRECT_URL: Final = "direct_url.json"
DIST_INFO_GLOB: Final = f"*.dist-info/{DIRECT_URL}"

#: `site.addpackage` EXECUTES a line that starts with either of these and
#: adds no path for it. `uv venv --relocatable` writes one: `_virtualenv.pth`
#: holds `import _virtualenv`. Refusing it would refuse every tree the
#: executor builds.
IMPORT_PREFIXES: Final = ("import ", "import\t")

COMMENT: Final = "#"

#: How many faults reach the ledger in one detail line. The count follows,
#: so a reader is never told a short list is the whole list.
MAX_REPORTED: Final = 5

NO_SITE_PACKAGES: Final = "the staged tree has no site-packages, so it is no venv"
UNREADABLE_PTH: Final = "a .pth file root cannot read"
UNREADABLE_DIRECT_URL: Final = f"a {DIRECT_URL} root cannot read"
OUTSIDE: Final = "names a path outside the tree"
EDITABLE_INSTALL: Final = "is installed editable"


def escapes(tree: Path) -> tuple[str, ...]:
    """Every way `tree` reads code from outside itself, in file order.

    An empty tuple means the tree is self-contained. Nothing here raises:
    a caller that wants a refusal calls `check_tree`, and the cutover script
    wants the lines.
    """
    root = tree.resolve()
    sites = sorted(root.glob(SITE_GLOB))
    if not sites:
        return (f"{NO_SITE_PACKAGES}: {safe_token(tree.name)}",)

    found: list[str] = []
    for site in sites:
        found.extend(_pth_faults(site, root))
        found.extend(_editable_faults(site))

    return tuple(found)


def check_tree(component: str, tree: Path) -> None:
    """Raise `Refusal` when the staged tree is not self-contained.

    It runs after the build and before the swap, so the old tree stays in
    service and the ledger carries the reason.
    """
    found = escapes(tree)
    if not found:
        return

    _refuse(component, found)


def _refuse(component: str, found: tuple[str, ...]) -> None:
    shown = "; ".join(found[:MAX_REPORTED])
    detail = f"the staged tree is not self-contained ({len(found)} fault(s)): {shown}"

    raise Refusal(RefusalCode.EDITABLE, component, detail)


def _pth_faults(site: Path, root: Path) -> list[str]:
    """Every `.pth` line that would put a directory outside `root` on
    `sys.path`.

    The reading follows `site.addpackage`: a blank line and a `#` line are
    skipped, an `import` line is executed rather than added, and every other
    line is a directory resolved AGAINST `site`. Relative matters — a line of
    `../../../../opt/creche/caregiver/src` escapes exactly as the
    absolute form does.
    """
    faults: list[str] = []
    for path in sorted(site.glob("*.pth")):
        name = safe_token(path.name)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            faults.append(f"{UNREADABLE_PTH}: {name}")
            continue

        faults.extend(
            f"{name} {OUTSIDE}: {safe_token(line)}"
            for line in text.splitlines()
            if _is_path_line(line) and not _inside(root, site, line)
        )

    return faults


def _is_path_line(line: str) -> bool:
    return bool(line.strip()) and not line.startswith((COMMENT, *IMPORT_PREFIXES))


def _inside(root: Path, site: Path, line: str) -> bool:
    """`site.addpackage` joins the line onto the site directory, so this
    does too, and then asks whether the result is still under the tree."""
    # CONTRACT-QUESTION: contract 06 §8.2 names no rule for a `.pth` line
    # that does not resolve. The reading taken fails closed: the line is a
    # path outside the tree, under each Python version. Python 3.13 passed
    # a line that names a link loop before. To pass it again costs a tree
    # with a path entry that nobody can state.
    try:
        candidate = (site / line.rstrip()).resolve()
    except (OSError, ValueError, RuntimeError):
        # Python 3.12 raises `RuntimeError` for a link loop, and it is no
        # `OSError`.
        return False

    # Python 3.13 raises nothing for a loop. It gives back the link at
    # which it stopped, and a path that resolved in full is never a link.
    if candidate.is_symlink():
        return False

    return candidate == root or root in candidate.parents


def _editable_faults(site: Path) -> list[str]:
    """Every package whose PEP 610 record says it was installed editable.

    An editable install names a source directory by definition, so the flag
    is the fault and there is no second path test here. A file root cannot
    parse is a package root cannot say is non-editable, and invariant 19
    answers that with a report rather than a guess.
    """
    faults: list[str] = []
    for path in sorted(site.glob(DIST_INFO_GLOB)):
        name = safe_token(path.parent.name)
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            faults.append(f"{UNREADABLE_DIRECT_URL}: {name}")
            continue

        if _says_editable(body):
            faults.append(f"{name} {EDITABLE_INSTALL}")

    return faults


def _says_editable(body: object) -> bool:
    if not isinstance(body, dict):
        return False

    info = cast("dict[str, object]", body).get("dir_info")
    if not isinstance(info, dict):
        return False

    return cast("dict[str, object]", info).get("editable") is True


# -- a tree of compiled programs ----------------------------------------------

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


def binary_escapes(tree: Path, programs: Sequence[Path], source: Path) -> tuple[str, ...]:
    """Every way a staged `kind: binary` tree is not self-contained.

    `programs` are the files under `tree` that a unit or the verify command
    starts, and `source` is the fetched work tree the build ran in. The
    tree itself is a directory first: a link in its place is the one fault
    reported, because every entry the walk would list is the link target's
    and not the tree's. Then three properties are proved, and an empty
    tuple means all three hold.

    1. Each program is in the tree, is a regular file and is executable. A
       link is refused whatever it names: the program must be the tree's
       own bytes.
    2. No link in the tree has an absolute target, and none resolves
       outside the tree. An absolute link into the staged tree itself is
       refused too: step 9 renames the tree, and the link then names a
       directory that is gone.
    3. No file in the tree holds the path of the fetched work tree. A
       record of cargo's, a wrapper script and a library search path all
       hold it, and each one makes the tree read from a directory that no
       release swaps. No file has a second name either: a hard link shares
       its bytes with a path this walk cannot see, and a write through
       that path changes the tree with no release.

    Nothing here raises. A file root cannot read is a fault, because a
    file root cannot read is a file root cannot say is clean. The same
    holds for a directory root cannot list and a link root cannot resolve.
    """
    if not _is_own_directory(tree):
        return (f"{safe_token(tree.name)} {NOT_A_TREE}",)

    root = tree.resolve()
    found = [fault for program in programs if (fault := _program_fault(tree, root, program))]
    found.extend(_walk_faults(tree, root, _needles(source)))

    return tuple(found)


def check_binary_tree(component: str, tree: Path, programs: Sequence[Path], source: Path) -> None:
    """Raise `Refusal` when the staged `kind: binary` tree is not
    self-contained. As `check_tree` does for a venv: after the build and
    before the swap, so the old tree stays in service.

    CONTRACT-QUESTION: contract 06 §8.2 names the code `editable` for a
    venv tree. The reading taken gives a binary tree the same code, because
    the fault is the same one: the artifact is wrong and no definition is.
    A code of its own costs a row in the closed list of
    `stage7-releases.md` §2.6.
    """
    found = binary_escapes(tree, programs, source)
    if not found:
        return

    _refuse(component, found)


def _is_own_directory(tree: Path) -> bool:
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


def _needles(source: Path) -> tuple[bytes, ...]:
    """The work tree's path, as the executor names it and as the kernel
    resolves it. A build can write down either one."""
    return tuple(sorted({os.fsencode(str(source)), os.fsencode(str(source.resolve()))}))


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
    cannot be read, or that is no longer a regular file.

    The file is read a chunk at a time. The end of each chunk is kept and
    joined to the next one, so a path that lies across two chunks is found.
    """
    keep = max(len(needle) for needle in needles) - 1
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
