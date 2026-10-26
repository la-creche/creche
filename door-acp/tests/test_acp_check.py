"""`agent-door-acp --check`: the key file, the bind and the unit file.

The door fails closed. A key file that another user can read, a short key or
a bind on every interface stops the door before it binds a port.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path

import pytest
from agent_door_acp.__main__ import main
from agent_door_acp.config import (
    ENV_ATTENDANCE_TOKEN_FILE,
    ENV_BIND,
    ENV_KEY_FILE,
    ENV_LAN_ADDRESS,
    ENV_REQUIRE_AUTHENTIK,
    ConfigError,
    from_env,
)

KEY = "k" * 32
TOKEN = "t" * 32
LAN = "192.0.2.10"
UNIT = Path(__file__).resolve().parents[2] / "systemd" / "creche-door-acp.service"


def _key_file(
    tmp_path: Path, mode: int = 0o600, text: str = KEY, name: str = "door-acp.key"
) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    os.chmod(path, mode)
    return path


def _env(key: Path, bind: str | None = f"{LAN}:8390") -> dict[str, str]:
    token = _key_file(key.parent, text=TOKEN, name="door-acp.token")
    env = {ENV_KEY_FILE: str(key), ENV_ATTENDANCE_TOKEN_FILE: str(token)}
    if bind is not None:
        env[ENV_BIND] = bind

    return env


def _check(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> int:
    for name in (
        ENV_KEY_FILE,
        ENV_BIND,
        ENV_LAN_ADDRESS,
        ENV_ATTENDANCE_TOKEN_FILE,
        ENV_REQUIRE_AUTHENTIK,
    ):
        monkeypatch.delenv(name, raising=False)

    for name, value in env.items():
        monkeypatch.setenv(name, value)

    return main(["--check"])


def test_a_group_readable_key_file_fails_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _check(monkeypatch, _env(_key_file(tmp_path, mode=0o640)))

    said = capsys.readouterr()
    assert code != 0
    assert "0600" in said.err
    assert KEY not in said.out + said.err


def test_a_missing_key_file_fails_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _check(monkeypatch, _env(tmp_path / "absent.key")) != 0


def test_an_unset_key_file_fails_the_check(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _check(monkeypatch, {ENV_BIND: f"{LAN}:8390"}) != 0


def test_a_good_config_passes_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _check(monkeypatch, _env(_key_file(tmp_path)))

    said = capsys.readouterr()
    assert code == 0
    assert f"bind={LAN}:8390" in said.out
    assert KEY not in said.out + said.err
    assert TOKEN not in said.out + said.err


def test_one_key_for_the_door_and_attendance_is_refused(tmp_path: Path) -> None:
    env = _env(_key_file(tmp_path, text=TOKEN))

    with pytest.raises(ConfigError, match="two secrets"):
        from_env(env)


@pytest.mark.parametrize(("value", "required"), [("", True), ("true", True), ("false", False)])
def test_require_authentik_defaults_to_true(tmp_path: Path, value: str, required: bool) -> None:
    env = _env(_key_file(tmp_path)) | {ENV_REQUIRE_AUTHENTIK: value}

    assert from_env(env).require_authentik is required


def test_a_bad_require_authentik_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        from_env(_env(_key_file(tmp_path)) | {ENV_REQUIRE_AUTHENTIK: "yes"})


@pytest.mark.parametrize("text", ["", "k" * 31, "é" * 16, "k" * 31 + "\n"])
def test_a_short_or_non_ascii_key_is_refused(tmp_path: Path, text: str) -> None:
    with pytest.raises(ConfigError):
        from_env(_env(_key_file(tmp_path, text=text)))


@pytest.mark.parametrize(
    "bind",
    [
        "0.0.0.0:8390",
        "[::]:8390",
        ":8390",
        "*:8390",
        "224.0.0.1:8390",
        "[ff02::1]:8390",
        "door.invalid:8390",
        f"{LAN}:0",
        f"{LAN}:65536",
        f"{LAN}:+80",
        f"{LAN}:x",
        LAN,
    ],
)
def test_a_wide_or_bad_bind_is_refused(tmp_path: Path, bind: str) -> None:
    with pytest.raises(ConfigError):
        from_env(_env(_key_file(tmp_path), bind=bind))


@pytest.mark.parametrize("bind", [f"{LAN}:8390", "127.0.0.1:8390", "[2001:db8::10]:8390"])
def test_a_lan_or_loopback_bind_is_allowed(tmp_path: Path, bind: str) -> None:
    config = from_env(_env(_key_file(tmp_path), bind=bind))

    assert f"{config.bind_host}:{config.bind_port}".endswith(":8390")


def test_the_default_bind_is_the_lan_address(tmp_path: Path) -> None:
    env = _env(_key_file(tmp_path), bind=None) | {ENV_LAN_ADDRESS: LAN}

    config = from_env(env)

    assert (config.bind_host, config.bind_port) == (LAN, 8390)


def test_no_bind_and_no_lan_address_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        from_env(_env(_key_file(tmp_path), bind=None))


def test_the_unit_checks_the_config_then_runs_the_door() -> None:
    lines = UNIT.read_text(encoding="utf-8").splitlines()
    values = {line.partition("=")[0]: line.partition("=")[2] for line in lines if "=" in line}

    start = shlex.split(values["ExecStart"])
    check = shlex.split(values["ExecStartPre"])
    assert Path(start[0]).name == "agent-door-acp"
    assert check == [start[0], "--check"]
    assert "Environment=HOME=%h" in lines
    assert (
        f"Environment={ENV_ATTENDANCE_TOKEN_FILE}=/srv/agents/state/rework/tokens/door-acp.token"
        in lines
    )
    assert "Environment=XDG_CONFIG_HOME=%h/.config" in lines
