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
"""

from __future__ import annotations

import json
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
    try:
        candidate = (site / line.rstrip()).resolve()
    except (OSError, ValueError):
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
