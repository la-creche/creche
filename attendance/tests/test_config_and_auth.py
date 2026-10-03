"""Config from the environment, and the fail-closed token rule."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from attendance.auth import (
    MIN_TOKEN_BYTES,
    Access,
    Principal,
    TokenBook,
    TokenError,
    check_access,
    check_family_kind,
    check_session_prefix,
    holder_of,
)
from attendance.config import LAN_ADDRESS_ENV, Bind, ConfigError, from_env
from attendance.errors import ApiError, ErrorCode
from attendance.models import Holder
from attendance.paths import token_file
from attendance.states import SessionKind

GOOD_TOKEN = "t" * MIN_TOKEN_BYTES


def write_tokens(state_root: Path, skip: Principal | None = None) -> dict[Principal, str]:
    """One distinct token per principal, at the mode the contract names."""
    made: dict[Principal, str] = {}

    for principal in Principal:
        if principal is skip:
            continue

        value = f"{principal.value}-{'x' * MIN_TOKEN_BYTES}"
        path = token_file(state_root, principal.value)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
        made[principal] = value

    return made


def _site(**extra: str) -> dict[str, str]:
    """What the unit's site file hands the service, plus `extra`."""
    return {LAN_ADDRESS_ENV: os.environ[LAN_ADDRESS_ENV], **extra}


def test_defaults_bind_socket_only() -> None:
    config = from_env(_site())

    assert config.bind is Bind.SOCKET_ONLY
    assert config.binds_lan is False
    assert str(config.socket_path).endswith("sessiond.sock")


def test_bind_lan_reads_the_lan_address() -> None:
    config = from_env(_site(SESSIOND_BIND_LAN="true", SESSIOND_LAN_PORT="8350"))

    assert config.bind is Bind.SOCKET_AND_LAN
    assert config.lan_address == os.environ[LAN_ADDRESS_ENV]
    assert config.lan_port == 8350


def test_an_explicit_lan_address_still_wins() -> None:
    config = from_env({"SESSIOND_LAN_ADDRESS": "192.0.2.99"})

    assert config.lan_address == "192.0.2.99"


def test_a_missing_lan_address_names_the_variable() -> None:
    with pytest.raises(ConfigError, match=LAN_ADDRESS_ENV):
        from_env({})


def test_a_nonsense_bind_flag_refuses() -> None:
    with pytest.raises(ConfigError):
        from_env(_site(SESSIOND_BIND_LAN="maybe"))


def test_a_port_outside_the_range_refuses() -> None:
    with pytest.raises(ConfigError):
        from_env(_site(SESSIOND_LAN_PORT="70000"))


def test_roots_come_from_the_environment(tmp_path: Path) -> None:
    config = from_env(_site(SESSIOND_SESSIONS_ROOT=str(tmp_path / "s")))

    assert config.sessions_root == tmp_path / "s"


def test_a_missing_token_file_stops_the_service(tmp_path: Path) -> None:
    write_tokens(tmp_path, skip=Principal.VIEW_RO)
    book = TokenBook(tmp_path)

    with pytest.raises(TokenError):
        book.load()


def test_an_empty_token_file_stops_the_service(tmp_path: Path) -> None:
    write_tokens(tmp_path)
    path = token_file(tmp_path, Principal.DOOR_OWUI.value)
    path.write_text("", encoding="utf-8")

    with pytest.raises(TokenError):
        TokenBook(tmp_path).load()


def test_a_short_token_file_stops_the_service(tmp_path: Path) -> None:
    write_tokens(tmp_path)
    path = token_file(tmp_path, Principal.DOOR_TUI.value)
    path.write_text("short", encoding="utf-8")

    with pytest.raises(TokenError):
        TokenBook(tmp_path).load()


def test_a_readable_token_file_stops_the_service(tmp_path: Path) -> None:
    write_tokens(tmp_path)
    path = token_file(tmp_path, Principal.MANAGERD.value)
    os.chmod(path, 0o644)

    with pytest.raises(TokenError):
        TokenBook(tmp_path).load()


def test_the_delegate_token_may_be_group_readable(tmp_path: Path) -> None:
    """Contract 02 §3 rule 5's exception: the PEP reads `door-delegate.token`
    as user `pep`, so this file may carry the group-read bit."""
    write_tokens(tmp_path)
    path = token_file(tmp_path, Principal.DOOR_DELEGATE.value)
    os.chmod(path, 0o640)

    TokenBook(tmp_path).load()  # must not raise


def test_the_delegate_token_still_refuses_a_world_bit(tmp_path: Path) -> None:
    write_tokens(tmp_path)
    path = token_file(tmp_path, Principal.DOOR_DELEGATE.value)
    os.chmod(path, 0o644)

    with pytest.raises(TokenError):
        TokenBook(tmp_path).load()


