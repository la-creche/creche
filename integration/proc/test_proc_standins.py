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

from proc_harness import Supervisor
from proc_standins import SYSTEMCTL, end_standins
from proc_tree import Tree

SH = "/bin/sh"
BASE_ENV = {"PATH": os.defpath}
IDLE = "sleep 30"
MARK_DEADLINE_S = 5.0
SHORT_DEADLINE_S = 0.2


def test_a_standin_that_outlives_its_service_is_killed(tree: Tree, supervisor: Supervisor) -> None:
    """A recorded pid in a session of the test is a process of the test."""
    child = supervisor.spawn("stays", [SH, "-c", IDLE], BASE_ENV, tree.root)
    _record(tree, SYSTEMCTL, child.popen.pid)

    problems = end_standins(tree, supervisor.sessions(), SHORT_DEADLINE_S)

    assert problems == [f"stand-in process {child.popen.pid} outlived its service and was killed"]
    assert child.wait(MARK_DEADLINE_S) == -signal.SIGKILL


def test_a_recorded_pid_of_another_program_gets_no_signal(
    tree: Tree, supervisor: Supervisor
) -> None:
    """The stand-in ended, and its pid names a program of no session here."""
    other = supervisor.spawn("other-program", [SH, "-c", IDLE], BASE_ENV, tree.root)
    _record(tree, SYSTEMCTL, other.popen.pid)

    problems = end_standins(tree, frozenset(), SHORT_DEADLINE_S)

    assert problems == [
        f"the recorded pid {other.popen.pid} names a process outside this test: no signal sent"
    ]
    assert other.exit_code() is None


def _record(tree: Tree, name: str, pid: int) -> None:
    """One call of a stand-in with no argument, as its wrapper leaves it."""
    directory = tree.standins / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{pid}.argv").write_text("\n", encoding="utf-8")

    with (directory / "order").open("a", encoding="utf-8") as order:
        order.write(f"{pid}\n")
