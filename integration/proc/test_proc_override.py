"""The override variables, proved on each topology that a port replaces.

A later packet points `CRECHE_PROC_ATTENDANCE` at another binary and runs
this suite with no test change. Each test here is the proof that the
variable reaches the process a scenario talks to: each service starts
through a program that the command of the run never runs by itself.

A service has three ways to start in this suite: as a process that serves,
as a command that runs to its end, and as a program on a terminal. Each way
reads the same table, and each has its proof here.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import httpx
import pytest
from proc_board import BoardStack
from proc_chat import chat_id, run_stream
from proc_harness import Supervisor
from proc_owui import OwuiStack
from proc_services import SERVICES, Service, command_of
from proc_tree import Tree
from proc_trigger import REVIEW, TriggerStack, hook_path
from proc_tui import TuiStack

CHECK_FLAG = "--check"
EXIT_DEADLINE_S = 30.0


async def test_an_override_starts_each_service_of_a_topology(
    tree: Tree, supervisor: Supervisor, bundle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran = tree.root / "override-ran"

    for service in (Service.ATTENDANCE, Service.DOOR_OWUI):
        program = _other_program(tree, service, ran)
        monkeypatch.setenv(SERVICES[service].override, str(program))

    stack = OwuiStack(tree, supervisor)
    stack.prepare()
    stack.start()

    async with stack.door_client() as door:
        frames = await run_stream(door, chat_id(), "hello")

    assert frames.ends_with_done
    assert frames.error_chunks == []
    assert set(ran.read_text(encoding="utf-8").split()) == {"attendance", "door-owui"}


async def test_an_override_starts_the_trigger_door_in_both_ways(
    tree: Tree, supervisor: Supervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The listener is a process that serves. The timer command runs to its end."""
    ran = tree.root / "override-ran"
    monkeypatch.setenv(
        SERVICES[Service.DOOR_TRIGGER].override,
        str(_other_program(tree, Service.DOOR_TRIGGER, ran)),
    )
    stack = TriggerStack(tree, supervisor)
    stack.prepare()
    stack.start_listener()

    async with stack.automation() as automation:
        answered = await automation.post(hook_path(name="no-such-hook"))

    checked = stack.fire(REVIEW, CHECK_FLAG)

    assert answered.status_code == httpx.codes.NOT_FOUND
    assert checked.exit_code == 0, checked.stderr
    assert ran.read_text(encoding="utf-8").split() == ["door-trigger", "door-trigger"]


async def test_an_override_starts_the_noticeboard(
    tree: Tree, supervisor: Supervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran = tree.root / "override-ran"
    monkeypatch.setenv(
        SERVICES[Service.NOTICEBOARD].override,
        str(_other_program(tree, Service.NOTICEBOARD, ran)),
    )
    stack = BoardStack(tree, supervisor)
    stack.prepare()
    stack.start_board()

    async with stack.client() as browser:
        health = await browser.get("/healthz")

    assert health.status_code == httpx.codes.OK
    assert ran.read_text(encoding="utf-8").split() == ["noticeboard"]


def test_an_override_starts_the_verify_hook_of_the_noticeboard(
    tree: Tree, supervisor: Supervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hook is a program of its own, with a variable of its own. It runs to its end.

    The hook looks for the audit directory, which the host has before a
    service starts. One audit record makes it here.
    """
    ran = tree.root / "override-ran"
    monkeypatch.setenv(
        SERVICES[Service.NOTICEBOARD_VERIFY].override,
        str(_other_program(tree, Service.NOTICEBOARD_VERIFY, ran)),
    )
    stack = BoardStack(tree, supervisor)
    stack.prepare()
    stack.write_audit_record()
    stack.start_board()

    checked = stack.verify(stack.write_view_env())

    assert checked.exit_code == 0, checked.stdout + checked.stderr
    assert ran.read_text(encoding="utf-8").split() == ["noticeboard-verify"]


def test_an_override_starts_the_terminal_door_on_a_terminal(
    tree: Tree, supervisor: Supervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran = tree.root / "override-ran"
    monkeypatch.setenv(
        SERVICES[Service.DOOR_TUI].override, str(_other_program(tree, Service.DOOR_TUI, ran))
    )
    stack = TuiStack(tree, supervisor)
    stack.prepare()

    child, _ = stack.open_terminal(CHECK_FLAG)

    assert child.wait(EXIT_DEADLINE_S) == 0, child.output()
    assert ran.read_text(encoding="utf-8").split() == ["door-tui"]


def _other_program(tree: Tree, service: Service, ran: Path) -> Path:
    """Another program for one service: it records its start, then serves.

    It becomes the command that this run gives the service: the default, or
    the binary that the variable of the service names. So the test starts
    no program that the run did not choose, and it proves the same thing for
    each. What the test proves is which program was started.
    """
    current = shlex.join(command_of(service).words)
    path = tree.root / f"other-{service.value}"
    path.write_text(
        f'#!/bin/sh\necho {service.value} >> {shlex.quote(str(ran))}\nexec {current} "$@"\n',
        encoding="utf-8",
    )
    path.chmod(0o755)

    return path
