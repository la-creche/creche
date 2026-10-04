"""Starting either process: fail closed, and never bind every interface."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from agent_door_trigger.config import (
    DEFAULT_REGISTRY_ROOT,
    DEFAULT_WEBHOOK_PORT,
    ENV_ATTENDANCE_SOCKET,
    ENV_ATTENDANCE_TOKEN_FILE,
    ENV_ATTENDANCE_URL,
    ENV_BIND,
    ENV_FAMILIES_DIR,
    ENV_LAN_ADDRESS,
    ENV_PEP_URL,
    ENV_REFRESH_S,
    ENV_REGISTRY_ROOT,
    ENV_WEBHOOKS_DIR,
    MIN_TOKEN_BYTES,
    PEP_PORT,
    ConfigError,
    fire_config_from_env,
    serve_config_from_env,
)

GOOD_TOKEN = "t" * MIN_TOKEN_BYTES


def _lan() -> str:
    """The example site's address, which the root conftest sets."""
    return os.environ[ENV_LAN_ADDRESS]


def _env(tmp_path: Path, **overrides: str) -> dict[str, str]:
    token_file = tmp_path / "door-trigger.token"
    token_file.write_text(GOOD_TOKEN, encoding="utf-8")
    env = {ENV_ATTENDANCE_TOKEN_FILE: str(token_file), ENV_LAN_ADDRESS: _lan()}
    env.update(overrides)
    return env


def _without_site(tmp_path: Path, **overrides: str) -> dict[str, str]:
    """`_env` with no `AGENT_LAN_ADDRESS`: a unit that lost its site file."""
    env = _env(tmp_path, **overrides)
    del env[ENV_LAN_ADDRESS]
    return env


# --- shared: the attendance target both configs carry ---


def test_a_unix_socket_is_the_default_attendance_target(tmp_path: Path) -> None:
    config = fire_config_from_env(_env(tmp_path))

    assert config.attendance.socket == Path("/srv/agents/state/rework/sock/sessiond.sock")
    assert config.attendance.token == GOOD_TOKEN


def test_a_lan_url_replaces_the_socket(tmp_path: Path) -> None:
    url = f"http://{_lan()}:8350"
    config = fire_config_from_env(_env(tmp_path, **{ENV_ATTENDANCE_URL: f"{url}/"}))

    assert config.attendance.socket is None
    assert config.attendance.url == url


def test_two_attendance_targets_are_refused(tmp_path: Path) -> None:
    env = _env(
        tmp_path,
        **{ENV_ATTENDANCE_URL: f"http://{_lan()}:8350", ENV_ATTENDANCE_SOCKET: "/run/s.sock"},
    )

    with pytest.raises(ConfigError):
        fire_config_from_env(env)


def test_a_short_token_refuses_to_start(tmp_path: Path) -> None:
    short = tmp_path / "short.token"
    short.write_text("abc", encoding="utf-8")

    with pytest.raises(ConfigError) as caught:
        fire_config_from_env(_env(tmp_path, **{ENV_ATTENDANCE_TOKEN_FILE: str(short)}))

    assert str(MIN_TOKEN_BYTES) in str(caught.value)


def test_an_empty_token_refuses_to_start(tmp_path: Path) -> None:
    empty = tmp_path / "empty.token"
    empty.write_text("", encoding="utf-8")

    with pytest.raises(ConfigError):
        fire_config_from_env(_env(tmp_path, **{ENV_ATTENDANCE_TOKEN_FILE: str(empty)}))


