"""The wheel of `creche-env` holds the package data.

A program of an installed tree finds the pair beside the module
(`envfile.package_dir`). The build backend leaves out a file that it does
not take as a part of the package, so this test builds the wheel with the
real `uv` and reads its member names.

Marked slow: it builds a wheel in a child process.
"""

from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Final

import pytest

pytestmark = pytest.mark.slow

PACKAGE_DIR: Final = Path(__file__).resolve().parents[1]

#: The package data that each tree needs.
DATA: Final = ("default.env", "example.env", "root-keys.txt")

BUILD_TIMEOUT_S: Final = 600.0

#: A build failure that means that this machine could not reach the index,
#: and not that the package is broken.
NETWORK_MARKERS: Final = ("failed to fetch", "could not connect", "dns error", "offline")


def test_the_wheel_holds_the_pair_and_the_root_keys(tmp_path: Path) -> None:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("the test builds a wheel, and the sandbox ships no uv")

    built = subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(tmp_path), str(PACKAGE_DIR)],
        capture_output=True,
        text=True,
        timeout=BUILD_TIMEOUT_S,
        check=False,
    )
    if built.returncode != 0 and any(one in built.stderr.lower() for one in NETWORK_MARKERS):
        pytest.skip(f"this machine could not reach the package index: {built.stderr[-300:]}")

    assert built.returncode == 0, built.stderr
    (wheel,) = tmp_path.glob("creche_env-*.whl")
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())

    assert {f"creche_env/{name}" for name in DATA} <= names
