"""The harness, held to its own rules with `sh` in the place of a service.

A judge that leaves a process behind, or that hides why a start failed, is
worse than no judge. Each test here breaks one rule on purpose and checks
that the harness reports it.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import proc_harness
import pytest
from proc_harness import (
    ProcError,
    Supervisor,
    UnixAddress,
    end_leaked_groups,
    hold_port,
    kill_pid,
    pid_is_alive,
    pids_gone_by,
    port_is_free,
)
from proc_report import end_processes
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

#: A leader that leaves one ended process in its group, with a parent that
#: never reaps it. `sh` cannot do this: it has no call that changes a group.
#:
#:   the leader    waits for SIGTERM
#:     the holder  moves to a group of its own, and reaps nothing
#:       ended     stays in the group of the leader, and ends at once
#:
#: The holder writes its pid and the pid of `ended`. It ends by itself when a
#: test loses it.
ENDED_MEMBER = """
import os, sys, time

holder = os.fork()
if holder:
    time.sleep(30)
    os._exit(0)

ended = os.fork()
if ended == 0:
    os._exit(0)

os.setpgid(0, 0)
with open(sys.argv[1] + ".tmp", "w") as mark:
    mark.write(f"{os.getpid()} {ended}")
os.replace(sys.argv[1] + ".tmp", sys.argv[1])
time.sleep(30)
os._exit(0)
"""

#: One line of `/proc/<pid>/stat` on Linux, as proc(5) gives it. The name of
#: the program holds a space and a bracket on purpose. The fields are the
#: pid, the state, the group, the session and the thread count.
STAT_LINE = (
    "{pid} (a (odd) name) {state} 1 {pgid} {session} 0 -1 4194560 0 0 0 0 0 0 0 0 20 0 "
    "{threads} 0 5309 0 0 18446744073709551615 0 0 0 0 0 0 0 0 0 0 0 0 0 17 1 0 0 0 0 0 0 0 0 0 "
    "0 0 0 0\n"
)

#: The session follows the group in that line. A written line gives the two
#: different numbers, so a harness that reads the wrong field finds no group.
SESSION_OFFSET = 7

#: The states of proc(5): a process that runs, one that sleeps, a zombie.
RUNS = "R"
SLEEPS = "S"
ZOMBIE = "Z"

#: A pid for a `stat` file that no test starts a process for.
OTHER_PID = 70_000


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


def test_a_teardown_failure_carries_the_output(tree: Tree) -> None:
    """What the child wrote is the evidence, and the root is gone after the teardown."""
    mark = tree.root / "trap-is-set"
    loop = "while :; do sleep 0.05; done"
    script = f'trap "" TERM; echo the-evidence >&2; echo set > "{mark}"; {loop}'
    stubborn = Supervisor(tree.proc_logs)
    stubborn.spawn("stubborn", [SH, "-c", script], BASE_ENV, tree.root)
    _read_when_written(mark)

    failure = end_processes(tree, stubborn, SHORT_GRACE_S)

    assert failure is not None
    assert f"stubborn ignored SIGTERM for {SHORT_GRACE_S} s and was killed" in failure
    assert "--- stubborn stderr ---\nthe-evidence" in failure


def test_a_clean_teardown_is_no_failure(tree: Tree) -> None:
    calm = Supervisor(tree.proc_logs)
    calm.spawn("idle", [SH, "-c", IDLE], BASE_ENV, tree.root)

    assert end_processes(tree, calm) is None


def test_a_member_that_outlives_sigterm_is_killed_and_reported(tree: Tree) -> None:
    """The leader ends at SIGTERM and a process of its group does not.

    On the host the stop of such a unit runs into `TimeoutStopSec`. The
    member writes its mark after it set the trap, so the signal cannot come
    first.
    """
    mark = tree.root / "member.pid"
    member_script = 'trap "" TERM; echo "$$" > "$0"; exec sleep 60'
    script = f"{SH} -c '{member_script}' \"{mark}\" & wait"
    holder = Supervisor(tree.proc_logs)
    child = holder.spawn("holder", [SH, "-c", script], BASE_ENV, tree.root)
    member = int(_read_when_written(mark))

    problems = holder.stop_all(grace_s=SHORT_GRACE_S)

    outlived = f"a process of holder outlived SIGTERM for {SHORT_GRACE_S} s and was killed"
    assert problems == [outlived]
    assert child.exit_code() == -signal.SIGTERM
    assert pids_gone_by([member], time.monotonic() + MARK_DEADLINE_S) == []


def test_an_ended_group_gets_no_signal(
    tree: Tree, supervisor: Supervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The system can give the id of an ended group to another program."""
    finished = supervisor.run("one-shot", [SH, "-c", "exit 0"], BASE_ENV, tree.root)
    pgid = supervisor.children[0].pgid
    signalled = _record_group_signals(monkeypatch)

    assert supervisor.stop_all() == []
    assert finished.exit_code == 0
    assert pgid not in signalled


