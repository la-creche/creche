"""Every file a host script reaches for is a file this tree still has.

A script that opens a file this tree no longer has fails on the host, in a
unit, in the middle of a deploy — which is the one place
nobody is reading. `bin/tests/test_bin_modes.py` pins how those files are
committed; this pins WHAT they name.

Three rules, each over a different shape of reference:

1. **The deployed tree.** `/opt/agent-control/<path>`, `$TREE/<path>`,
   `$DEPLOYED/<path>` and `$REPO/<path>` all mean "this repository, as root
   deployed it". Every literal one must exist here.
2. **The workspace venv.** `<tree>/.venv/bin/<name>` is a console script,
   and `agent-control-deploy` fills that venv from the workspace members'
   `[project.scripts]`. A name no member declares is a unit that dies with
   status 203/EXEC.
3. **The two CI workflows.** `gate.yml` and `release.yml` must name no
   deleted path either: CI is the last thing that would notice.

A reference with a shell variable, a `<placeholder>` or a glob in its tail
is skipped — it cannot be resolved by reading the text, and guessing would
make this test lie in the other direction.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

#: A tail this test cannot resolve by reading: a variable, a placeholder, a
#: glob, or the end of a quoted string.
UNRESOLVABLE = ("$", "<", "*", '"', "'", "%")

#: Paths that are deliberately absent from the tree. Each is created on the
#: host, out of band, by the operator.
NOT_IN_THE_TREE = (
    ".venv",  # agent-control-deploy builds it
    ".git",  # the checkout's own
)

#: `python` is the interpreter every venv has; the rest come from a member's
#: `[project.scripts]`.
VENV_ALWAYS = ("python", "python3")

#: EMPTY, and it stays empty: rule 2 below guards every unit alike. An
#: entry added here is a unit allowed to break in silence: say what it is
#: and what it costs, here, before you add one.
ORPHANED: frozenset[str] = frozenset()

REFERENCE = re.compile(
    r"(?:/opt/agent-control/|\$TREE/|\$DEPLOYED/|\$REPO/)"
    r"([A-Za-z0-9._@/+-]*(?:\{[A-Za-z0-9._,+-]+\}[A-Za-z0-9._@/+-]*)?)"
)

#: One shell brace list, `{a,b}`, in a referenced path. The deploy names
#: `/opt/agent-control/{pep,indexer}/src` this way. A list that still names
#: a deleted directory stops the deploy on the host at `chmod`, and a scan
#: that does not expand the list cannot see it.
BRACES = re.compile(r"\{([A-Za-z0-9._,+-]+)\}")


def expand_braces(tail: str) -> list[str]:
    """`pep/{a,b}/src` -> `pep/a/src`, `pep/b/src`; a tail with no list -> itself."""
    found = BRACES.search(tail)
    if found is None:
        return [tail]

    head, rest = tail[: found.start()], tail[found.end() :]

    return [head + one + rest for one in found.group(1).split(",")]


VENV_SCRIPT = re.compile(r"\.venv/bin/([A-Za-z0-9._-]+)")


def _tracked(*prefixes: str) -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", *prefixes],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    return [REPO / line for line in out.stdout.splitlines() if line]


def _tracked_set() -> tuple[frozenset[str], frozenset[str]]:
    """Every tracked file, and every directory that holds one.

    The DISK is not the answer: a deleted package leaves ignored leftovers
    (`__pycache__/`, `*.egg-info/`) in a checkout and in `/opt`, so
    `Path.exists()` says yes for a directory the tree no longer has, and the
    deploy stops on the host at `chmod`. What git tracks is what a deploy's
    `git checkout -f` puts on the host."""
    files = {str(one.relative_to(REPO)) for one in _tracked(".")}
    dirs: set[str] = set()
    for name in files:
        parts = name.split("/")
        for depth in range(1, len(parts)):
            dirs.add("/".join(parts[:depth]))

    return frozenset(files), frozenset(dirs)


def in_the_tree(tail: str) -> bool:
    files, dirs = TRACKED
    return tail in files or tail in dirs


def _text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, IsADirectoryError):
        return ""


def _console_scripts() -> set[str]:
    """Every console script the surviving workspace members declare."""
    root = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    names: set[str] = set(VENV_ALWAYS)
    for member in root["tool"]["uv"]["workspace"]["members"]:
        body = tomllib.loads((REPO / member / "pyproject.toml").read_text(encoding="utf-8"))
        names |= set(body.get("project", {}).get("scripts", {}))

    return names


def _skipped(tail: str) -> bool:
    if not tail or tail.endswith("/"):
        return True

    if any(one in tail for one in UNRESOLVABLE):
        return True

    return any(tail == one or tail.startswith(f"{one}/") for one in NOT_IN_THE_TREE)


#: The scripts and units themselves. `bin/tests/` is left out on purpose: a
#: test names the paths it is testing, including ones that must NOT exist.
SCRIPTS = [one for one in _tracked("bin", "systemd") if one.parent.name != "tests"]
TRACKED = _tracked_set()
WORKFLOWS = _tracked(".github/workflows")


@pytest.mark.docs
@pytest.mark.parametrize("path", SCRIPTS, ids=lambda one: one.name)
def test_every_deployed_path_a_script_names_is_in_the_tree(path: Path) -> None:
    for number, line in enumerate(_text(path).splitlines(), start=1):
        for match in REFERENCE.finditer(line):
            for tail in expand_braces(match.group(1).rstrip(".,;:)")):
                if _skipped(tail):
                    continue

                assert in_the_tree(tail), (
                    f"{path.name}:{number} names {tail}, which git does not track"
                )


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda one: one.name)
def test_every_console_script_a_unit_runs_still_has_a_package(path: Path) -> None:
    """A unit's ExecStart into the workspace venv is the reference with no
    second chance: systemd reports 203/EXEC and the journal says nothing
    about which package went missing."""
    declared = _console_scripts()
    for number, line in enumerate(_text(path).splitlines(), start=1):
        if line.strip().startswith("#"):
            continue

        for match in VENV_SCRIPT.finditer(line):
            name = match.group(1)
            if "$" in name or name in ORPHANED:
                continue

            assert name in declared, f"{path.name}:{number} runs .venv/bin/{name}"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda one: one.name)
def test_no_workflow_names_a_path_the_deletion_removed(path: Path) -> None:
    """CI runs on a tree nobody is watching."""
    body = _text(path)
    # The `schema-v*` TAGS stay: a tag prefix is not a path, so it does not
    # match here.
    for prefix in ("materializer", "runner", "shim", "statusboard", "ui", "models", "schema"):
        assert f"{prefix}/" not in body, f"{path.name} names {prefix}/"

    assert "workbench/" not in body or (REPO / "workbench").exists()
