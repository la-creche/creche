"""Fixtures of the process-level suite: one root and its processes per test.

Every test here starts real processes and is marked `slow` for that reason.
The mark is applied once, here, so no scenario can forget it.

Four rules are enforced here and not left to a test:

1. Every process of a test ends with the test. A teardown that had to kill
   one fails the test.
2. A failed test carries the stdout and the stderr of every process it
   started, and every playpen log. A failed teardown carries them in its
   own text, because the root is gone when pytest makes that report.
3. The session ends with no process left. `no_process_left` is that check.
4. A run says which command it judged. A variable with the suite's prefix
   that the suite does not read stops the run, and each run prints the
   command of each service before its result line.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable, Generator, Iterator
from pathlib import Path
from typing import NoReturn, cast

import httpx
import pytest
from proc_board import BoardStack
from proc_delegate import DelegateStack
from proc_harness import Supervisor, end_leaked_groups
from proc_owui import OwuiStack
from proc_report import describe, end_processes
from proc_services import KEEP_ROOTS_ENV, NO_SKIP_ENV, describe_table, unknown_variables
from proc_tree import Tree, make_root, playpen_bundle, remove_root, socket_path_fits
from proc_trigger import TriggerStack

_HERE = Path(__file__).resolve().parent
_BUILD_HINT = "run `pnpm install && pnpm run build` in playpen/ first"

#: What a failed test adds to its report. One callable per test.
_REPORT = pytest.StashKey[Callable[[], str]]()

#: The phase of a test in which pytest ends its fixtures.
_TEARDOWN = "teardown"


def pytest_configure() -> None:
    """Fail closed on a variable that the suite does not read.

    A misspelled override starts the default command. Every test would then
    pass, and the run would judge the wrong program.
    """
    unknown = unknown_variables()

    if unknown:
        raise pytest.UsageError(
            f"the process suite reads no variable named {', '.join(unknown)}. "
            "Correct the name or unset it. `integration/proc/AGENTS.md` lists every variable."
        )


def pytest_report_header() -> list[str]:
    """Say which command each service runs, so a reader knows what was judged."""
    return describe_table()


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    """Say it again before the result line. `-q` hides the header of a run."""
    for line in describe_table():
        terminalreporter.write_line(line)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Contract: `uv run pytest integration/proc -m slow` runs the suite."""
    for item in items:
        if _HERE in item.path.parents:
            item.add_marker(pytest.mark.slow)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """Put the output of every process into the report of a failed test.

    Not at the teardown: the root is gone then. The `supervisor` fixture
    puts the output into the text of a failed teardown itself.
    """
    report = yield
    output = item.stash.get(_REPORT, None)

    if report.failed and output is not None and call.when != _TEARDOWN:
        report.sections.append((f"processes at {call.when}", output()))

    return report


@pytest.fixture(scope="session")
def bundle() -> Path:
    """The built playpen. A test that needs it and has none skips.

    Skip rather than fail. A silent pass would be worse than either.
    """
    path = playpen_bundle()

    if not path.exists():
        _skip(f"{path} is missing: {_BUILD_HINT}")

    return path


@pytest.fixture(scope="session", autouse=True)
def no_process_left() -> Iterator[None]:
    """The check at session end: no test left a process behind."""
    yield

    leaked = end_leaked_groups()

    if leaked:
        pytest.fail("a test left a process behind:\n" + "\n".join(leaked))


@pytest.fixture
def tree() -> Iterator[Tree]:
    """The temporary root of one test. Nothing of the test is outside it."""
    root = make_root()
    built = Tree(root)

    if not socket_path_fits(built):
        remove_root(root)
        _skip(f"this platform cannot bind a Unix socket path as long as {root}/sock")

    yield built

    if not os.environ.get(KEEP_ROOTS_ENV):
        remove_root(root)


@pytest.fixture
def supervisor(tree: Tree, request: pytest.FixtureRequest) -> Iterator[Supervisor]:
    """Every process of one test, ended as a whole when the test ends."""
    built = Supervisor(tree.proc_logs)
    item = cast("pytest.Item", request.node)  # pyright: ignore[reportUnknownMemberType]
    item.stash[_REPORT] = lambda: describe(tree, built)

    yield built

    failure = end_processes(tree, built)

    if failure is not None:
        pytest.fail(failure)


@pytest.fixture
def owui_prepared(tree: Tree, supervisor: Supervisor) -> OwuiStack:
    """The first topology on disk, with no service started."""
    stack = OwuiStack(tree, supervisor)
    stack.prepare()

    return stack


@pytest.fixture
def owui(owui_prepared: OwuiStack, bundle: Path) -> OwuiStack:
    """The first topology, serving. The `supervisor` fixture ends it."""
    owui_prepared.start()

    return owui_prepared


@pytest.fixture
async def door(owui: OwuiStack) -> AsyncIterator[httpx.AsyncClient]:
    """A client that plays Open WebUI against the door."""
    async with owui.door_client() as client:
        yield client


@pytest.fixture
async def attendance_api(owui: OwuiStack) -> AsyncIterator[httpx.AsyncClient]:
    """A client to `attendance`, with the token the Open WebUI door holds."""
    async with owui.attendance_client() as client:
        yield client


@pytest.fixture
def delegate(tree: Tree, supervisor: Supervisor, bundle: Path) -> DelegateStack:
    """The second topology, serving. The `supervisor` fixture ends it."""
    stack = DelegateStack(tree, supervisor)
    stack.prepare()
    stack.start()

    return stack


@pytest.fixture
async def sandbox(delegate: DelegateStack) -> AsyncIterator[httpx.AsyncClient]:
    """A client that plays the sandbox of the caller family at the chaperone."""
    async with delegate.sandbox_client() as client:
        yield client


@pytest.fixture
def board_prepared(tree: Tree, supervisor: Supervisor) -> BoardStack:
    """The fourth topology on disk, with no service started."""
    stack = BoardStack(tree, supervisor)
    stack.prepare()

    return stack


@pytest.fixture
def board(board_prepared: BoardStack, bundle: Path) -> BoardStack:
    """The fourth topology, serving. The `supervisor` fixture ends it."""
    board_prepared.start()

    return board_prepared


@pytest.fixture
def board_alone(board_prepared: BoardStack) -> BoardStack:
    """The noticeboard with no `attendance` beside it. It needs no bundle."""
    board_prepared.start_board()

    return board_prepared


@pytest.fixture
def trigger_prepared(tree: Tree, supervisor: Supervisor) -> TriggerStack:
    """The third topology on disk, with no service started."""
    stack = TriggerStack(tree, supervisor)
    stack.prepare()

    return stack


@pytest.fixture
def timer(trigger_prepared: TriggerStack, bundle: Path) -> TriggerStack:
    """`attendance` alone. A test runs the timer command of the trigger door."""
    trigger_prepared.spawn_attendance()
    trigger_prepared.await_attendance()

    return trigger_prepared


@pytest.fixture
def trigger(trigger_prepared: TriggerStack, bundle: Path) -> TriggerStack:
    """The third topology, serving: `attendance` and the webhook listener."""
    trigger_prepared.start()

    return trigger_prepared


def _skip(reason: str) -> NoReturn:
    """Skip a test that cannot run here. Fail it when the run forbids a skip."""
    if os.environ.get(NO_SKIP_ENV):
        pytest.fail(f"{reason} ({NO_SKIP_ENV} is set, so a skip is a failure)")

    pytest.skip(reason)
