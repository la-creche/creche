"""The record of a stand-in, held to one rule: a pid is not a name.

A stand-in records its pid at its start. When it ends, the system can give
that pid to another program. So nothing here sends a signal to a recorded
pid that is in no session of the test.

Each test writes the record by hand, in the shape that the head of
`proc_standins.py` gives, and puts `sh` in the place of each program.
"""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path

import pytest
from proc_harness import Supervisor, pid_is_alive, pids_gone_by
from proc_stack import Stack
from proc_standins import (
    PI,
    SBX,
    SESSIONS_MOUNT_ENV,
    TERMINAL_FLAG,
    calls_of,
    end_standins,
    install_sbx,
)
from proc_tree import FAMILY, SANDBOX, Tree, build_tree

SH = "/bin/sh"
BASE_ENV = {"PATH": os.defpath}
IDLE = "sleep 30"
MARK_DEADLINE_S = 5.0
SHORT_DEADLINE_S = 0.2


def test_a_standin_that_outlives_its_service_is_killed(tree: Tree, supervisor: Supervisor) -> None:
    """A recorded pid in a session of the test is a process of the test."""
    child = supervisor.spawn("stays", [SH, "-c", IDLE], BASE_ENV, tree.root)
    _record(tree, PI, child.popen.pid)

    problems = end_standins(tree, supervisor.sessions(), SHORT_DEADLINE_S)

    assert problems == [f"stand-in process {child.popen.pid} outlived its service and was killed"]
    assert child.wait(MARK_DEADLINE_S) == -signal.SIGKILL


def test_a_recorded_pid_of_another_program_gets_no_signal(
    tree: Tree, supervisor: Supervisor
) -> None:
    """The stand-in ended, and its pid names a program of no session here."""
    other = supervisor.spawn("other-program", [SH, "-c", IDLE], BASE_ENV, tree.root)
    _record(tree, PI, other.popen.pid)

    problems = end_standins(tree, frozenset(), SHORT_DEADLINE_S)

    assert problems == [
        f"the recorded pid {other.popen.pid} names a process outside this test: no signal sent"
    ]
    assert other.exit_code() is None


def test_kill_playpens_ends_a_playpen_of_attendance(tree: Tree, supervisor: Supervisor) -> None:
    """A playpen keeps the session of the `attendance` that started it."""
    mark = tree.root / "playpen.pid"
    script = f'{IDLE} & echo "$!" > "{mark}"; wait'
    stack = Stack(tree, supervisor)
    stack.attendance = supervisor.spawn("attendance", [SH, "-c", script], BASE_ENV, tree.root)
    playpen = int(_read_when_written(mark))
    _record(tree, SBX, playpen)

    assert stack.kill_playpens() == [playpen]
    assert pids_gone_by([playpen], time.monotonic() + MARK_DEADLINE_S) == []


def test_kill_playpens_leaves_another_program_alone(tree: Tree, supervisor: Supervisor) -> None:
    """The playpen ended earlier, and its pid names a program of another session."""
    stack = Stack(tree, supervisor)
    stack.attendance = supervisor.spawn("attendance", [SH, "-c", IDLE], BASE_ENV, tree.root)
    other = supervisor.spawn("other-program", [SH, "-c", IDLE], BASE_ENV, tree.root)
    _record(tree, SBX, other.popen.pid)

    assert stack.kill_playpens() == []
    assert pid_is_alive(other.popen.pid)
    assert other.exit_code() is None


def test_the_sbx_wrapper_records_the_terminal_flag_and_drops_it(
    tree: Tree, supervisor: Supervisor
) -> None:
    """The record holds the call as the service made it. The stand-in gets no `-it`.

    The stand-in refuses a second word before the command. So an exit code
    of 0 is the proof that the flag was dropped.
    """
    build_tree(tree)
    install_sbx(tree)
    words = ["exec", TERMINAL_FLAG, "--env-file", str(tree.playpen_env()), SANDBOX, "--", "env"]

    finished = supervisor.run("sbx", [str(tree.bin_dir / SBX), *words], BASE_ENV, tree.root)

    (call,) = calls_of(tree, SBX)
    assert finished.exit_code == 0, finished.stderr
    assert call.argv == tuple(words)


def test_the_sbx_wrapper_keeps_the_words_of_the_command(tree: Tree, supervisor: Supervisor) -> None:
    """A word after the separator belongs to the command, `-it` too."""
    build_tree(tree)
    install_sbx(tree)
    command = [SH, "-c", 'printf "%s\\n" "$@"', "sh", TERMINAL_FLAG, "--", "two words"]
    words = ["exec", "--env-file", str(tree.playpen_env()), SANDBOX, "--", *command]

    finished = supervisor.run("sbx", [str(tree.bin_dir / SBX), *words], BASE_ENV, tree.root)

    assert finished.exit_code == 0, finished.stderr
    assert finished.stdout.split("\n")[:-1] == [TERMINAL_FLAG, "--", "two words"]


@pytest.mark.parametrize(
    ("sandbox", "family"), [(SANDBOX, FAMILY), ("code-sandbox-s12", "code-sandbox")]
)
def test_the_sbx_wrapper_names_the_sessions_mount_of_the_family(
    tree: Tree, supervisor: Supervisor, sandbox: str, family: str
) -> None:
    """Contract 03 §7.6 rule 4: the family of the sandbox id names the sessions mount."""
    build_tree(tree)
    install_sbx(tree)
    words = ["exec", "--env-file", str(tree.playpen_env()), sandbox, "--", "env"]

    finished = supervisor.run("sbx", [str(tree.bin_dir / SBX), *words], BASE_ENV, tree.root)

    assert finished.exit_code == 0, finished.stderr
    assert f"{SESSIONS_MOUNT_ENV}={tree.sessions_root / family}" in finished.stdout.split("\n")


def _record(tree: Tree, name: str, pid: int) -> None:
    """One call of a stand-in with no argument, as its wrapper leaves it."""
    directory = tree.standins / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{pid}.argv").write_text("\n", encoding="utf-8")

    with (directory / "order").open("a", encoding="utf-8") as order:
        order.write(f"{pid}\n")


def _read_when_written(path: Path) -> str:
    """The content of a file a child writes at start, once it is there."""
    deadline = time.monotonic() + MARK_DEADLINE_S

    while time.monotonic() < deadline:
        if path.exists() and (text := path.read_text(encoding="utf-8").strip()):
            return text

        time.sleep(0.01)

    raise AssertionError(f"{path} was never written")
