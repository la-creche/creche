"""A git child root cannot start fails one step, not the drain pass.

`drain.root_readers` reads a component's input digest out of the corpus
through root's one child starter. `host.run` turns a timeout into a
`Result` and nothing else: a clone re-made under root's feet (ENOENT), or
no file descriptor left (EMFILE), raises `OSError` out of the child start.
Every other child the executor starts goes through `Installer._run`, which
makes that a `StepFailed`. The digest reader must do the same, or one
request's bad moment ends the whole pass with a traceback and leaves every
request behind it unread.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from handover.executor.drain import root_readers
from handover.executor.host import Command
from handover.executor.install import StepFailed
from handover_executor_fixtures import FakeRun, RunResult, fake_host

SOME_SHA = "0123456789abcdef0123456789abcdef01234567"


def _git_cannot_start(command: Command) -> RunResult | None:
    if "git" in Path(command.argv[0]).name:
        raise FileNotFoundError(command.argv[0])

    return None


def test_a_git_child_that_cannot_start_fails_the_step(tmp_path: Path) -> None:
    run = FakeRun(dynamic=_git_cannot_start)
    readers = root_readers(fake_host(tmp_path, run), api=None)

    with pytest.raises(StepFailed) as raised:
        readers.input_digest("chaperone", SOME_SHA)

    assert "FileNotFoundError" in raised.value.detail


def test_a_git_child_that_exits_nonzero_is_no_digest(tmp_path: Path) -> None:
    """The existing answer for a clone that lacks the commit stays: None,
    which step 2 notes and the fetch refuses with the command that fixes it."""
    run = FakeRun(fails={"cat-file": 1})
    readers = root_readers(fake_host(tmp_path, run), api=None)

    assert readers.input_digest("chaperone", SOME_SHA) is None