def test_a_second_stop_all_sends_no_signal(
    tree: Tree, supervisor: Supervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = supervisor.spawn("idle", [SH, "-c", IDLE], BASE_ENV, tree.root)
    assert supervisor.stop_all() == []
    signalled = _record_group_signals(monkeypatch)

    assert supervisor.stop_all() == []
    assert child.pgid not in signalled
    assert end_leaked_groups() == []


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


def test_a_group_that_holds_only_an_ended_process_is_empty(tree: Tree) -> None:
    """A process that ended stays in its group until its parent reaps it,
    and a signal to the group still finds the group. Such a process runs
    nothing. The teardown does not wait for it and does not report it."""
    mark = tree.root / "holder.pids"
    keeper = Supervisor(tree.proc_logs)
    keeper.spawn("leader", [sys.executable, "-c", ENDED_MEMBER, str(mark)], BASE_ENV, tree.root)
    holder, ended = (int(pid) for pid in _read_when_written(mark).split())

    try:
        problems = keeper.stop_all(grace_s=MARK_DEADLINE_S)
    finally:
        kill_pid(holder)

    assert problems == []
    assert pids_gone_by([holder, ended], time.monotonic() + MARK_DEADLINE_S) == []


def test_a_child_that_ended_and_was_not_reaped_is_not_alive() -> None:
    """A signal finds a pid that ended until its parent reaps it. The wait
    for a stand-in asks about one pid, so it must not count such a pid."""
    unreaped = subprocess.Popen([SH, "-c", "exit 0"])

    try:
        assert pids_gone_by([unreaped.pid], time.monotonic() + MARK_DEADLINE_S) == []
    finally:
        unreaped.wait()


@pytest.mark.parametrize(
    ("states", "gone"),
    [
        ([ZOMBIE], True),
        ([ZOMBIE, ZOMBIE], True),
        ([ZOMBIE, SLEEPS], False),
        ([RUNS], False),
    ],
    ids=["one ended", "two ended", "one of two runs", "one runs"],
)
def test_a_group_is_gone_when_each_process_of_it_ended(
    tree: Tree,
    supervisor: Supervisor,
    monkeypatch: pytest.MonkeyPatch,
    states: list[str],
    gone: bool,
) -> None:
    """The first look at a group, with the answers that Linux gives: the
    signal finds the group, and `/proc` holds the state of each process."""
    child = supervisor.spawn("one-shot", [SH, "-c", "exit 0"], BASE_ENV, tree.root)
    proc = _fake_proc(tree, monkeypatch)
    for number, state in enumerate(states):
        _write_stat(proc, OTHER_PID + number, state, pgid=child.pgid)
    _write_stat(proc, OTHER_PID + len(states), SLEEPS, pgid=child.pgid + 1)
    monkeypatch.setattr(proc_harness.os, "killpg", _group_answers)

    assert child.wait(MARK_DEADLINE_S) == 0
    assert child.group_gone is gone
    child.close_group()


def test_a_group_that_proc_does_not_list_counts_as_alive(
    tree: Tree, supervisor: Supervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The signal found the group, and `/proc` shows no process of it: the
    last one left between the two looks, or this `/proc` hides it. The
    signal is the answer then. A group that can run is never called empty."""
    child = supervisor.spawn("one-shot", [SH, "-c", "exit 0"], BASE_ENV, tree.root)
    proc = _fake_proc(tree, monkeypatch)
    _write_stat(proc, OTHER_PID, ZOMBIE, pgid=child.pgid + 1)
    (proc / "self").mkdir()
    (proc / str(OTHER_PID + 1)).mkdir()
    monkeypatch.setattr(proc_harness.os, "killpg", _group_answers)

    assert child.wait(MARK_DEADLINE_S) == 0
    assert child.group_gone is False
    child.close_group()


def test_an_ended_first_thread_beside_one_that_runs_is_alive(
    tree: Tree, supervisor: Supervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first thread of a program can end while another thread runs.
    Linux then gives the process the state `Z`, and the program runs."""
    child = supervisor.spawn("one-shot", [SH, "-c", "exit 0"], BASE_ENV, tree.root)
    proc = _fake_proc(tree, monkeypatch)
    _write_stat(proc, OTHER_PID, ZOMBIE, pgid=child.pgid, threads=3)
    monkeypatch.setattr(proc_harness.os, "killpg", _group_answers)

    assert child.wait(MARK_DEADLINE_S) == 0
    assert child.group_gone is False
    child.close_group()


@pytest.mark.parametrize(("state", "alive"), [(ZOMBIE, False), (SLEEPS, True), (RUNS, True)])
def test_the_state_in_proc_says_whether_a_pid_is_alive(
    tree: Tree, monkeypatch: pytest.MonkeyPatch, state: str, alive: bool
) -> None:
    """One pid, with the answers that Linux gives: the signal finds the pid,
    and `/proc` holds its state."""
    proc = _fake_proc(tree, monkeypatch)
    _write_stat(proc, os.getpid(), state, pgid=os.getpgrp())

    assert pid_is_alive(os.getpid()) is alive


def test_a_pid_is_alive_where_no_proc_exists(tree: Tree, monkeypatch: pytest.MonkeyPatch) -> None:
    """macOS has no `/proc`. A pid that has a process group runs."""
    monkeypatch.setattr(proc_harness, "_PROC_DIR", tree.root / "no-proc")

    assert pid_is_alive(os.getpid())


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


def _record_group_signals(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Every group that gets a real signal from now on. Signal 0 only asks."""
    signalled: list[int] = []
    real = os.killpg

    def recording(pgid: int, signum: int) -> None:
        if signum != 0:
            signalled.append(pgid)

        real(pgid, signum)

    monkeypatch.setattr(proc_harness.os, "killpg", recording)

    return signalled


def _group_answers(pgid: int, signum: int) -> None:
    """`os.killpg` as Linux answers it for a group of ended processes."""


def _fake_proc(tree: Tree, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty directory in the place of `/proc`, for the rest of the test."""
    proc = tree.root / "proc"
    proc.mkdir()
    monkeypatch.setattr(proc_harness, "_PROC_DIR", proc)

    return proc


def _write_stat(proc: Path, pid: int, state: str, *, pgid: int, threads: int = 1) -> None:
    """The `stat` file of one process, as Linux writes it."""
    (proc / str(pid)).mkdir()
    line = STAT_LINE.format(
        pid=pid, state=state, pgid=pgid, session=pgid + SESSION_OFFSET, threads=threads
    )
    (proc / str(pid) / "stat").write_text(line, encoding="utf-8")


def _read_when_written(path: Path) -> str:
    """The content of a file a child writes at start, once it is there."""
    deadline = time.monotonic() + MARK_DEADLINE_S

    while time.monotonic() < deadline:
        if path.exists() and (text := path.read_text(encoding="utf-8").strip()):
            return text

        time.sleep(0.01)

    raise AssertionError(f"{path} was never written")
