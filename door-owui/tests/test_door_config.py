"""Starting the door: fail closed, and never bind every interface."""

from __future__ import annotations

from pathlib import Path

import pytest
from agent_door_owui.config import (
    ENV_BIND,
    ENV_FAMILIES_DIR,
    ENV_KEY_FILE,
    ENV_SOCKET,
    ENV_TOKEN_FILE,
    ENV_URL,
    MIN_KEY_BYTES,
    ConfigError,
    from_env,
)

GOOD_KEY = "k" * MIN_KEY_BYTES


def _env(tmp_path: Path, **overrides: str) -> dict[str, str]:
    key_file = tmp_path / "door.key"
    key_file.write_text(GOOD_KEY, encoding="utf-8")
    token_file = tmp_path / "attendance.token"
    token_file.write_text("t" * MIN_KEY_BYTES, encoding="utf-8")
    env = {
        ENV_KEY_FILE: str(key_file),
        ENV_TOKEN_FILE: str(token_file),
        ENV_FAMILIES_DIR: str(tmp_path / "families"),
    }
    env.update(overrides)
    return env


def test_a_unix_socket_is_the_default_target(tmp_path: Path) -> None:
    config = from_env(_env(tmp_path))

    assert config.attendance_socket == Path("/srv/agents/state/rework/sock/sessiond.sock")
    assert config.bind_host == "127.0.0.1"
    assert config.bind_port == 8340
    assert config.door_key == GOOD_KEY


def test_a_lan_url_replaces_the_socket(tmp_path: Path) -> None:
    config = from_env(_env(tmp_path, **{ENV_URL: "http://192.0.2.10:8310/"}))

    assert config.attendance_socket is None
    assert config.attendance_url == "http://192.0.2.10:8310"


def test_two_targets_are_refused(tmp_path: Path) -> None:
    env = _env(tmp_path, **{ENV_URL: "http://192.0.2.10:8310", ENV_SOCKET: "/run/s.sock"})

    with pytest.raises(ConfigError):
        from_env(env)


def test_binding_every_interface_is_refused(tmp_path: Path) -> None:
    for bad in ("0.0.0.0:8340", ":::8340", "8340"):
        with pytest.raises(ConfigError) as caught:
            from_env(_env(tmp_path, **{ENV_BIND: bad}))

        assert "0.0.0.0" in str(caught.value) or "host:port" in str(caught.value)


def test_a_lan_bind_is_accepted(tmp_path: Path) -> None:
    config = from_env(_env(tmp_path, **{ENV_BIND: "192.0.2.10:8340"}))

    assert (config.bind_host, config.bind_port) == ("192.0.2.10", 8340)


def test_a_short_key_refuses_to_start(tmp_path: Path) -> None:
    short = tmp_path / "short.key"
    short.write_text("abc", encoding="utf-8")

    with pytest.raises(ConfigError) as caught:
        from_env(_env(tmp_path, **{ENV_KEY_FILE: str(short)}))

    assert str(MIN_KEY_BYTES) in str(caught.value)


def test_an_empty_key_refuses_to_start(tmp_path: Path) -> None:
    empty = tmp_path / "empty.key"
    empty.write_text("", encoding="utf-8")

    with pytest.raises(ConfigError):
        from_env(_env(tmp_path, **{ENV_KEY_FILE: str(empty)}))


def test_a_missing_key_file_refuses_to_start(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as caught:
        from_env(_env(tmp_path, **{ENV_KEY_FILE: str(tmp_path / "absent.key")}))

    assert "cannot read" in str(caught.value)


def test_no_key_setting_refuses_to_start(tmp_path: Path) -> None:
    env = _env(tmp_path)
    del env[ENV_KEY_FILE]

    with pytest.raises(ConfigError):
        from_env(env)


def test_the_summary_carries_no_secret(tmp_path: Path) -> None:
    summary = from_env(_env(tmp_path)).describe()

    assert GOOD_KEY not in summary
    assert "t" * MIN_KEY_BYTES not in summary
    assert "key_bytes=32" in summary
