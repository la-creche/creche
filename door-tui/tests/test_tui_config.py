"""Config, ids and exit codes: everything `--check` exercises.

The TUI door holds one secret, its `sessiond` token, and it reads it from a
file into memory (invariant 13). Nothing here may put a token on argv, in a
URL or in a message.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from agent_door_tui.config import (
    ENV_FAMILIES_DIR,
    ENV_PI_LAUNCH,
    ENV_SBX,
    ENV_SOCKET,
    ENV_TOKEN_FILE,
    ENV_URL,
    MIN_TOKEN_BYTES,
    ConfigError,
    from_env,
)
from agent_door_tui.errors import Exit
from agent_door_tui.ids import (
    SESSION_ID_MAX,
    door_of,
    is_family,
    is_sandbox,
    is_session,
    is_ulid,
    new_session_id,
)

TOKEN = "door-tui-token-" + "t" * 32


def write_token(tmp_path: Path, value: str = TOKEN) -> Path:
    path = tmp_path / "door-tui.token"
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(0o600)

    return path


def base_env(tmp_path: Path) -> dict[str, str]:
    return {ENV_TOKEN_FILE: str(write_token(tmp_path))}


def test_config_reads_the_token_file(tmp_path: Path) -> None:
    config = from_env(base_env(tmp_path))

    assert config.sessiond_token == TOKEN
    assert config.sessiond_socket is not None
    assert config.families_dir.name == "families"


def test_config_refuses_a_short_token(tmp_path: Path) -> None:
    path = tmp_path / "short.token"
    path.write_text("abc\n", encoding="utf-8")

    with pytest.raises(ConfigError, match=str(MIN_TOKEN_BYTES)):
        from_env({ENV_TOKEN_FILE: str(path)})


def test_config_refuses_a_missing_token_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read"):
        from_env({ENV_TOKEN_FILE: str(tmp_path / "nothing.token")})


def test_config_refuses_a_url_and_a_socket_together(tmp_path: Path) -> None:
    env = base_env(tmp_path)
    env[ENV_URL] = "http://192.0.2.10:8350"
    env[ENV_SOCKET] = "/srv/agents/state/rework/sock/sessiond.sock"

    with pytest.raises(ConfigError, match="not both"):
        from_env(env)


def test_config_refuses_a_url_with_no_scheme(tmp_path: Path) -> None:
    env = base_env(tmp_path)
    env[ENV_URL] = "192.0.2.10:8350"

    with pytest.raises(ConfigError, match="http://"):
        from_env(env)


def test_config_takes_the_lan_url_when_it_is_given(tmp_path: Path) -> None:
    env = base_env(tmp_path)
    env[ENV_URL] = "http://192.0.2.10:8350/"

    config = from_env(env)

    assert config.sessiond_url == "http://192.0.2.10:8350"
    assert config.sessiond_socket is None


def test_config_overrides_sbx_and_the_launcher(tmp_path: Path) -> None:
    env = base_env(tmp_path)
    env[ENV_SBX] = str(tmp_path / "fake-sbx")
    env[ENV_PI_LAUNCH] = "/opt/agent-supervisor/agent-pi-launch.js"
    env[ENV_FAMILIES_DIR] = str(tmp_path / "families")

    config = from_env(env)

    assert config.sbx == str(tmp_path / "fake-sbx")
    assert config.families_dir == tmp_path / "families"


def test_describe_carries_no_token(tmp_path: Path) -> None:
    line = from_env(base_env(tmp_path)).describe()

    assert TOKEN not in line
    assert "token_bytes=" in line


def test_door_instance_is_one_per_process(tmp_path: Path) -> None:
    """Two terminals are two writers, so each names itself (contract 02 §7.1)."""
    instance = from_env(base_env(tmp_path)).door_instance

    assert instance.startswith("tui.")
    assert instance.split(".")[1].isdigit()


def test_a_minted_session_id_is_a_tui_ulid() -> None:
    session = new_session_id()

    assert session.startswith("tui-")
    assert is_ulid(session.removeprefix("tui-"))
    assert is_session(session)
    assert len(session) <= SESSION_ID_MAX


def test_two_minted_ids_differ() -> None:
    assert new_session_id() != new_session_id()


def test_a_ulid_excludes_the_four_letters() -> None:
    """Crockford base32 has no I, L, O or U (contract 02 §2)."""
    minted = {new_session_id().removeprefix("tui-") for _ in range(50)}

    assert all(is_ulid(one) for one in minted)
    assert not set("ILOU") & set("".join(minted))


@pytest.mark.parametrize(
    ("value", "expected"),
    [("chat", True), ("Chat", False), ("1chat", False), ("c", False), ("a-b-c", True)],
)
def test_family_names(value: str, expected: bool) -> None:
    assert is_family(value) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [("chat-s1", True), ("chat-s12", True), ("chat", False), ("chat-sX", False)],
)
def test_sandbox_ids(value: str, expected: bool) -> None:
    assert is_sandbox(value) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [("tui-01JB", "tui"), ("owui-3f2a", "owui"), ("job-01JB", "job"), ("nothing", "")],
)
def test_which_door_made_a_session(value: str, expected: str) -> None:
    assert door_of(value) == expected


def test_a_session_id_is_bounded(tmp_path: Path) -> None:
    assert not is_session("x" * (SESSION_ID_MAX + 1))
    assert not is_session("..")
    assert not is_session("/etc/passwd")


def test_exit_codes_never_collide_with_the_launcher() -> None:
    """Contract 03 §7.6 owns 0 to 11. This door's own codes start at 64."""
    own = [code for code in Exit if code is not Exit.OK]

    assert all(code >= 64 for code in own)
