"""No test runs `git` with the config file of a person or of the system.

Some fixtures run `git` in a throwaway repository with the environment they
inherit (`handover/tests/test_handover_tag_script.py` is one). `git` then
reads the global config file of the person who runs the suite, and the
config file of the system. One setting there can start a program for each
repository: `core.fsmonitor`. Other settings change what a test sees: the
default branch, a commit template, a signing rule.

The root `conftest.py` names an empty global file and no system file when
pytest imports it, before pytest imports a test. Each `git` child of the run
gets the two variables.

Each test here starts pytest as a child, with a config file of a person or
of the system in the environment of the child.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

#: What makes `git` read an empty file in place of the global config file,
#: and no config file of the system. The root `conftest.py` sets both.
NO_CONFIG = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}

#: The variable that names the config file of the system, for a test that
#: cannot write the real one.
SYSTEM_FILE_ENV = "GIT_CONFIG_SYSTEM"

#: A test whose fixture runs `git init`, `git add` and `git commit` with the
#: environment it inherits. When this id moves, name another test of that file.
HANDOVER_TEST = "handover/tests/test_handover_tag_script.py::test_an_unknown_argument_stops_the_run"

#: The probe below, as the child names it.
PROBE = "bin/tests/test_git_config_dropped.py::test_git_reads_no_config_file_of_a_person"

#: What the result line of a child holds when it ran exactly one test.
ONE_PASSED = "1 passed"

#: A setting that only a config file of these tests holds.
MARKER_KEY = "creche.person"
MARKER_CONFIG = "[creche]\n\tperson = a person\n"

#: The exit code of `git config --get` for a key that no file holds.
KEY_NOT_FOUND = 1

#: What the two variables held when this module was imported: before any
#: fixture ran.
AT_IMPORT = {name: os.environ.get(name) for name in NO_CONFIG}


def _pytest(tmp_path: Path, test: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Runs one test of this repository in a child pytest.

    The child gets the environment of this process without the two variables
    of `NO_CONFIG`, then `env`. So it starts as a run from the shell of a
    person starts. The child keeps its temporary files under `tmp_path`.
    """
    inherited = {name: value for name, value in os.environ.items() if name not in NO_CONFIG}

    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            test,
            "-o",
            "addopts=",
            "-p",
            "no:cacheprovider",
            f"--basetemp={tmp_path / 'child'}",
        ],
        cwd=REPO,
        env=inherited | env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def _person_file(tmp_path: Path) -> Path:
    """A config file of a person that holds the marker."""
    path = tmp_path / "person.gitconfig"
    path.write_text(MARKER_CONFIG, encoding="utf-8")

    return path


def _home_with_config(tmp_path: Path) -> Path:
    """A home directory whose `.gitconfig` holds the marker."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text(MARKER_CONFIG, encoding="utf-8")

    return home


def test_a_fixture_that_runs_git_starts_no_program_of_a_person(tmp_path: Path) -> None:
    """The config file of a person names a program for `core.fsmonitor`. `git`
    runs that program when a command reads the index. A fixture of the
    handover test runs such commands in a throwaway repository. The program
    must not run."""
    started = tmp_path / "started"
    program = tmp_path / "fsmonitor-program"
    program.write_text(f'#!/bin/sh\necho run >> "{started}"\n', encoding="utf-8")
    program.chmod(0o755)
    person = tmp_path / "person.gitconfig"
    person.write_text(f"[core]\n\tfsmonitor = {program}\n", encoding="utf-8")

    done = _pytest(tmp_path, HANDOVER_TEST, {"GIT_CONFIG_GLOBAL": str(person)})

    assert done.returncode == 0, done.stdout + done.stderr
    assert ONE_PASSED in done.stdout, done.stdout + done.stderr
    assert not started.exists(), "a fixture ran git with the config file of a person"


@pytest.mark.parametrize("source", ["global", "home", "system"])
def test_pytest_drops_each_config_file_before_it_imports_a_test(
    tmp_path: Path, source: str
) -> None:
    """A config file of a person in a variable, one in the home directory,
    and a config file of the system. The probe holds what the two variables
    were at the import of its module: a module that runs git as it is
    imported, and a fixture of any scope, come after that."""
    env = {
        "global": lambda: {"GIT_CONFIG_GLOBAL": str(_person_file(tmp_path))},
        "home": lambda: {"HOME": str(_home_with_config(tmp_path))},
        "system": lambda: {SYSTEM_FILE_ENV: str(_person_file(tmp_path))},
    }[source]()

    done = _pytest(tmp_path, PROBE, env)

    assert done.returncode == 0, done.stdout + done.stderr
    assert ONE_PASSED in done.stdout, done.stdout + done.stderr


def test_git_reads_no_config_file_of_a_person() -> None:
    """The probe. The test above runs it in a child that got a config file
    with the marker. In a run with no such file it has nothing to find."""
    read = subprocess.run(
        ["git", "config", "--get", MARKER_KEY], capture_output=True, text=True, timeout=60
    )

    assert AT_IMPORT == NO_CONFIG
    assert read.returncode == KEY_NOT_FOUND, read.stdout + read.stderr
    assert read.stdout == ""
