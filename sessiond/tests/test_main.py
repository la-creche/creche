"""The entry point: `--check` validates and exits, and fails closed."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import stat
import tempfile
from pathlib import Path

import pytest
from agent_sessiond.__main__ import EXIT_BAD_CONFIG, EXIT_OK, _prepare_socket_dir, main, serve
from agent_sessiond.auth import Principal, TokenBook
from agent_sessiond.config import LAN_ADDRESS_ENV, from_env
from agent_sessiond.paths import token_file
from sessiond_harness import wait_until, write_tokens


def env_for(tmp_path: Path, **extra: str) -> dict[str, str]:
    values = {
        "SESSIOND_SESSIONS_ROOT": str(tmp_path / "sessions"),
        "SESSIOND_STATE_ROOT": str(tmp_path / "state"),
        "SESSIOND_WORK_ROOT": str(tmp_path / "work"),
        "SESSIOND_SOCKET": str(tmp_path / "sessiond.sock"),
        "SESSIOND_LOG_DIR": str(tmp_path / "log"),
        LAN_ADDRESS_ENV: os.environ[LAN_ADDRESS_ENV],
    }
    values.update(extra)
    return values


def use_env(monkeypatch: pytest.MonkeyPatch, values: dict[str, str]) -> None:
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def _mode_of(path: Path) -> int | None:
    """None before the path exists, so a poll loop can wait on it."""
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        return None


def test_check_passes_with_every_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_tokens(tmp_path / "state")
    use_env(monkeypatch, env_for(tmp_path))

    assert main(["--check"]) == EXIT_OK

    printed = capsys.readouterr().out
    assert "tcp           off" in printed
    assert "sessiond.sock" in printed
    assert "door-owui" not in printed


def test_check_reports_the_lan_bind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_tokens(tmp_path / "state")
    use_env(monkeypatch, env_for(tmp_path, SESSIOND_BIND_LAN="true"))

    assert main(["--check"]) == EXIT_OK
    assert f"{os.environ[LAN_ADDRESS_ENV]}:8350" in capsys.readouterr().out


def test_check_fails_closed_on_a_short_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Contract 02 §3 rule 7. The service must not start weak."""
    write_tokens(tmp_path / "state")
    token_file(tmp_path / "state", Principal.DOOR_OWUI.value).write_text("short")
    use_env(monkeypatch, env_for(tmp_path))

    assert main(["--check"]) == EXIT_BAD_CONFIG
    assert "under 32 bytes" in capsys.readouterr().err


def test_check_fails_closed_on_a_bad_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_tokens(tmp_path / "state")
    use_env(monkeypatch, env_for(tmp_path, SESSIOND_LAN_PORT="nope"))

    assert main(["--check"]) == EXIT_BAD_CONFIG


async def test_serve_binds_the_socket_at_0660_under_a_setgid_dir(tmp_path: Path) -> None:
    """Contract 02 §3 rule 1.

    `sessiond` is a systemd USER unit, so its default runtime directory sits
    under `/run/user/<uid>`, which the hardened PEP unit cannot reach. The
    socket lives on the production state root instead, in a directory this
    process makes setgid so the PEP's group can traverse it. The unit's
    umask would otherwise leave a fresh socket at 0750, which gives the
    group no write, and `connect(2)` needs write -- so `serve` sets it to
    0660 itself, right after it binds.
    """
    write_tokens(tmp_path / "state")
    # A short, independent directory: AF_UNIX cannot bind past ~104 bytes on
    # macOS, and pytest's own tmp_path (named after this test function) is
    # already close to that on its own.
    socket_dir = Path(tempfile.mkdtemp(prefix="sessiond-sock-"))
    try:
        config = from_env(env_for(tmp_path, SESSIOND_SOCKET=str(socket_dir / "sessiond.sock")))
        tokens = TokenBook(config.state_root)
        tokens.load()

        task = asyncio.create_task(serve(config, tokens))
        try:
            # The file exists the instant uvicorn binds it, before the ASGI
            # lifespan startup this override's chmod waits behind -- so the
            # condition has to be the FINAL mode, or this races the fix it
            # means to prove and passes for the wrong reason under load.
            await wait_until(lambda: _mode_of(config.socket_path) == 0o660)

            dir_mode = stat.S_IMODE(config.socket_path.parent.stat().st_mode)
            sock_mode = _mode_of(config.socket_path)

            assert dir_mode == 0o2750
            assert sock_mode == 0o660
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    finally:
        shutil.rmtree(socket_dir, ignore_errors=True)


def test_prepare_socket_dir_warns_when_it_cannot_set_the_bit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Warn rather than crash: a lost setgid bit means only that the PEP
    cannot connect yet, never that this service is unsafe to serve."""
    target = tmp_path / "sock"

    def refuse(self: Path, mode: int) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr(Path, "chmod", refuse)

    with caplog.at_level(logging.WARNING, logger="sessiond"):
        _prepare_socket_dir(target)

    assert "could not set mode" in caplog.text
