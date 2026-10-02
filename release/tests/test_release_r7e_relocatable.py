"""A `kind: venv` component must survive its own rename.

Step 8 builds into `<install.to>.new` and step 9 renames that tree to
`<install.to>`. `uv sync` writes an ABSOLUTE interpreter path into every
console script it installs, so after the rename each one still names
`<install.to>.new/bin/python`, which no longer exists. Every `kind: venv`
component declares its verify hook as a console script under `install.to`
(contract 06 §8), so the hook exits 126,
the verify fails, and contract 06 §5.1 restores the whole release.

**Without it no venv release lands**, and that is five of the eight
components.

`_check_staged_hook` cannot catch it: it proves the hook exists under
`.new`, and under `.new` the shebang is still correct. Only a rename shows
it, which is why the first test below is a real one.

These run a REAL `uv`, because a fake child cannot show this. They
are marked slow: one builds two environments and that costs a few seconds.
The rest of this package's tests keep their fakes.

Measured here against uv 0.12.17:

| Build | Console script after the rename | `python -m` after it |
|---|---|---|
| `uv sync` alone | exit 126, names `<to>.new/bin/python` | works |
| `uv venv --relocatable`, then `uv sync` | exit 0 | works |

`python -m` survives either way, because the interpreter recomputes
`sys.prefix` from its own path. The console script is the whole problem,
and the verify hook is a console script.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Final

import pytest
from agent_release.catalog import Kind
from agent_release.executor.host import As
from agent_release.executor.install import (
    RELOCATABLE_ARGV,
    UV,
    Installer,
    paths_of,
)
from agent_release.manifest import parse_manifest
from release_executor_fixtures import FakeRun, fake_host, git_host_answers
from release_fixtures import manifest_text

#: What a shebang must NOT hold after the rename: an absolute path into the
#: staging name. The relocatable form names `python` beside the script.
STAGING_SUFFIX: Final = ".new"

#: `126` is "found, but cannot execute" and `127` is "not found" — the
#: shell's own codes when a shebang names an interpreter that is gone.
CANNOT_EXECUTE: Final = (126, 127)

BUILD_TIMEOUT_S: Final = 180.0

PYPROJECT: Final = """\
[project]
name = "relocprobe"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = []

[project.scripts]
relocprobe-verify = "relocprobe.cli:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
"""

CLI: Final = """\
import sys


