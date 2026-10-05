"""No `git` command of a test run starts work that outlives it.

After a commit, a merge or a fetch, `git` starts `git maintenance run --auto`
and does not wait for it. A repository that receives a push does the same.
From git 2.55 that process keeps the file `objects/maintenance.lock` until it
ends, after the command returned. One test of the gate removed `.git` of a
throwaway repository right after a commit of its fixture. The delete listed
the file, the process removed it, and the test failed with
`FileNotFoundError: 'maintenance.lock'`. That happened on `main`, after the
same test passed in the pull request, and it stopped the tags of that push.

The root `conftest.py` sets two settings when pytest imports it, before
pytest imports a test. `maintenance.auto=false` stops that process.
`gc.autoDetach=false` keeps maintenance that a fixture asks for in the
foreground. Each setting reaches a `git` child in two ways:

- As a pair in the environment. A fixture that names its own global config
  file still gets the pair.
- In the global config file of the run. `git` removes the pairs from the
  environment of a repository that receives a push. That repository reads
  the file.

Each test here reads the trace of one command. The trace names each program
that the command and its children started. So a test does not depend on how
fast the maintenance ends.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

#: The variable that names the global config file, and what the file of the
#: root `conftest.py` holds.
GLOBAL_ENV = "GIT_CONFIG_GLOBAL"
SETTINGS = "[maintenance]\n\tauto = false\n[gc]\n\tautoDetach = false\n"

#: The start of the name of the directory that the root `conftest.py` makes
#: for that file. A test here writes into no other directory, and removes no
#: other one. Without the variable of the root `conftest.py`, the path is the
#: config file of a person.
SETTINGS_PREFIX = "git-settings-"

#: Each variable that the root `conftest.py` sets for a `git` child.
SET = (
    GLOBAL_ENV,
    "GIT_CONFIG_NOSYSTEM",
    "GIT_ATTR_NOSYSTEM",
    "GIT_CONFIG_COUNT",
    *(f"GIT_CONFIG_{part}_{index}" for index in range(4) for part in ("KEY", "VALUE")),
)

#: The variable that makes `git` append one line to a file for each program
#: it starts.
TRACE_ENV = "GIT_TRACE"

#: What the trace holds when a command started the maintenance, and the word
#: that sends the maintenance to the background.
MAINTENANCE = "git maintenance run"
DETACH = "--detach"

#: The line of the trace for a command, up to its first argument.
BUILT_IN = "trace: built-in: git {name}"

#: The author of each commit here. No config file holds one.
AUTHOR = ("-c", "user.email=t@t", "-c", "user.name=t")

#: A test whose fixture runs `git commit` and `git push` with the environment
#: it inherits. When this id moves, name another test of that file.
HANDOVER_TEST = "handover/tests/test_handover_tag_script.py::test_an_unknown_argument_stops_the_run"

#: What the result line of a child holds when it ran exactly one test.
ONE_PASSED = "1 passed"

#: A config file of a person who asks for the maintenance.
PERSON_CONFIG = "[maintenance]\n\tauto = true\n"

#: Imports the root `conftest.py` as pytest does, then starts a copy of the
#: process that ends as a process ends. Prints the path of the global config
#: file, and whether the file is there after the copy ended.
FORK_PROGRAM = """
import os
import sys

import conftest

path = os.environ["GIT_CONFIG_GLOBAL"]
copy = os.fork()
if copy == 0:
    sys.exit(0)

os.waitpid(copy, 0)
print(path)
print(os.path.exists(path))
"""

#: Imports the root `conftest.py`, then removes the directory of the global
#: config file, as a program that empties the temporary directory does.
#:
#: The program removes only a directory that the root `conftest.py` made for
#: it: one with the name of `SETTINGS_PREFIX`, in the temporary directory that
#: the test names. For each other path it ends with an error and removes
#: nothing.
GONE_PROGRAM = f"""
import os
import shutil
import sys

import conftest

directory = os.path.dirname(os.environ["GIT_CONFIG_GLOBAL"])
if os.path.dirname(directory) != os.environ["TMPDIR"]:
    sys.exit("not in the temporary directory of the test: " + directory)

if not os.path.basename(directory).startswith({SETTINGS_PREFIX!r}):
    sys.exit("not a directory of the root conftest.py: " + directory)

os.chmod(directory, 0o700)
shutil.rmtree(directory)
"""


def _git(cwd: Path, *args: str, env: Mapping[str, str] | None = None) -> None:
    """One git command with the environment of this process, then `env`."""
    done = subprocess.run(
        ["git", *AUTHOR, *args],
        cwd=cwd,
        env=os.environ | (env or {}),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stdout + done.stderr


def _traced(cwd: Path, *args: str, env: Mapping[str, str] | None = None) -> str:
    """Runs one git command with a trace. Returns the trace."""
    trace = cwd.parent / "trace"
    _git(cwd, *args, env={**(env or {}), TRACE_ENV: str(trace)})

    return trace.read_text(encoding="utf-8")


def _change(repo: Path, name: str, env: Mapping[str, str] | None = None) -> None:
    """Writes one file and adds it to the index."""
    (repo / name).write_text(f"{name}\n", encoding="utf-8")
    _git(repo, "add", "-A", env=env)


def _commit(repo: Path, name: str) -> None:
    _change(repo, name)
    _git(repo, "commit", "-q", "-m", name)


def _repository(path: Path) -> Path:
    """A repository with one commit."""
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _commit(path, "one")

    return path


def _pytest(tmp_path: Path, test: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Runs one test of this repository in a child pytest.

    The child gets the environment of this process without the variables of
    `SET`, then `env`. So it starts as a run from the shell of a person
    starts. The child keeps its temporary files under `tmp_path`.
    """
    inherited = {name: value for name, value in os.environ.items() if name not in SET}

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


