"""A run-time data file must be IN the wheel (contract 06 §8.2).

An editable install puts the source directory on `sys.path`, so a package
that reads a template, a stylesheet or a YAML default beside its own module
finds it whether or not the wheel carries it. A non-editable tree does not
see the source tree at all: an unpackaged data file becomes a
`FileNotFoundError` at start-up, on the host, after the swap.

One package in this workspace reads such a file today, found by grepping
every first-party package for `Path(__file__)`, `importlib.resources` and
`parents[`:

| Package | File | How it reads it |
|---|---|---|
| `noticeboard` | `templates/*.html`, `static/*.css` | `Path(__file__).parent` in `app.py` |

The rule below names no package. It builds the wheel of every
workspace member that ships a non-Python file and asks whether the wheel
carries it, so a data file added to any package next month is covered
without an edit here.

Marked slow: it builds a wheel per such member. Each is well under a
second, because a wheel build resolves no dependency.
"""

from __future__ import annotations

import shutil
import subprocess
import tomllib
import zipfile
from pathlib import Path
from typing import Final

import pytest
from agent_release.executor.install import UV

pytestmark = pytest.mark.slow

REPO_ROOT: Final = Path(__file__).resolve().parents[2]

BUILD_TIMEOUT_S: Final = 300.0

#: Never in a wheel and never read at run time. `AGENTS.md` is an
#: instruction file for whoever edits the directory.
NOT_DATA: Final = frozenset({".pyc", ".pyo", ".pyi", ".so", ".dylib"})


def _uv() -> str:
    found = shutil.which("uv")

    return found if found is not None else UV


@pytest.fixture(scope="module")
def uv_available() -> None:
    if shutil.which("uv") is None and not Path(UV).exists():
        pytest.skip("uv is on neither the PATH nor /usr/local/bin: no wheel can be built")


def _members() -> list[Path]:
    """Every workspace member, from the root `pyproject.toml` itself."""
    body = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    workspace = body["tool"]["uv"]["workspace"]["members"]

    return [REPO_ROOT / str(one) for one in workspace]


def _data_files(member: Path) -> list[Path]:
    """Every non-Python file under the member's package, relative to `src`."""
    src = member / "src"
    if not src.is_dir():
        return []

    return sorted(
        path.relative_to(src)
        for path in src.rglob("*")
        if path.is_file()
        and path.suffix not in NOT_DATA
        and path.suffix != ".py"
        and not _is_build_litter(path.relative_to(src))
    )


def _is_build_litter(relative: Path) -> bool:
    """A file a BUILD writes under `src` and removes again, not one a package
    ships. `uv` builds a member in place, and for the length of that build
    `src/<name>.egg-info/` and dot-files exist. This list is read at
    COLLECTION time, so under `pytest -n auto` two workers that collect while
    a build runs see different parameters and xdist refuses the whole run
    ("Different tests were collected between gw2 and gw0"). `__pycache__` is
    the same race one layer down: CPython writes a bytecode file as
    `<name>.pyc.<tmp>` and renames it, and that temp name has no `.pyc`
    suffix, so a worker that collects while another imports counts it as
    data. What a package ships never sits in any of the three."""
    return any(
        part.endswith(".egg-info") or part.startswith(".") or part == "__pycache__"
        for part in relative.parts
    )


def _with_data() -> list[Path]:
    return [member for member in _members() if _data_files(member)]


def _package_name(member: Path) -> str:
    body = tomllib.loads((member / "pyproject.toml").read_text(encoding="utf-8"))

    return str(body["project"]["name"])


def test_the_scan_finds_the_package_that_ships_data() -> None:
    """The discovery itself. A rule that silently matched nothing would
    pass forever and prove nothing, so the set is named once, here."""
    found = {member.name for member in _with_data()}

    assert {"noticeboard"} <= found, found


@pytest.mark.parametrize("member", _with_data(), ids=lambda one: one.name)
def test_every_data_file_a_package_ships_is_in_its_wheel(
    member: Path, tmp_path: Path, uv_available: None
) -> None:
    del uv_available
    name = _package_name(member)
    done = subprocess.run(
        [_uv(), "build", "--package", name, "--wheel", "--out-dir", str(tmp_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=BUILD_TIMEOUT_S,
    )
    assert done.returncode == 0, done.stderr

    built = next(tmp_path.glob("*.whl"))
    with zipfile.ZipFile(built) as wheel:
        carried = set(wheel.namelist())

    missing = [str(one) for one in _data_files(member) if str(one) not in carried]

    assert missing == [], f"{name}: {missing}"