def main() -> int:
    print("relocprobe ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
"""

MAIN: Final = """\
import sys

from .cli import main

sys.exit(main())
"""


def _project(root: Path) -> Path:
    package = root / "proj" / "src" / "relocprobe"
    package.mkdir(parents=True)
    (root / "proj" / "pyproject.toml").write_text(PYPROJECT, "utf-8")
    (package / "__init__.py").write_text('"""Probe."""\n', "utf-8")
    (package / "cli.py").write_text(CLI, "utf-8")
    (package / "__main__.py").write_text(MAIN, "utf-8")

    return root / "proj"


def _uv() -> str:
    """The `uv` THIS machine has.

    Production names `/usr/local/bin/uv`, because a child's `PATH` is built
    by `host.child_env` and is not promised to hold one. A developer's Mac
    puts it elsewhere, so the tests resolve it and the shape of the
    production argv is asserted separately.
    """
    found = shutil.which("uv")

    return found if found is not None else UV


def _sync(project: Path, into: Path) -> None:
    subprocess.run(
        [_uv(), "sync", "--quiet"],
        cwd=project,
        env=os.environ | {"UV_PROJECT_ENVIRONMENT": str(into)},
        check=True,
        timeout=BUILD_TIMEOUT_S,
    )


def _make_relocatable(project: Path, into: Path) -> None:
    subprocess.run(
        [_uv(), *RELOCATABLE_ARGV[1:], str(into)],
        cwd=project,
        check=True,
        timeout=BUILD_TIMEOUT_S,
        capture_output=True,
    )


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=BUILD_TIMEOUT_S,
    )


def _cannot_execute(script: Path) -> bool:
    """Whether `script` exists and cannot run, because its interpreter is
    gone. There are TWO shapes of that, and a test that accepted one only
    was red on GitHub's Linux runner and green on a Mac for 46 runs.

    | Shebang `uv` wrote | Starting the script |
    |---|---|
    | `#!<tree>/bin/python` | `execve` answers ENOENT, which reaches Python as `FileNotFoundError` |
    | `#!/bin/sh` and an `exec` line | `/bin/sh` starts, cannot exec the interpreter, exits 126 |

    `uv` writes the second form when the first would be too long for a
    `#!` line, so which shape a machine produces depends on the length of
    the path the tree was built at and on nothing a release does. A Linux
    runner builds under `/tmp/pytest-of-runner/...` and takes the first
    row. A Mac builds under `/private/var/folders/...`, which is long
    enough for the second.

    The existence check is not decoration: `FileNotFoundError` cannot on
    its own tell a missing interpreter from a missing script, and a test
    whose script was never built would otherwise pass.
    """
    assert script.is_file(), f"no script at {script}"
    try:
        return _run([str(script)]).returncode in CANNOT_EXECUTE
    except FileNotFoundError:
        return True


@pytest.fixture
def uv_available() -> None:
    if shutil.which("uv") is None and not Path(UV).exists():
        pytest.skip("uv is not on this machine")


def test_the_production_argv_names_uv_absolutely() -> None:
    """The constant the executor runs, checked for shape rather than for
    what this machine happens to have installed."""
    assert RELOCATABLE_ARGV[0] == UV
    assert UV.startswith("/")
    assert RELOCATABLE_ARGV[1:] == ("venv", "--relocatable")


def _manifest(tmp_path: Path, name: str, kind: Kind) -> object:
    """One valid manifest of `name`, with its install paths under
    `tmp_path` so nothing here can touch a real root."""
    text = manifest_text(name).replace(f"/opt/components/{name}", str(tmp_path / name))
    # The shared fixture writes `kind: venv` for every releasable row, so a
    # case about another kind says which one it means.
    text = text.replace(f"kind: {Kind.VENV}", f"kind: {kind}")
    parsed = parse_manifest(text, f"{name}/component.yaml")
    assert parsed.kind is kind

    return parsed


def test_the_executor_creates_the_environment_before_the_build(tmp_path: Path) -> None:
    """The fix, where it actually has to happen. Faked children, because
    what is proved here is the ORDER of the argv the executor runs."""
    manifest = _manifest(tmp_path, "pep", Kind.VENV)
    run = FakeRun(answers=git_host_answers())
    source = tmp_path / "src"
    source.mkdir()
    paths = paths_of(manifest, (tmp_path,))  # pyright: ignore[reportArgumentType]

    with pytest.raises(Exception, match="wrote no"):
        # The fake runs no real `uv`, so nothing lands and the staged hook
        # check refuses. The argv before that point is the subject.
        Installer(fake_host(tmp_path, run)).build(manifest, source, paths)  # pyright: ignore[reportArgumentType]

    first = run.seen[0]
    assert first.argv[:3] == RELOCATABLE_ARGV
    assert first.argv[-1] == str(paths.new)
    assert first.identity is As.ROOT
    assert first.cwd == source


def test_a_compose_component_gets_no_environment(tmp_path: Path) -> None:
    """A compose project has no console script to break, and a `uv venv`
    there would create a directory its own build has to work around."""
    manifest = _manifest(tmp_path, "infra", Kind.COMPOSE)
    run = FakeRun(answers=git_host_answers())
    source = tmp_path / "src"
    source.mkdir()

    with pytest.raises(Exception, match="wrote no"):
        Installer(fake_host(tmp_path, run)).build(  # pyright: ignore[reportArgumentType]
            manifest,
            source,
            paths_of(manifest, (tmp_path,)),  # pyright: ignore[reportArgumentType]
        )

    assert all(command.argv[:3] != RELOCATABLE_ARGV for command in run.seen)


@pytest.mark.parametrize("form", ("direct", "trampoline"))
def test_both_shapes_of_a_missing_interpreter_read_as_cannot_execute(
    tmp_path: Path, form: str
) -> None:
    """`_cannot_execute`'s two branches, forced rather than waited for.

    Which shape a machine produces depends on the length of the path the
    tree was built at, so one run on one machine sees one of them. Both are
    built here, so both are exercised everywhere.
    """
    gone = tmp_path / "gone" / "bin" / "python"
    script = tmp_path / "probe"
    body = (
        f"#!{gone}\nprint('never')\n"
        if form == "direct"
        else f"#!/bin/sh\n'''exec' '{gone}' \"$0\" \"$@\"\n'''\n"
    )
    script.write_text(body, encoding="utf-8")
    script.chmod(0o755)

    assert _cannot_execute(script)


def test_a_script_that_runs_does_not_read_as_cannot_execute(tmp_path: Path) -> None:
    """The other side of the helper. One that answered True for everything
    would pass the failure test and prove nothing."""
    script = tmp_path / "probe"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)

    assert not _cannot_execute(script)


@pytest.mark.usefixtures("uv_available")
def test_a_plain_sync_does_not_survive_the_rename(tmp_path: Path) -> None:
    """The bug itself, reproduced: the first row of the table above."""
    project = _project(tmp_path)
    staged = tmp_path / f"c{STAGING_SUFFIX}"
    _sync(project, staged)
    live = tmp_path / "c"
    staged.rename(live)

    assert STAGING_SUFFIX in (live / "bin" / "relocprobe-verify").read_text("utf-8")
    assert _cannot_execute(live / "bin" / "relocprobe-verify")


@pytest.mark.usefixtures("uv_available")
def test_a_relocatable_venv_survives_the_rename(tmp_path: Path) -> None:
    """The fix: create the environment first, then let the build fill it."""
    project = _project(tmp_path)
    staged = tmp_path / f"c{STAGING_SUFFIX}"
    _make_relocatable(project, staged)
    _sync(project, staged)
    live = tmp_path / "c"
    staged.rename(live)

    hook = _run([str(live / "bin" / "relocprobe-verify")])

    assert hook.returncode == 0
    assert hook.stdout.strip() == "relocprobe ok"


@pytest.mark.usefixtures("uv_available")
def test_the_build_does_not_undo_the_relocatable_shebang(tmp_path: Path) -> None:
    """`uv sync` reuses an environment it did not create, rather than
    rebuilding it, so the shebang the `uv venv` wrote is the one that
    ships. A future uv that recreated it would fail here, loudly."""
    project = _project(tmp_path)
    staged = tmp_path / f"c{STAGING_SUFFIX}"
    _make_relocatable(project, staged)
    _sync(project, staged)
    shebang = (staged / "bin" / "relocprobe-verify").read_text("utf-8")

    assert "realpath" in shebang
    assert str(staged) not in shebang


@pytest.mark.usefixtures("uv_available")
def test_python_dash_m_survives_either_way(tmp_path: Path) -> None:
    """Why the fix is about console scripts and not about the interpreter:
    `python` recomputes `sys.prefix` from its own path, so a module entry
    point was never broken. A reader who tests the wrong entry point would
    conclude the bug does not exist."""
    project = _project(tmp_path)
    staged = tmp_path / f"c{STAGING_SUFFIX}"
    _sync(project, staged)
    live = tmp_path / "c"
    staged.rename(live)

    assert _run([str(live / "bin" / "python"), "-m", "relocprobe"]).returncode == 0
