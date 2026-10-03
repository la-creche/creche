"""Packet CT: the TUI door against the real `sessiond`, off the host.

Invariant 3 says "one session, every UI". The stage 1 gate proves the Open
WebUI half. This proves the other half meets it on the SAME session:

    agent-tui chat --session owui-<chat id>

The door, `sessiond`, the playpen and the status document are all real.
Two stand-ins, both the gate's own: `fake_sbx.py` for `sbx exec`, and
`fake-pi.mjs` behind the playpen. Nothing dials a live service.

The TUI door runs synchronously — it makes a call, then blocks on a terminal
— so each scenario drives it in a worker thread and keeps the stack's event
loop free for `sessiond`.

Three scenarios, one per promise packet CT makes.

1. **A TUI terminal attaches to an Open WebUI chat's session**, and the
   `sbx` argv names the sandbox and the `supervisor.env` path that
   `managerd`'s status document publishes (contract 05 §4.1.1). The
   terminal needs no `--force` and no wait: contract 02 draft 6 §7.3 rule
   5 hands an idle lease to another door, because no turn is running and
   nothing is lost. Draft 5 refused it for the 60 seconds after a chat
   turn settled, which is the refusal this scenario used to pin.
2. **A second terminal on the same session is refused** while the first
   holds the writer lease. Contract 02 §7.2: contention refuses, it never
   queues and never steals. Two pi writers on one session store
   cross-contaminate context and both report success.
3. **A new session is created through `sessiond` first**, so the session
   exists where sessions are owned before pi writes a byte, and the
   launcher is told this terminal is its first writer.
"""

from __future__ import annotations

import asyncio
import stat
from collections.abc import Callable
from pathlib import Path

import pytest
from agent_door_tui.app import Request, TuiDoor, Want
from agent_door_tui.config import TuiConfig
from agent_door_tui.errors import DoorError, Exit
from agent_door_tui.launch import LAUNCHER_ARG_NEW, Terminal
from agent_door_tui.picker import ScriptedTerminal
from agent_door_tui.sessiond import HttpSessiond
from agent_door_tui.status import StatusFiles
from conftest import chat_body, chat_id, message_id, owui_headers, session_of
from stack import FAMILY, SANDBOX, Stack

CHAT_PATH = "/v1/chat/completions"
HTTP_OK = 200

LAUNCHER = "/opt/agent-supervisor/agent-pi-launch.js"
INSTANCE = "tui.1"
SECOND_INSTANCE = "tui.2"


def fake_sbx(root: Path) -> tuple[Path, Path]:
    """An `sbx` that records its argv and exits 0.

    The real one would boot a VM and run pi's interactive UI in it. What
    this scenario has to prove is the argv, and packet CV already proves the
    launcher itself against the built bundle.
    """
    log = root / "ct-sbx-argv.txt"
    path = root / "ct-sbx"
    path.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$@" >> "{log}"\nexit 0\n',
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)

    return path, log


def recorded(log: Path) -> list[str]:
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def tui_config(stack: Stack, sbx: Path, instance: str) -> TuiConfig:
    """The door's config, pointed at the stack's own `sessiond` and status."""
    socket = stack.sessiond_socket
    assert socket is not None, "the stack fixture always serves before it yields"
    token = (stack.state_root / "tokens" / "door-tui.token").read_text(encoding="utf-8")

    return TuiConfig(
        sessiond_token=token.strip(),
        sessiond_url="http://sessiond",
        sessiond_socket=socket,
        families_dir=stack.families_dir,
        sbx=str(sbx),
        pi_launch=LAUNCHER,
        door_instance=instance,
    )


def build_door(
    stack: Stack, sbx: Path, instance: str, answers: list[str] | None = None
) -> tuple[TuiDoor, HttpSessiond, ScriptedTerminal]:
    config = tui_config(stack, sbx, instance)
    client = HttpSessiond(config)
    screen = ScriptedTerminal(answers if answers is not None else [])
    door = TuiDoor(
        client,
        StatusFiles(config.families_dir),
        screen,
        Terminal(),
        config.door_instance,
        config.sbx,
        config.pi_launch,
    )

    return door, client, screen


async def run_door(door: TuiDoor, request: Request) -> int:
    """Drive the synchronous door without blocking the stack's event loop."""
    return await asyncio.to_thread(door.run, request)


async def run_one_turn(stack: Stack, chat: str) -> None:
    """One non-streamed turn through the Open WebUI door, so a session exists."""
    client = stack.client
    assert client is not None, "the stack fixture always serves before it yields"

    response = await client.post(
        CHAT_PATH,
        headers=owui_headers(chat, message_id()),
        json=chat_body("hello", stream=False),
    )

    assert response.status_code == HTTP_OK, response.text