def test_the_delegate_token_still_refuses_group_write(tmp_path: Path) -> None:
    """0640 is the ceiling: group READ, never group write."""
    write_tokens(tmp_path)
    path = token_file(tmp_path, Principal.DOOR_DELEGATE.value)
    os.chmod(path, 0o660)

    with pytest.raises(TokenError):
        TokenBook(tmp_path).load()


def test_a_non_delegate_token_still_refuses_group_read(tmp_path: Path) -> None:
    """The exception is `door-delegate` alone. Every other file stays 0600."""
    write_tokens(tmp_path)
    path = token_file(tmp_path, Principal.DOOR_OWUI.value)
    os.chmod(path, 0o640)

    with pytest.raises(TokenError):
        TokenBook(tmp_path).load()


def test_each_token_identifies_its_own_principal(tmp_path: Path) -> None:
    made = write_tokens(tmp_path)
    book = TokenBook(tmp_path)
    book.load()

    for principal, value in made.items():
        assert book.identify(f"Bearer {value}") is principal


def test_an_unknown_or_absent_token_is_unauthorized(tmp_path: Path) -> None:
    write_tokens(tmp_path)
    book = TokenBook(tmp_path)
    book.load()

    for header in (None, "", "Bearer ", "Basic abc", f"Bearer {GOOD_TOKEN}"):
        with pytest.raises(ApiError) as caught:
            book.identify(header)

        assert caught.value.code is ErrorCode.UNAUTHORIZED


def test_reload_picks_up_a_rotated_token(tmp_path: Path) -> None:
    made = write_tokens(tmp_path)
    book = TokenBook(tmp_path)
    book.load()

    path = token_file(tmp_path, Principal.DOOR_OWUI.value)
    rotated = "rotated-" + "y" * MIN_TOKEN_BYTES
    path.write_text(rotated, encoding="utf-8")
    os.chmod(path, 0o600)
    book.reload()

    assert book.identify(f"Bearer {rotated}") is Principal.DOOR_OWUI

    with pytest.raises(ApiError):
        book.identify(f"Bearer {made[Principal.DOOR_OWUI]}")


def test_the_view_token_never_writes() -> None:
    check_access(Principal.VIEW_RO, Access.READ)

    with pytest.raises(ApiError) as caught:
        check_access(Principal.VIEW_RO, Access.WRITE)

    assert caught.value.code is ErrorCode.FORBIDDEN


def test_a_door_token_never_reaches_internal() -> None:
    with pytest.raises(ApiError):
        check_access(Principal.DOOR_OWUI, Access.INTERNAL)

    check_access(Principal.MANAGERD, Access.INTERNAL)


def test_the_managerd_token_never_reads_sessions() -> None:
    with pytest.raises(ApiError):
        check_access(Principal.MANAGERD, Access.READ)


def test_a_writer_may_also_read() -> None:
    check_access(Principal.DOOR_OWUI, Access.READ)


def test_a_door_token_is_pinned_to_one_family_kind() -> None:
    check_family_kind(Principal.DOOR_OWUI, "chat", SessionKind.ATTENDED)

    with pytest.raises(ApiError) as caught:
        check_family_kind(Principal.DOOR_OWUI, "code-sandbox", SessionKind.THIN)

    assert caught.value.code is ErrorCode.FORBIDDEN


def test_the_refusal_reads_the_same_for_every_kind() -> None:
    """The message reaches the operator through systemd's journal, because
    `agent-trigger fire` prints it and exits (`door-trigger/cli.py`). An
    article chosen for one kind reads wrong for another, so the message
    carries none."""
    for kind in (SessionKind.ATTENDED, SessionKind.THIN, SessionKind.AUTONOMOUS):
        if kind is SessionKind.AUTONOMOUS:
            continue

        with pytest.raises(ApiError) as caught:
            check_family_kind(Principal.DOOR_TRIGGER, "chat", kind)

        assert f" a {kind.value} " not in caught.value.message
        assert kind.value in caught.value.message


def test_the_view_token_sees_every_kind() -> None:
    for kind in SessionKind:
        check_family_kind(Principal.VIEW_RO, "chat", kind)


def test_a_door_token_is_pinned_to_one_session_prefix() -> None:
    check_session_prefix(Principal.DOOR_TUI, "chat", "tui-01JBQ7WZ0X4T9V6K2H8M3N5PQR")

    with pytest.raises(ApiError) as caught:
        check_session_prefix(Principal.DOOR_TUI, "chat", "owui-abc")

    assert caught.value.code is ErrorCode.FORBIDDEN


def test_each_writer_has_its_own_lease_holder() -> None:
    assert holder_of(Principal.DOOR_OWUI, "chat", "owui-a") is Holder.OWUI
    assert holder_of(Principal.DOOR_DELEGATE, "code-sandbox", "job-a") is Holder.DELEGATE

    with pytest.raises(ApiError):
        holder_of(Principal.VIEW_RO, "chat", "owui-a")
