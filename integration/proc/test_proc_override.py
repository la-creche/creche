"""The override variables, proved on one whole topology.

A later packet points `CRECHE_PROC_ATTENDANCE` at another binary and runs
this suite with no test change. This test is the proof that the variable
reaches the process a scenario talks to: each service starts through a
program that the command of the run never runs by itself.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest
from proc_chat import chat_id, run_stream
from proc_harness import Supervisor
from proc_owui import OwuiStack
from proc_services import SERVICES, Service, command_of
from proc_tree import Tree


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
