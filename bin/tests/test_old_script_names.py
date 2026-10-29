"""The old names of the two renamed scripts still run the new scripts.

A unit file on the host names its script by path. A deploy replaces the
scripts, but the operator installs the unit files by hand. So for one
deploy each old name is a wrapper that runs the script under its new name.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]

#: The old name, then the new name.
RENAMED = (
    ("rework-watchdog.sh", "creche-watchdog.sh"),
    ("rework-registry-sync.sh", "creche-registry-sync.sh"),
)


@pytest.mark.parametrize(("old", "new"), RENAMED)
def test_the_old_name_runs_the_new_script(old: str, new: str) -> None:
    """An unknown flag makes each script print its usage line and exit 2,
    before it reads the host. The usage line names the new script."""
    done = subprocess.run(
        [str(BIN / old), "--no-such-flag"],
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"},
        check=False,
    )

    assert done.returncode == 2, done.stderr
    assert f"usage: {new} [--last]" in done.stdout


@pytest.mark.parametrize(("old", "new"), RENAMED)
def test_the_wrapper_holds_nothing_else(old: str, new: str) -> None:
    lines = [
        line
        for line in (BIN / old).read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]

    assert lines == [f'exec "$(dirname -- "${{BASH_SOURCE[0]}}")/{new}" "$@"']
