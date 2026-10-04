"""A test run that starts with a signal ignored passes the same tests.

A shell with no job control starts a background job with SIGINT ignored, and
`nohup` starts a command with SIGHUP ignored. pytest inherits that. A service
that runs in the test process takes signals in its asyncio loop. When the
loop closes, asyncio cannot put an ignored signal back. It writes the default
handler of Python for SIGINT, and the default action for each other signal.
The root `conftest.py` fails a test that leaves a signal changed, so each
such test failed in such a run and passed in a run from a terminal.

The fixture of the root `conftest.py` now ignores the signal again, and does
not fail the test for that one change. It fails each other change as before.

Each test here starts pytest as a child with one signal ignored.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

#: A test that runs a service in the test process, with the signal handlers
#: of that service. When this id moves, name another test of that kind.
SERVICE_TEST = (
    "attendance/tests/test_main.py::test_serve_binds_the_socket_at_0660_under_a_setgid_dir"
)

#: The three probes below, as the child names them.
THIS = "bin/tests/test_ignored_signal_kept.py"
LOOP_PROBE = f"{THIS}::test_a_loop_that_takes_each_signal_leaves_no_change"
KEPT_PROBE = f"{THIS}::test_the_run_still_ignores_its_signal"
LEAK_PROBE = f"{THIS}::test_a_handler_stays_when_the_variable_asks"

#: The signals that a service takes in its loop: two for a stop and one for a
#: reload. A run from a background job ignores the first, and a run under
#: `nohup` ignores the last.
TAKEN = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)

#: The same three, as `trap` of a shell names them.
TRAP_NAMES = [one.name.removeprefix("SIG") for one in TAKEN]

#: The variable that tells the second probe which signal the run ignores, and
#: the variable that tells the third probe which signal to leave changed. Each
#: holds a name such as `SIGHUP`. Only a child of these tests gets one.
IGNORED_ENV = "CRECHE_TEST_IGNORED_SIGNAL"
LEAK_ENV = "CRECHE_TEST_LEAK_SIGNAL"

#: What the result line of a child holds for each count of tests, and what it
#: holds when a fixture failed after a test.
ONE_PASSED = "1 passed"
TWO_PASSED = "2 passed"
AN_ERROR = "error"

#: What the fixture says about a signal that a test left changed.
LEFT_CHANGED = "the test left {name} changed"


def _pytest_with_one_ignored(
    tmp_path: Path, trap_name: str, tests: list[str], env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Runs tests of this repository in a child pytest that starts with one
    signal ignored.

    A shell ignores the signal, then becomes the command in its arguments.
    The command inherits the ignored signal, as a background job does. The
    child keeps its temporary files under `tmp_path`.
    """
    return subprocess.run(
        [
            "sh",
            "-c",
            f'trap "" {trap_name}; exec "$@"',
            "sh",
            sys.executable,
            "-m",
            "pytest",
            *tests,
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


def _result_line(done: subprocess.CompletedProcess[str]) -> str:
    """The last line that the child wrote: its counts."""
    return done.stdout.strip().splitlines()[-1] if done.stdout.strip() else ""


def _all_passed(done: subprocess.CompletedProcess[str], count: str) -> bool:
    """Whether the child ran `count` tests, and no fixture failed after one."""
    last = _result_line(done)

    return done.returncode == 0 and count in last and AN_ERROR not in last


@pytest.mark.parametrize("trap_name", TRAP_NAMES)
def test_a_test_that_runs_a_service_passes_with_a_signal_ignored(
    tmp_path: Path, trap_name: str
) -> None:
    """The service takes the signal in its loop. The test must pass, and the
    fixture that compares the signal handlers must not fail it."""
    done = _pytest_with_one_ignored(tmp_path, trap_name, [SERVICE_TEST], {})

    assert _all_passed(done, ONE_PASSED), done.stdout + done.stderr


@pytest.mark.parametrize("trap_name", TRAP_NAMES)
def test_a_loop_may_take_a_signal_that_the_run_ignores(tmp_path: Path, trap_name: str) -> None:
    """The same, with no service: only a loop that takes the signal. The test
    that comes after it must find the signal ignored again."""
    done = _pytest_with_one_ignored(
        tmp_path, trap_name, [LOOP_PROBE, KEPT_PROBE], {IGNORED_ENV: f"SIG{trap_name}"}
    )

    assert _all_passed(done, TWO_PASSED), done.stdout + done.stderr


@pytest.mark.parametrize("trap_name", TRAP_NAMES)
def test_a_handler_that_a_test_leaves_fails_with_a_signal_ignored(
    tmp_path: Path, trap_name: str
) -> None:
    """Only the change that a loop makes passes. A test that leaves a handler
    of its own on the ignored signal fails, as in each other run."""
    name = f"SIG{trap_name}"

    done = _pytest_with_one_ignored(tmp_path, trap_name, [LEAK_PROBE], {LEAK_ENV: name})

    assert done.returncode != 0, done.stdout + done.stderr
    assert LEFT_CHANGED.format(name=name) in done.stdout, done.stdout + done.stderr
    assert AN_ERROR in _result_line(done), done.stdout + done.stderr


async def test_a_loop_that_takes_each_signal_leaves_no_change() -> None:
    """The first probe: what a service does when it starts in the test
    process. The fixture of the root `conftest.py` judges it after the loop
    closes."""
    loop = asyncio.get_running_loop()

    for one in TAKEN:
        loop.add_signal_handler(one, lambda: None)


def test_the_run_still_ignores_its_signal() -> None:
    """The second probe. In a child it runs after the first one. In a run
    with no variable it has nothing to find."""
    name = os.environ.get(IGNORED_ENV)
    if name is None:
        return

    assert signal.getsignal(signal.Signals[name]) is signal.SIG_IGN


def test_a_handler_stays_when_the_variable_asks() -> None:
    """The third probe: a test that sets a handler and does not put it back.
    In a run with no variable it changes nothing."""
    name = os.environ.get(LEAK_ENV)
    if name is None:
        return

    signal.signal(signal.Signals[name], lambda _number, _frame: None)
