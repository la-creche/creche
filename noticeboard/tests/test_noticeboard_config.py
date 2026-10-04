"""The config and the perimeter."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from noticeboard.config import DEFAULT_PORT, LAN_ADDRESS_ENV, ConfigError, from_env
from noticeboard.security import Origin, Refusal, check_csrf, check_key, is_keyless, mint_csrf

LONG_KEY = "k" * 32

#: An address on the LAN, not loopback: TEST-NET-1 (RFC 5737).
LAN_BIND = "192.0.2.99"


def _site(**extra: str) -> dict[str, str]:
    """What the unit's site file hands the service, plus `extra`."""
    return {LAN_ADDRESS_ENV: os.environ[LAN_ADDRESS_ENV], **extra}


def test_defaults_bind_the_lan_address() -> None:
    config = from_env(_site(VIEW_ACCESS_KEY=LONG_KEY))

    assert config.bind == os.environ[LAN_ADDRESS_ENV]
    assert config.port == DEFAULT_PORT
    assert not config.on_loopback


def test_an_explicit_bind_still_wins() -> None:
    config = from_env({"VIEW_BIND": LAN_BIND, "VIEW_ACCESS_KEY": LONG_KEY})

    assert config.bind == LAN_BIND


def test_a_missing_lan_address_names_the_variable() -> None:
    with pytest.raises(ConfigError, match=LAN_ADDRESS_ENV):
        from_env({"VIEW_ACCESS_KEY": LONG_KEY})


def test_lan_bind_with_no_key_refuses_to_start() -> None:
    """An unkeyed LAN bind would be an open admin surface."""
    with pytest.raises(ConfigError, match="unkeyed admin surface"):
        from_env({"VIEW_BIND": LAN_BIND, "VIEW_ACCESS_KEY": ""})


def test_lan_bind_with_a_short_key_refuses_to_start() -> None:
    with pytest.raises(ConfigError, match="under 32 bytes"):
        from_env({"VIEW_BIND": LAN_BIND, "VIEW_ACCESS_KEY": "short"})


def test_loopback_bind_with_no_key_starts() -> None:
    config = from_env({"VIEW_BIND": "127.0.0.1", "VIEW_ACCESS_KEY": ""})

    assert config.on_loopback
    assert config.access_key == ""


@pytest.mark.parametrize("bind", ["0.0.0.0", "::", "*"])
def test_wildcard_bind_is_refused(bind: str) -> None:
    with pytest.raises(ConfigError, match="never a wildcard"):
        from_env({"VIEW_BIND": bind, "VIEW_ACCESS_KEY": LONG_KEY})


def test_key_file_wins_over_the_literal_value(tmp_path: Path) -> None:
    path = tmp_path / "noticeboard.key"
    path.write_text("f" * 40 + "\n", encoding="utf-8")

    config = from_env(_site(VIEW_ACCESS_KEY_FILE=str(path), VIEW_ACCESS_KEY=LONG_KEY))

    assert config.access_key == "f" * 40


def test_unreadable_key_file_refuses_to_start(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot be read"):
        from_env(_site(VIEW_ACCESS_KEY_FILE=str(tmp_path / "missing")))


def test_a_key_file_that_is_not_utf8_refuses_to_start(tmp_path: Path) -> None:
    path = tmp_path / "noticeboard.key"
    path.write_bytes(b"\xff" * 40)

    with pytest.raises(ConfigError, match="cannot be read"):
        from_env(_site(VIEW_ACCESS_KEY_FILE=str(path)))


def test_a_key_file_name_that_names_no_file_refuses_to_start() -> None:
    """No file has a NUL character in its name."""
    with pytest.raises(ConfigError, match="cannot be read"):
        from_env(_site(VIEW_ACCESS_KEY_FILE="noticeboard\x00.key"))


def test_bad_port_refuses_to_start() -> None:
    with pytest.raises(ConfigError, match="outside 1 to 65535"):
        from_env(_site(VIEW_ACCESS_KEY=LONG_KEY, VIEW_PORT="0"))

    with pytest.raises(ConfigError, match="not a number"):
        from_env(_site(VIEW_ACCESS_KEY=LONG_KEY, VIEW_PORT="eight"))


def test_state_paths_come_off_one_root(tmp_path: Path) -> None:
    config = from_env({"VIEW_BIND": "127.0.0.1", "VIEW_STATE_ROOT": str(tmp_path)})

    assert config.families_dir == tmp_path / "families"
    assert config.audit_dir == tmp_path / "audit"
    assert config.outcomes_dir == tmp_path / "outcomes"
    assert config.view_token_file == tmp_path / "tokens" / "view-ro.token"


def test_a_attendance_url_replaces_the_socket() -> None:
    url = f"http://{LAN_BIND}:8350"
    config = from_env({"VIEW_BIND": "127.0.0.1", "VIEW_SESSIOND_URL": url})

    assert config.attendance_socket is None
    assert config.attendance_url == url


def test_key_check_refuses_a_missing_and_a_wrong_key() -> None:
    assert check_key(LONG_KEY, LONG_KEY) is None
    assert check_key(None, LONG_KEY) is Refusal.NO_KEY
    assert check_key("", LONG_KEY) is Refusal.NO_KEY
    assert check_key("wrong", LONG_KEY) is Refusal.BAD_KEY


def test_an_unset_key_checks_nothing() -> None:
    """Only reachable on loopback: `from_env` refuses the LAN case."""
    assert check_key(None, "") is None


def test_keyless_paths_are_styling_and_health_only() -> None:
    assert is_keyless("/healthz")
    assert is_keyless("/static/noticeboard.css")
    assert not is_keyless("/")
    assert not is_keyless("/audit")
    assert not is_keyless("/staticky")


def test_csrf_needs_a_cookie_a_field_and_an_origin() -> None:
    token = mint_csrf()
    same = Origin(origin="https://agents.example", referer=None, host="agents.example")

    assert check_csrf(token, token, same) is None
    assert check_csrf(None, token, same) is Refusal.NO_TOKEN
    assert check_csrf(token, None, same) is Refusal.NO_TOKEN
    assert check_csrf(token, mint_csrf(), same) is Refusal.BAD_TOKEN


def test_csrf_refuses_a_foreign_origin_even_with_a_good_token() -> None:
    token = mint_csrf()
    foreign = Origin(origin="https://evil.example", referer=None, host="agents.example")

    assert check_csrf(token, token, foreign) is Refusal.FOREIGN_ORIGIN


def test_csrf_falls_back_to_referer_and_refuses_when_both_are_absent() -> None:
    token = mint_csrf()
    referer = "https://agents.example/families"
    by_referer = Origin(origin=None, referer=referer, host="agents.example")
    naked = Origin(origin=None, referer=None, host="agents.example")

    assert check_csrf(token, token, by_referer) is None
    assert check_csrf(token, token, naked) is Refusal.FOREIGN_ORIGIN