def test_a_missing_token_file_refuses_to_start(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as caught:
        fire_config_from_env(_env(tmp_path, **{ENV_ATTENDANCE_TOKEN_FILE: str(tmp_path / "gone")}))

    assert "cannot read" in str(caught.value)


def test_a_token_file_that_is_not_utf8_refuses_to_start(tmp_path: Path) -> None:
    binary = tmp_path / "binary.token"
    binary.write_bytes(b"\xff\xfe" * MIN_TOKEN_BYTES)
    env = _env(tmp_path, **{ENV_ATTENDANCE_TOKEN_FILE: str(binary)})

    for from_env in (fire_config_from_env, serve_config_from_env):
        with pytest.raises(ConfigError, match="not UTF-8") as caught:
            from_env(env)

        assert ENV_ATTENDANCE_TOKEN_FILE in str(caught.value)
        # The decode error holds bytes of the token. The refusal does not carry it.
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None


def test_a_token_path_that_the_system_refuses_refuses_to_start(tmp_path: Path) -> None:
    env = _env(tmp_path, **{ENV_ATTENDANCE_TOKEN_FILE: str(tmp_path / "a\x00b")})

    with pytest.raises(ConfigError, match="cannot read") as caught:
        fire_config_from_env(env)

    assert "\x00" not in str(caught.value)


def test_the_fire_summary_carries_no_secret(tmp_path: Path) -> None:
    summary = fire_config_from_env(_env(tmp_path)).describe()

    assert GOOD_TOKEN not in summary


# --- fire: the default token path needs no override at all ---


def test_fire_needs_only_a_token_file_by_default(tmp_path: Path) -> None:
    # No DOOR_TRIGGER_* var but the token file: matches the systemd unit,
    # which sets no environment beyond HOME and XDG_CONFIG_HOME
    # (systemd/creche-trigger@.service). The default token path is absolute
    # and cannot be pointed at tmp_path, so this only proves the socket and
    # families/registry settings need no override to construct.
    config = fire_config_from_env(_env(tmp_path))

    assert config.attendance.url == "http://sessiond"


# --- fire: the PEP the quiet check calls ---


def test_fire_builds_the_pep_url_from_the_lan_address(tmp_path: Path) -> None:
    config = fire_config_from_env(_env(tmp_path))

    assert config.pep_url == f"http://{_lan()}:{PEP_PORT}"


def test_an_explicit_pep_url_still_wins(tmp_path: Path) -> None:
    config = fire_config_from_env(
        _without_site(tmp_path, **{ENV_PEP_URL: "http://chaperone.test:1/"})
    )

    assert config.pep_url == "http://chaperone.test:1"


def test_fire_with_no_lan_address_names_the_variable(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=ENV_LAN_ADDRESS):
        fire_config_from_env(_without_site(tmp_path))


# --- serve: LAN bind, registry root, webhook directory ---


def test_serve_binds_the_lan_address_by_default(tmp_path: Path) -> None:
    config = serve_config_from_env(_env(tmp_path))

    assert (config.bind_host, config.bind_port) == (_lan(), DEFAULT_WEBHOOK_PORT)
    assert config.registry_root == Path(DEFAULT_REGISTRY_ROOT)


def test_serve_with_no_lan_address_names_the_variable(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=ENV_LAN_ADDRESS):
        serve_config_from_env(_without_site(tmp_path))


def test_serve_binding_every_interface_is_refused(tmp_path: Path) -> None:
    for bad in ("0.0.0.0:8360", ":::8360", "8360"):
        with pytest.raises(ConfigError) as caught:
            serve_config_from_env(_env(tmp_path, **{ENV_BIND: bad}))

        assert "0.0.0.0" in str(caught.value) or "host:port" in str(caught.value)


def test_serve_accepts_an_explicit_lan_bind(tmp_path: Path) -> None:
    config = serve_config_from_env(_without_site(tmp_path, **{ENV_BIND: "192.0.2.99:9999"}))

    assert (config.bind_host, config.bind_port) == ("192.0.2.99", 9999)


def test_serve_registry_and_webhooks_dir_are_overridable(tmp_path: Path) -> None:
    config = serve_config_from_env(
        _env(
            tmp_path,
            **{
                ENV_REGISTRY_ROOT: str(tmp_path / "registry"),
                ENV_WEBHOOKS_DIR: str(tmp_path / "webhooks"),
                ENV_FAMILIES_DIR: str(tmp_path / "families"),
            },
        )
    )

    assert config.registry_root == tmp_path / "registry"
    assert config.webhooks_dir == tmp_path / "webhooks"
    assert config.families_dir == tmp_path / "families"


def test_serve_refresh_interval_is_configurable(tmp_path: Path) -> None:
    config = serve_config_from_env(_env(tmp_path, **{ENV_REFRESH_S: "5"}))

    assert config.refresh_s == 5.0


def test_serve_refresh_interval_must_be_positive(tmp_path: Path) -> None:
    for bad in ("0", "-1", "not-a-number"):
        with pytest.raises(ConfigError):
            serve_config_from_env(_env(tmp_path, **{ENV_REFRESH_S: bad}))


def test_the_serve_summary_carries_no_secret(tmp_path: Path) -> None:
    summary = serve_config_from_env(_env(tmp_path)).describe()

    assert GOOD_TOKEN not in summary
