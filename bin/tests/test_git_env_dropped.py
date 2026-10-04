"""No test runs `git` against the repository of the caller.

git exports `GIT_DIR` to every command it starts: a hook, an alias,
`git rebase --exec`, `git bisect run`. From a linked worktree it exports
`GIT_WORK_TREE` too. Some fixtures run `git init` and `git config` in a
throwaway repository with the environment they inherit
(`handover/tests/test_handover_tag_script.py` is one). With `GIT_DIR` set,
those commands write into the repository of the caller instead. That
happened: a test run from a linked worktree left `core.bare` and a `[user]`
section in the config that every worktree of the checkout shares.

The two hooks unset the variables for what they start. A pytest that git
starts in any other way had no such guard. The root `conftest.py` drops the
five variables when pytest imports it, before pytest imports a test.

Each test here starts pytest as a child with the variables set, the way git
starts a command.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

#: The variables that point a `git` child at the repository of the caller.
#: The two hooks unset the same five (`githooks/pre-commit`).
GIT_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
)

#: A test whose fixture runs `git init` and `git config` with the environment
#: it inherits. When this id moves, name another test of that file.
HANDOVER_TEST = "handover/tests/test_handover_tag_script.py::test_an_unknown_argument_stops_the_run"

#: The probe below, as the child names it.
PROBE = "bin/tests/test_git_env_dropped.py::test_a_test_sees_none_of_the_git_variables"

#: What the result line of a child holds when it ran exactly one test.
ONE_PASSED = "1 passed"

#: The variables that this module saw at its import: before any fixture ran.
AT_IMPORT = sorted(name for name in GIT_ENV if name in os.environ)


def _git(*args: str) -> None:
    """One git command with no variable of the caller and no personal config."""
    env = {name: value for name, value in os.environ.items() if name not in GIT_ENV}
    done = subprocess.run(
        ["git", *args],
        env=env | {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stdout + done.stderr


def _files(root: Path) -> dict[str, bytes]:
    """Every file under `root` with its bytes, and every directory."""
    found: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        name = str(path.relative_to(root))
        found[name] = path.read_bytes() if path.is_file() else b"<directory>"

    return found


def _pytest(tmp_path: Path, test: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Runs one test of this repository in a child pytest that gets `env` on
    top of the environment of this process. The child keeps its temporary
    files under `tmp_path`."""
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
        env=os.environ | env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_a_fixture_that_runs_git_leaves_the_callers_repository_alone(tmp_path: Path) -> None:
    """The child gets `GIT_DIR`, as a command that git starts does. A fixture
    of the handover test runs `git init` and `git config`. Nothing in the
    repository that `GIT_DIR` names may change."""
    caller = tmp_path / "caller"
    _git("init", "-q", "-b", "main", str(caller))
    git_dir = caller / ".git"
    config = (git_dir / "config").read_bytes()
    before = _files(git_dir)

    done = _pytest(tmp_path, HANDOVER_TEST, {"GIT_DIR": str(git_dir)})

    assert done.returncode == 0, done.stdout + done.stderr
    assert ONE_PASSED in done.stdout, done.stdout + done.stderr
    assert (git_dir / "config").read_bytes() == config
    after = _files(git_dir)
    names = sorted(before.keys() | after.keys())
    changed = [name for name in names if before.get(name) != after.get(name)]
    assert changed == [], f"the test run changed {changed} in the repository of the caller"


def test_pytest_drops_every_git_variable_before_it_imports_a_test(tmp_path: Path) -> None:
    """All five, and at the import of a test module: a module that runs git
    as it is imported, and a fixture of any scope, come after that."""
    leaked = {name: str(tmp_path / name.lower()) for name in GIT_ENV}

    done = _pytest(tmp_path, PROBE, leaked)

    assert done.returncode == 0, done.stdout + done.stderr
    assert ONE_PASSED in done.stdout, done.stdout + done.stderr


def test_a_test_sees_none_of_the_git_variables() -> None:
    """The probe. The test above runs it in a child that got all five. In a
    run that git did not start it has nothing to find."""
    assert AT_IMPORT == []
    assert sorted(name for name in GIT_ENV if name in os.environ) == []
