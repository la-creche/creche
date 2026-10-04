"""The harness, held to its own rules with `sh` in the place of a service.

A judge that leaves a process behind, or that hides why a start failed, is
worse than no judge. Each test here breaks one rule on purpose and checks
that the harness reports it.
"""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path

import pytest
from proc_harness import (
    ProcError,
    Supervisor,
    UnixAddress,
    end_leaked_groups,
    hold_port,
    pid_is_alive,
    pids_gone_by,
    port_is_free,
)
from proc_tree import Tree

SH = "/bin/sh"
BASE_ENV = {"PATH": os.defpath}

#: Long enough that no test waits for it, short enough to end by itself if a
#: teardown is ever lost.
IDLE = "sleep 30"

#: How long a test waits for a file that a child writes at start.
MARK_DEADLINE_S = 5.0
SHORT_GRACE_S = 0.3
SHORT_DEADLINE_S = 0.2


def test_a_child_leads_its_own_process_group(tree: Tree, supervisor: Supervisor) -> None:
    child = supervisor.spawn("idle", [SH, "-c", IDLE], BASE_ENV, tree.root)

    assert os.getpgid(child.popen.pid) == child.popen.pid
    assert child.pgid != os.getpgrp()


def test_a_child_gets_only_the_environment_it_was_given(
    tree: Tree, supervisor: Supervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PROC_HARNESS_MARKER", "from-the-test-process")

    finished = supervisor.run("env", [SH, "-c", "env"], BASE_ENV | {"GIVEN": "yes"}, tree.root)

    assert finished.exit_code == 0
    assert "GIVEN=yes" in finished.stdout
    assert "PROC_HARNESS_MARKER" not in finished.stdout


def test_run_returns_the_exit_code_and_both_streams(tree: Tree, supervisor: Supervisor) -> None:
    script = "echo to-stdout; echo to-stderr >&2; exit 7"

    finished = supervisor.run("one-shot", [SH, "-c", script], BASE_ENV, tree.root)

    assert finished.exit_code == 7
    assert finished.stdout == "to-stdout\n"
    assert finished.stderr == "to-stderr\n"


def test_stop_all_ends_a_grandchild(tree: Tree, supervisor: Supervisor) -> None:
    """One signal reaches every process of the group, not only its leader."""
    mark = tree.root / "grandchild.pid"
    script = f'{IDLE} & echo "$!" > "{mark}"; wait'
    supervisor.spawn("parent", [SH, "-c", script], BASE_ENV, tree.root)
    grandchild = int(_read_when_written(mark))

    assert pid_is_alive(grandchild)
    assert supervisor.stop_all() == []
    assert pids_gone_by([grandchild], time.monotonic() + MARK_DEADLINE_S) == []


def test_a_child_that_ignores_sigterm_is_killed_and_reported(tree: Tree) -> None:
    mark = tree.root / "trap-is-set"
    script = f'trap "" TERM; echo set > "{mark}"; while :; do sleep 0.05; done'
    stubborn = Supervisor(tree.proc_logs)
    child = stubborn.spawn("stubborn", [SH, "-c", script], BASE_ENV, tree.root)
    _read_when_written(mark)

    problems = stubborn.stop_all(grace_s=SHORT_GRACE_S)

    assert problems == [f"stubborn ignored SIGTERM for {SHORT_GRACE_S} s and was killed"]
    assert child.exit_code() == -signal.SIGKILL


def test_wait_ready_reports_a_child_that_exited(tree: Tree, supervisor: Supervisor) -> None:
    """The exit code and the stderr of the child are in the error."""
    script = "echo the-reason >&2; exit 3"
    child = supervisor.spawn("broken", [SH, "-c", script], BASE_ENV, tree.root)

    with pytest.raises(ProcError, match="broken exited 3 before it was ready") as caught:
        supervisor.wait_ready(child, UnixAddress(tree.root / "no.sock"))

    assert "the-reason" in str(caught.value)


def test_wait_ready_gives_up_at_the_deadline(tree: Tree, supervisor: Supervisor) -> None:
    child = supervisor.spawn("deaf", [SH, "-c", IDLE], BASE_ENV, tree.root)

    with pytest.raises(ProcError, match="deaf was not ready"):
        supervisor.wait_ready(child, UnixAddress(tree.root / "no.sock"), SHORT_DEADLINE_S)


def test_a_group_that_no_teardown_ended_is_a_leak(tree: Tree) -> None:
    """The check at session end finds a group, names it and ends it."""
    forgotten = Supervisor(tree.proc_logs)
    child = forgotten.spawn("forgotten", [SH, "-c", IDLE], BASE_ENV, tree.root)

    leaked = end_leaked_groups()

    assert leaked == [f"forgotten (process group {child.pgid}) was still running"]
    assert child.popen.wait(MARK_DEADLINE_S) == -signal.SIGKILL
    assert end_leaked_groups() == []


def test_a_group_whose_leader_ended_is_reported_and_left_alone(tree: Tree) -> None:
    """A pid that ended can belong to another program now. It gets no signal."""
    forgotten = Supervisor(tree.proc_logs)
    finished = forgotten.run("one-shot", [SH, "-c", "exit 0"], BASE_ENV, tree.root)
    pgid = forgotten.children[0].pgid

    leaked = end_leaked_groups()

    assert finished.exit_code == 0
    assert leaked == [f"one-shot (process group {pgid}) was never stopped by its test"]


def test_a_free_port_is_below_the_ephemeral_range(supervisor: Supervisor) -> None:
    port = supervisor.free_port()

    assert 20_000 <= port < 32_000
    assert port_is_free(port)


def test_a_port_is_held_until_the_teardown(supervisor: Supervisor) -> None:
    """No second run of this suite takes the port between the check and the bind."""
    port = supervisor.free_port()

    assert hold_port(port) is None

    supervisor.stop_all()
    lock = hold_port(port)

    assert lock is not None
    os.close(lock)


def _read_when_written(path: Path) -> str:
    """The content of a file a child writes at start, once it is there."""
    deadline = time.monotonic() + MARK_DEADLINE_S

    while time.monotonic() < deadline:
        if path.exists() and (text := path.read_text(encoding="utf-8").strip()):
            return text

        time.sleep(0.01)

    raise AssertionError(f"{path} was never written")
