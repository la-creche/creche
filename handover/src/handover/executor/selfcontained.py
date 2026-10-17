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

`binary_escapes` finds all three with the walk of `treewalk.py`, and
`check_binary_tree` refuses with the same code. The verify hook cannot find
the first two: it runs while the work tree is still on disk, so a program
that reads from it passes.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path
from typing import Final, cast

from .. import notices, strictjson, treewalk
from ..errors import Refusal, RefusalCode, safe_token
from ..treewalk import ABSOLUTE_LINK as ABSOLUTE_LINK
from ..treewalk import LINK_LOOP as LINK_LOOP
from ..treewalk import LINK_OUTSIDE as LINK_OUTSIDE
from ..treewalk import NAMES_WORK_TREE as NAMES_WORK_TREE
from ..treewalk import NOT_A_FILE as NOT_A_FILE
from ..treewalk import NOT_A_PROGRAM as NOT_A_PROGRAM
from ..treewalk import NOT_A_TREE as NOT_A_TREE
from ..treewalk import NOT_EXECUTABLE as NOT_EXECUTABLE
from ..treewalk import NOT_STAGED as NOT_STAGED
from ..treewalk import OWNER_EXECUTE as OWNER_EXECUTE
from ..treewalk import PROGRAM_OUTSIDE as PROGRAM_OUTSIDE
from ..treewalk import SCAN_CHUNK_BYTES as SCAN_CHUNK_BYTES
from ..treewalk import SHARED_FILE as SHARED_FILE
from ..treewalk import UNLISTED_DIRECTORY as UNLISTED_DIRECTORY
from ..treewalk import UNREADABLE_FILE as UNREADABLE_FILE
from ..treewalk import is_own_directory

#: Where a venv keeps its packages. `uv venv` writes exactly one of these,
#: and the glob rather than a fixed `python3.12` is what keeps this working
#: across the interpreter the operator's host and the operator's Mac each choose.
SITE_GLOB: Final = "lib/*/site-packages"

#: PEP 610's file. `uv sync` writes `{"dir_info": {"editable": true}}` into
#: it for a workspace member installed editable.
DIRECT_URL: Final = "direct_url.json"
DIST_INFO_GLOB: Final = f"*.dist-info/{DIRECT_URL}"

#: The most bytes of one such file. The file holds a URL and a few short
#: fields, so 64 KiB is far above each real one.
#: CONTRACT-QUESTION: contract 06 §8.2 and PEP 610 name no size for this
#: file. Root reads 64 KiB of it at most, and a larger file is a record that
#: root cannot read: an `UNREADABLE_DIRECT_URL` fault. A change costs this
#: one constant.
MAX_DIRECT_URL_BYTES: Final = 64 * 1024

#: The surface word of a notice for such a file that is not strict JSON.
SURFACE: Final = "release.direct_url"

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

    The staged tree is a directory of its own, as `binary_escapes` holds a
    binary tree to. Step 9 renames it, and a rename moves a link and not
    the directory behind the link.
    """
    # CONTRACT-QUESTION: contract 06 §8.2 names no rule for a staged tree
    # that is a link. The reading taken is a fault with the code `editable`,
    # as for a binary tree. The other reading lets step 9 put a link in
    # service, and a new code costs a row in the closed list.
    if not is_own_directory(tree):
        return (f"{safe_token(tree.name)} {NOT_A_TREE}",)

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
    answers that with a report rather than a guess. The file is strict JSON
    (`strictjson.py`) of `MAX_DIRECT_URL_BYTES` at most. A file that is not
    strict is a file root cannot parse, and `notices.record` writes its log
    line with the surface `SURFACE`.
    """
    faults: list[str] = []
    for path in sorted(site.glob(DIST_INFO_GLOB)):
        name = safe_token(path.parent.name)
        try:
            with path.open("rb") as file:
                data = file.read(MAX_DIRECT_URL_BYTES + 1)

            body = strictjson.loads(data, cap=MAX_DIRECT_URL_BYTES)
        except strictjson.NotStrictJson as refusal:
            notices.record(SURFACE, refusal.rule)
            faults.append(f"{UNREADABLE_DIRECT_URL}: {name}")
            continue
        except OSError:
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


def binary_escapes(tree: Path, programs: Sequence[Path], source: Path) -> tuple[str, ...]:
    """Every way a staged `kind: binary` tree is not self-contained.

    `programs` are the files under `tree` that a unit or the verify command
    starts, and `source` is the fetched work tree the build ran in. The walk
    is `treewalk.binary_escapes`, and its needles are the two forms of
    `source`.
    """
    return treewalk.binary_escapes(tree, programs, _needles(source))


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


def _needles(source: Path) -> tuple[bytes, ...]:
    """The work tree's path, as the executor names it and as the kernel
    resolves it. A build can write down either one."""
    return tuple(sorted({os.fsencode(str(source)), os.fsencode(str(source.resolve()))}))
