"""A server's venv must survive its own rename too.

`test_release_r7e_relocatable.py` proves this for a `kind: venv` COMPONENT
with a real `uv`. A server's tree has the same failure
and a worse consequence: `/opt/mcp/<name>.new/bin/<entrypoint>` becomes
`/opt/mcp/<name>/bin/<entrypoint>` at the switch, the PEP spawns exactly
that path, and a console script whose shebang still names `.new` exits 126.
The upstream then fails its boot probe, the reload refuses it, and the only
symptom is `running: false` on a server that was just installed.

`_check_entrypoint` cannot catch it on its own: under `.new` the shebang is
still correct. Only a rename shows it, which is why this runs a real `uv`
rather than `McpFake`. Marked slow: it builds two environments.

Measured against the `uv` on this machine, matching the table
`test_release_r7e_relocatable.py` gives for components.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Final

import pytest
from agent_release.executor.install import BUILD_TIMEOUT_S, UV
from agent_release.executor.mcpbuild import names_the_staging_tree

pytestmark = pytest.mark.slow

#: `126` is "found, but cannot execute" and `127` is "not found" — the
#: shell's own codes for a shebang naming an interpreter that is gone.
CANNOT_EXECUTE: Final = (126, 127)

NEW_SUFFIX: Final = ".new"

#: One package with one console script, which is the shape every `pypi` and
#: `agent-mcp` server has: `run.entrypoint` names exactly such a script.
PYPROJECT: Final = """
[project]
name = "servervenvprobe"
version = "0.1.0"
requires-python = ">=3.11"

[project.scripts]
servervenvprobe = "servervenvprobe.cli:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
"""

CLI: Final = '''"""One console script, the shape run.entrypoint names."""


def main() -> int:
    print("served")

    return 0
'''


def _uv() -> str:
    """The `uv` THIS machine has. Production names `/usr/local/bin/uv`."""
    found = shutil.which("uv")

    return found if found is not None else UV


@pytest.fixture
def uv_available() -> None:
    if shutil.which("uv") is None and not Path(UV).exists():
        pytest.skip("uv is not on this machine")


def _project(root: Path) -> Path:
    package = root / "proj" / "src" / "servervenvprobe"
    package.mkdir(parents=True)
    (root / "proj" / "pyproject.toml").write_text(PYPROJECT, "utf-8")
    (package / "__init__.py").write_text('"""Probe."""\n', "utf-8")
    (package / "cli.py").write_text(CLI, "utf-8")

    return root / "proj"


def _install(project: Path, into: Path, relocatable: bool) -> None:
    """What `McpBuilder` does for a `pypi` or `agent-mcp` server: make the
    environment, then fill it from the pin."""
    if relocatable:
        subprocess.run(
            [_uv(), "venv", "--relocatable", str(into)],
            check=True,
            timeout=BUILD_TIMEOUT_S,
            capture_output=True,
        )
    else:
        subprocess.run(
            [_uv(), "venv", str(into)], check=True, timeout=BUILD_TIMEOUT_S, capture_output=True
        )

    subprocess.run(
        [
            _uv(),
            "pip",
            "install",
            "--quiet",
            "--python",
            str(into / "bin" / "python"),
            str(project),
        ],
        check=True,
        timeout=BUILD_TIMEOUT_S,
        capture_output=True,
        env=os.environ,
    )


def _rename(staged: Path) -> Path:
    """Step 9's swap, for one server tree."""
    live = staged.with_name(staged.name.removesuffix(NEW_SUFFIX))
    staged.rename(live)

    return live


def _spawn(entrypoint: Path) -> subprocess.CompletedProcess[str]:
    """What the PEP does: start the console script by absolute path."""
    return subprocess.run(
        [str(entrypoint)], capture_output=True, text=True, check=False, timeout=BUILD_TIMEOUT_S
    )


def _cannot_execute(entrypoint: Path) -> bool:
    """Whether `entrypoint` exists and cannot run, because its interpreter
    is gone. Two shapes, and which one a machine produces depends on the
    length of the path the tree was built at.

    `uv` writes `#!<tree>/bin/python` while that fits a `#!` line, and
    `execve` on a missing interpreter answers ENOENT, which reaches Python
    as `FileNotFoundError` with no exit code. Past that length `uv` writes
    a `#!/bin/sh` trampoline instead, `/bin/sh` starts, and the failed
    `exec` exits 126. Both prove the switch broke the script.

    `test_release_r7e_relocatable.py` carries the same helper and the case
    that forces both branches. The existence check separates a missing
    interpreter from a missing script, which `FileNotFoundError` alone
    cannot.
    """
    assert entrypoint.is_file(), f"no entrypoint at {entrypoint}"
    try:
        return _spawn(entrypoint).returncode in CANNOT_EXECUTE
    except FileNotFoundError:
        return True


@pytest.mark.usefixtures("uv_available")
def test_a_plain_venv_does_not_survive_the_switch(tmp_path: Path) -> None:
    """The failure, with a real `uv`. Without `--relocatable` the PEP
    spawns a script whose interpreter is gone."""
    project = _project(tmp_path)
    staged = tmp_path / "opt-mcp" / f"probe{NEW_SUFFIX}"
    _install(project, staged, relocatable=False)

    assert _cannot_execute(_rename(staged) / "bin" / "servervenvprobe")


@pytest.mark.usefixtures("uv_available")
def test_a_relocatable_venv_survives_the_switch(tmp_path: Path) -> None:
    """`mcpbuild` assumption 6, proved. The same tree, one flag earlier."""
    project = _project(tmp_path)
    staged = tmp_path / "opt-mcp" / f"probe{NEW_SUFFIX}"
    _install(project, staged, relocatable=True)

    live = _rename(staged)
    result = _spawn(live / "bin" / "servervenvprobe")

    assert result.returncode == 0
    assert result.stdout.strip() == "served"


@pytest.mark.usefixtures("uv_available")
def test_the_staged_entrypoint_check_catches_it_before_the_switch(tmp_path: Path) -> None:
    """`_check_entrypoint`'s real job, and the reason it reads more than a
    shebang LINE.

    When the staged path is too long for a `#!` line, `uv` writes a
    `#!/bin/sh` wrapper and puts the absolute interpreter path on the
    SECOND line. A check that read line 1 would pass exactly the script
    that exits 126 after the rename, which the test above proves it does.
    """
    project = _project(tmp_path)
    plain = tmp_path / "opt-mcp" / f"plain{NEW_SUFFIX}"
    moved = tmp_path / "opt-mcp" / f"moved{NEW_SUFFIX}"
    _install(project, plain, relocatable=False)
    _install(project, moved, relocatable=True)

    assert names_the_staging_tree(plain / "bin" / "servervenvprobe", plain)
    assert not names_the_staging_tree(moved / "bin" / "servervenvprobe", moved)
