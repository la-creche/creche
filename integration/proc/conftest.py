"""Fixtures of the process-level suite: one root and its processes per test.

Every test here starts real processes and is marked `slow` for that reason.
The mark is applied once, here, so no scenario can forget it.

Three rules are enforced here and not left to a test:

1. Every process of a test ends with the test. A teardown that had to kill
   one fails the test.
2. A failed test carries the stdout and the stderr of every process it
   started, and every playpen log.
3. The session ends with no process left. `no_process_left` is that check.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable, Generator, Iterator
from pathlib import Path
from typing import cast

import httpx
import pytest
from proc_delegate import DelegateStack
from proc_harness import Supervisor, end_leaked_groups
from proc_owui import OwuiStack
from proc_services import describe_table
from proc_standins import end_standins
from proc_tree import Tree, make_root, playpen_bundle, remove_root, socket_path_fits

_HERE = Path(__file__).resolve().parent
_BUILD_HINT = "run `pnpm install && pnpm run build` in playpen/ first"

#: Set it to keep the root of every test on disk, to read after a run.
KEEP_ROOTS_ENV = "CRECHE_PROC_KEEP"

#: What a failed test adds to its report. One callable per test.
_REPORT = pytest.StashKey[Callable[[], str]]()


def pytest_report_header() -> list[str]:
    """Say which command each service runs, so a reader knows what was judged."""
    return describe_table()


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Contract: `uv run pytest integration/proc -m slow` runs the suite."""
    for item in items:
        if _HERE in item.path.parents:
            item.add_marker(pytest.mark.slow)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """Put the output of every process into the report of a failed test."""
    report = yield
    describe = item.stash.get(_REPORT, None)

    if report.failed and describe is not None:
        report.sections.append((f"processes at {call.when}", describe()))

    return report


@pytest.fixture(scope="session")
def bundle() -> Path:
    """The built playpen. A test that needs it and has none skips.

    Skip rather than fail. A silent pass would be worse than either.
    """
    path = playpen_bundle()

    if not path.exists():
        pytest.skip(f"{path} is missing: {_BUILD_HINT}")

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
        pytest.skip(f"this platform cannot bind a Unix socket path as long as {root}/sock")

    yield built

    if not os.environ.get(KEEP_ROOTS_ENV):
        remove_root(root)


@pytest.fixture
def supervisor(tree: Tree, request: pytest.FixtureRequest) -> Iterator[Supervisor]:
    """Every process of one test, ended as a whole when the test ends."""
    built = Supervisor(tree.proc_logs)
    item = cast("pytest.Item", request.node)  # pyright: ignore[reportUnknownMemberType]
    item.stash[_REPORT] = lambda: _describe(tree, built)

    yield built

    problems = built.stop_all() + end_standins(tree)

    if problems:
        pytest.fail("the teardown had to end a process:\n" + "\n".join(problems))


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


def _describe(tree: Tree, supervisor: Supervisor) -> str:
    """The root, what each process wrote, and what each playpen wrote."""
    parts = [f"root: {tree.root} (set {KEEP_ROOTS_ENV}=1 to keep it)", supervisor.output()]

    if tree.log_dir.is_dir():
        for log in sorted(tree.log_dir.iterdir()):
            text = log.read_text(encoding="utf-8", errors="replace")
            parts.append(f"--- {log.name} ---\n{text}")

    return "\n".join(parts)
