"""A test run that starts with SIGINT ignored passes the same tests.

A shell with no job control starts a background job with SIGINT ignored, and
pytest inherits that. A service that runs in the test process takes SIGINT in
its asyncio loop. When the loop closes, asyncio puts the default handler of
Python back, not the handler that it found. The root `conftest.py` fails a
test that leaves a signal changed, so each such test failed in a run from a
background job and passed in a run from a terminal.

The root `conftest.py` now gives SIGINT the default handler of Python when
pytest imports it, if the run started with SIGINT ignored.

Each test here starts pytest as a child with SIGINT ignored.
"""

from __future__ import annotations

import asyncio
import signal
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

#: A test that runs a service in the test process, with the signal handlers
#: of that service. When this id moves, name another test of that kind.
SERVICE_TEST = (
    "attendance/tests/test_main.py::test_serve_binds_the_socket_at_0660_under_a_setgid_dir"
)

#: The two probes below, as the child names them.
IMPORT_PROBE = "bin/tests/test_sigint_default.py::test_sigint_is_not_ignored_at_import"
LOOP_PROBE = "bin/tests/test_sigint_default.py::test_a_loop_that_takes_sigint_leaves_no_change"

#: What the result line of a child holds when it ran exactly one test, and
#: what it holds when a fixture failed after the test.
ONE_PASSED = "1 passed"
AN_ERROR = "error"

#: A shell that ignores SIGINT, then becomes the command in its arguments.
#: The command inherits the ignored signal, as a background job does.
IGNORE_SIGINT = ["sh", "-c", 'trap "" INT; exec "$@"', "sh"]

#: How this process took SIGINT when this module was imported: before any
#: fixture ran.
AT_IMPORT = signal.getsignal(signal.SIGINT)


def _pytest_with_sigint_ignored(tmp_path: Path, test: str) -> subprocess.CompletedProcess[str]:
    """Runs one test of this repository in a child pytest that starts with
    SIGINT ignored. The child keeps its temporary files under `tmp_path`."""
    return subprocess.run(
        [
            *IGNORE_SIGINT,
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
        capture_output=True,
        text=True,
        timeout=300,
    )


def _passed_alone(done: subprocess.CompletedProcess[str]) -> bool:
    """Whether the child ran one test, and no fixture failed after it."""
    last = done.stdout.strip().splitlines()[-1] if done.stdout.strip() else ""

    return done.returncode == 0 and ONE_PASSED in last and AN_ERROR not in last


def test_a_test_that_runs_a_service_passes_with_sigint_ignored(tmp_path: Path) -> None:
    """The service takes SIGINT in its loop. The test must pass, and the
    fixture that compares the signal handlers must find no change."""
    done = _pytest_with_sigint_ignored(tmp_path, SERVICE_TEST)

    assert _passed_alone(done), done.stdout + done.stderr


def test_a_loop_may_take_sigint_in_a_run_with_sigint_ignored(tmp_path: Path) -> None:
    """The same, with no service: only a loop that takes the signal."""
    done = _pytest_with_sigint_ignored(tmp_path, LOOP_PROBE)

    assert _passed_alone(done), done.stdout + done.stderr


def test_pytest_takes_sigint_before_it_imports_a_test(tmp_path: Path) -> None:
    """At the import of a test module: a fixture of any scope comes after that."""
    done = _pytest_with_sigint_ignored(tmp_path, IMPORT_PROBE)

    assert _passed_alone(done), done.stdout + done.stderr


def test_sigint_is_not_ignored_at_import() -> None:
    """The first probe. In a run that started with SIGINT at its default it
    has nothing to find."""
    assert AT_IMPORT is not signal.SIG_IGN


async def test_a_loop_that_takes_sigint_leaves_no_change() -> None:
    """The second probe: what a service does when it starts in the test
    process. The fixture of the root `conftest.py` judges it after the loop
    closes."""
    asyncio.get_running_loop().add_signal_handler(signal.SIGINT, lambda: None)