def test_a_commit_starts_no_maintenance(tmp_path: Path) -> None:
    repo = _repository(tmp_path / "repo")
    _change(repo, "two")

    trace = _traced(repo, "commit", "-q", "-m", "two")

    assert BUILT_IN.format(name="commit") in trace
    assert MAINTENANCE not in trace


def test_a_merge_starts_no_maintenance(tmp_path: Path) -> None:
    repo = _repository(tmp_path / "repo")
    _git(repo, "checkout", "-q", "-b", "side")
    _commit(repo, "theirs")
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "ours")

    trace = _traced(repo, "merge", "-q", "--no-edit", "side")

    assert BUILT_IN.format(name="merge") in trace
    assert MAINTENANCE not in trace


def test_a_fetch_starts_no_maintenance(tmp_path: Path) -> None:
    origin = _repository(tmp_path / "origin")
    _git(tmp_path, "clone", "-q", str(origin), "clone")
    _commit(origin, "two")

    trace = _traced(tmp_path / "clone", "fetch", "-q")

    assert BUILT_IN.format(name="fetch") in trace
    assert MAINTENANCE not in trace


def test_a_repository_that_receives_a_push_starts_no_maintenance(tmp_path: Path) -> None:
    """`git push` starts `git receive-pack` in the other repository, with no
    pair of the environment. That repository reads the global config file."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    repo = _repository(tmp_path / "repo")

    trace = _traced(repo, "push", "-q", str(origin), "main")

    assert BUILT_IN.format(name="receive-pack") in trace
    assert MAINTENANCE not in trace


def test_a_fixture_with_its_own_global_file_starts_no_maintenance(tmp_path: Path) -> None:
    """Some fixtures name the empty file as the global config file of each
    command (`bin/tests/test_gate_workflow.py` is one). Such a command does
    not read the file of the root `conftest.py`. It gets the pairs."""
    own = {GLOBAL_ENV: os.devnull}
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main", env=own)
    _change(repo, "one", env=own)

    trace = _traced(repo, "commit", "-q", "-m", "one", env=own)

    assert BUILT_IN.format(name="commit") in trace
    assert MAINTENANCE not in trace


def test_a_fixture_that_asks_for_maintenance_gets_it_in_the_foreground(tmp_path: Path) -> None:
    """`git -c` outranks a pair, so a fixture can still ask. The maintenance
    then ends before the command returns: nothing sends it to the
    background."""
    repo = _repository(tmp_path / "repo")
    _change(repo, "two")

    trace = _traced(repo, "-c", "maintenance.auto=true", "commit", "-q", "-m", "two")

    assert MAINTENANCE in trace
    assert DETACH not in trace


def test_a_fixture_that_commits_and_pushes_starts_no_maintenance(tmp_path: Path) -> None:
    """A fixture of the handover test runs `git commit` and `git push` in a
    throwaway repository. The child gets a config file of a person who asks
    for the maintenance."""
    person = tmp_path / "person.gitconfig"
    person.write_text(PERSON_CONFIG, encoding="utf-8")
    trace = tmp_path / "trace"

    done = _pytest(tmp_path, HANDOVER_TEST, {GLOBAL_ENV: str(person), TRACE_ENV: str(trace)})

    assert done.returncode == 0, done.stdout + done.stderr
    assert ONE_PASSED in done.stdout, done.stdout + done.stderr
    commands = trace.read_text(encoding="utf-8")
    assert BUILT_IN.format(name="commit") in commands
    assert BUILT_IN.format(name="receive-pack") in commands
    assert MAINTENANCE not in commands


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes into a read-only directory anyway")
def test_a_test_cannot_write_the_global_config_file() -> None:
    """A setting that one test wrote there would reach each later test of
    the process. The directory of the file takes no new file, so `git` cannot
    replace the file.

    The command runs only against a file of the root `conftest.py`."""
    directory = Path(os.environ[GLOBAL_ENV]).parent
    assert directory.name.startswith(SETTINGS_PREFIX), directory

    done = subprocess.run(
        ["git", "config", "--global", "creche.written", "yes"],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert done.returncode != 0, done.stdout + done.stderr
    assert Path(os.environ[GLOBAL_ENV]).read_text(encoding="utf-8") == SETTINGS


def _python(tmp_path: Path, program: str) -> subprocess.CompletedProcess[str]:
    """Runs one program at the root of this repository, where it can import
    the root `conftest.py`. The program keeps its temporary files under
    `tmp_path`."""
    return subprocess.run(
        [sys.executable, "-c", program],
        cwd=REPO,
        env=os.environ | {"TMPDIR": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_a_process_removes_its_global_config_file_when_it_ends(tmp_path: Path) -> None:
    """Each pytest process makes one file. A copy of the process holds the
    same exit handler, and a copy that ends must leave the file to the
    process that made it."""
    done = _python(tmp_path, FORK_PROGRAM)

    assert done.returncode == 0, done.stdout + done.stderr
    path, there_after_the_copy = done.stdout.split("\n")[:2]
    assert Path(path).parent.parent == tmp_path
    assert there_after_the_copy == "True"
    assert not Path(path).parent.exists()


def test_a_process_ends_with_no_error_when_its_file_is_gone(tmp_path: Path) -> None:
    done = _python(tmp_path, GONE_PROGRAM)

    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stderr == ""
