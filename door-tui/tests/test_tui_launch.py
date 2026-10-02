"""The `sbx exec` that hands the terminal to pi.

Contract 03 §7.6 fixes the command. This file pins its argv word for word,
against a fake `sbx` on PATH that records what it was given and exits with a
code the test chooses.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from agent_door_tui.launch import (
    LAUNCHER_ARG_NEW,
    Store,
    Terminal,
    launch_argv,
)

SANDBOX = "chat-s1"
SESSION = "tui-01JBQ7WZ0X4T9V6K2H8M3N5PQR"
ENV_FILE = "/srv/agents/state/rework/families/chat/supervisor.env"
LAUNCHER = "/opt/agent-supervisor/agent-pi-launch.js"


def test_the_argv_is_contract_03_7_6s_command() -> None:
    argv = launch_argv(
        sbx="sbx",
        supervisor_env=ENV_FILE,
        sandbox=SANDBOX,
        session=SESSION,
        launcher=LAUNCHER,
        store=Store.EXISTING,
    )

    assert argv == [
        "sbx",
        "exec",
        "-it",
        "--env-file",
        ENV_FILE,
        SANDBOX,
        "--",
        "node",
        LAUNCHER,
        "--sandbox",
        SANDBOX,
        "--session",
        SESSION,
    ]


def test_a_session_with_no_store_yet_passes_new() -> None:
    """§7.6: without `--new` the launcher exits 9 when no store exists."""
    argv = launch_argv(
        sbx="sbx",
        supervisor_env=ENV_FILE,
        sandbox=SANDBOX,
        session=SESSION,
        launcher=LAUNCHER,
        store=Store.NONE_YET,
    )

    assert argv[-1] == LAUNCHER_ARG_NEW
    assert argv[:7] == ["sbx", "exec", "-it", "--env-file", ENV_FILE, SANDBOX, "--"]


def test_no_secret_rides_on_the_argv() -> None:
    """Invariant 13. Every word is a path or an identifier."""
    argv = launch_argv(
        sbx="sbx",
        supervisor_env=ENV_FILE,
        sandbox=SANDBOX,
        session=SESSION,
        launcher=LAUNCHER,
        store=Store.NONE_YET,
    )

    assert not [word for word in argv if "token" in word.lower() or "key" in word.lower()]


def fake_sbx(root: Path, exit_code: int) -> Path:
    """An `sbx` that records its argv and exits with the code a test picks."""
    log = root / "sbx-argv.txt"
    path = root / "sbx"
    path.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$@" >> "{log}"\nexit {exit_code}\n',
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)

    return path


def recorded(root: Path) -> list[str]:
    log = root / "sbx-argv.txt"

    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def test_the_terminal_runs_sbx_and_returns_its_code(tmp_path: Path) -> None:
    program = fake_sbx(tmp_path, 7)
    argv = launch_argv(
        sbx=str(program),
        supervisor_env=ENV_FILE,
        sandbox=SANDBOX,
        session=SESSION,
        launcher=LAUNCHER,
        store=Store.EXISTING,
    )

    code = Terminal().run(argv)

    assert code == 7
    assert recorded(tmp_path)[:3] == ["exec", "-it", "--env-file"]
    assert SESSION in recorded(tmp_path)


def test_pis_own_exit_code_passes_through(tmp_path: Path) -> None:
    argv = launch_argv(
        sbx=str(fake_sbx(tmp_path, 0)),
        supervisor_env=ENV_FILE,
        sandbox=SANDBOX,
        session=SESSION,
        launcher=LAUNCHER,
        store=Store.EXISTING,
    )

    assert Terminal().run(argv) == 0


def test_an_sbx_that_is_not_on_path_says_so(tmp_path: Path) -> None:
    argv = launch_argv(
        sbx=str(tmp_path / "no-such-sbx"),
        supervisor_env=ENV_FILE,
        sandbox=SANDBOX,
        session=SESSION,
        launcher=LAUNCHER,
        store=Store.EXISTING,
    )

    code = Terminal().run(argv)

    assert code != 0


def test_a_path_injected_sbx_is_what_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The fake resolves through PATH exactly as the real one would."""
    fake_sbx(tmp_path, 3)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    argv = launch_argv(
        sbx="sbx",
        supervisor_env=ENV_FILE,
        sandbox=SANDBOX,
        session=SESSION,
        launcher=LAUNCHER,
        store=Store.EXISTING,
    )

    assert Terminal().run(argv) == 3
    assert recorded(tmp_path)[-1] == SESSION