async def test_ct_tui_attaches_to_an_owui_session(stack: Stack, tmp_path: Path) -> None:
    """Invariant 3. One session, two doors, and the second is a terminal."""
    sbx, log = fake_sbx(tmp_path)
    chat = chat_id()
    session = session_of(chat)
    await run_one_turn(stack, chat)

    door, client, _ = build_door(stack, sbx, INSTANCE)
    polite = Request(FAMILY, Want.NAMED, session=session)

    try:
        # Contract 02 §7.3 rule 5. The chat's lease is idle, so the terminal
        # takes it with no `--force` and no wait. Draft 5 refused this for
        # the 60 seconds after every Open WebUI turn.
        code = await run_door(door, polite)
    finally:
        client.close()

    assert code == 0
    argv = recorded(log)
    assert argv[:3] == ["exec", "-it", "--env-file"]
    # Contract 05 §4.1.1 rule 3: the path comes from the status document, and
    # `sessiond` puts the same one on its own channel command.
    assert argv[3] == str(stack.playpen_env)
    assert argv[4] == SANDBOX
    assert argv[argv.index("--session") + 1] == session
    # The session already ran a turn, so it is not this terminal's first writer.
    assert LAUNCHER_ARG_NEW not in argv


async def test_ct_a_second_terminal_is_refused(stack: Stack, tmp_path: Path) -> None:
    """Contract 02 §7.2. Contention refuses: it never queues and never steals.

    Two terminals is the case `sessiond`'s lease is the ONLY fence for.
    Contract 03 §7.5's process record names the playpen's process, so it
    stops neither a second `agent-pi-launch` nor a `start_turn` racing one
    (§7.6, "What this does NOT fence").
    """
    sbx, _ = fake_sbx(tmp_path)
    _, first_client, _ = build_door(stack, sbx, INSTANCE)
    second, second_client, _ = build_door(stack, sbx, SECOND_INSTANCE)
    loop = asyncio.get_running_loop()
    running: asyncio.Future[str] = loop.create_future()
    holding = asyncio.Event()

    def hold_the_terminal(argv: list[str]) -> int:
        """Stand in for pi: hold the lease until the second door has tried."""
        session = argv[argv.index("--session") + 1]
        loop.call_soon_threadsafe(running.set_result, session)
        asyncio.run_coroutine_threadsafe(holding.wait(), loop).result(timeout=30)

        return 0

    first = TuiDoor(
        first_client,
        StatusFiles(stack.families_dir),
        ScriptedTerminal([]),
        _Held(hold_the_terminal),
        INSTANCE,
        str(sbx),
        LAUNCHER,
    )
    task = asyncio.create_task(run_door(first, Request(FAMILY, Want.NEW, title="first")))

    try:
        session = await asyncio.wait_for(running, timeout=30)

        with pytest.raises(DoorError) as caught:
            await run_door(second, Request(FAMILY, Want.NAMED, session=session))

        assert caught.value.code is Exit.SESSION_BUSY
        assert "tui" in caught.value.message
        # A running turn is the one case `--force` cannot help with, and
        # there is none here, so the refusal offers it.
        assert "--force" in caught.value.message
    finally:
        holding.set()
        assert await task == 0
        first_client.close()
        second_client.close()


async def test_ct_a_new_session_exists_before_pi_runs(stack: Stack, tmp_path: Path) -> None:
    """Packet CT step 2. `sessiond` creates the session before the exec, and
    the launcher is told `--new`."""
    sbx, log = fake_sbx(tmp_path)
    door, client, screen = build_door(stack, sbx, INSTANCE)

    try:
        code = await run_door(door, Request(FAMILY, Want.NEW, title="Boiler"))
    finally:
        client.close()

    assert code == 0
    argv = recorded(log)
    session = argv[argv.index("--session") + 1]
    assert session.startswith("tui-")
    # `sessiond` owns sessions, so the directory is on disk before the exec.
    assert stack.session_dir(session).exists()
    assert LAUNCHER_ARG_NEW in argv
    assert any(session in line for line in screen.shown)

    kinds = [line["kind"] for line in stack.journal_lines(session)]
    assert kinds[0] == "session_created"
    assert "writer_changed" in kinds


class _Held:
    """A `TerminalRunner` that runs a callback instead of `sbx`."""

    def __init__(self, run: Callable[[list[str]], int]) -> None:
        self._run = run

    def run(self, argv: list[str]) -> int:
        return self._run(argv)
