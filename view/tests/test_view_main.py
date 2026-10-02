"""`--check`, and the two starts the service refuses."""

from __future__ import annotations

from pathlib import Path

import pytest
from agent_view.__main__ import EXIT_BAD_CONFIG, EXIT_OK, main
from agent_view.config import MIN_KEY_BYTES

KEY = "k" * MIN_KEY_BYTES


def env(**extra: str) -> dict[str, str]:
    body = {"VIEW_BIND": "127.0.0.1"}
    body.update(extra)
    return body


def run(monkeypatch: pytest.MonkeyPatch, values: dict[str, str], *argv: str) -> int:
    monkeypatch.setattr("os.environ", values)
    return main(list(argv))


def test_check_reports_and_exits(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    code = run(monkeypatch, env(VIEW_STATE_ROOT=str(tmp_path)), "--check")

    assert code == EXIT_OK
    printed = capsys.readouterr().out
    assert "bind          127.0.0.1:8370" in printed
    assert "MISSING" in printed


def test_check_never_prints_the_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Invariant 13. It says whether a key is set, never what it is."""
    code = run(monkeypatch, env(VIEW_ACCESS_KEY=KEY), "--check")

    assert code == EXIT_OK
    printed = capsys.readouterr().out
    assert KEY not in printed
    assert "access key    set" in printed


def test_a_lan_bind_with_no_key_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unkeyed LAN bind would be an open admin surface. No flag makes it legal."""
    code = run(monkeypatch, {"VIEW_BIND": "192.0.2.10"}, "--check")

    assert code == EXIT_BAD_CONFIG
    assert "refusing to serve an unkeyed admin surface" in capsys.readouterr().err


def test_a_wildcard_bind_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code = run(monkeypatch, {"VIEW_BIND": "0.0.0.0", "VIEW_ACCESS_KEY": KEY}, "--check")

    assert code == EXIT_BAD_CONFIG
    assert "never a wildcard" in capsys.readouterr().err


def test_a_lan_bind_with_a_long_key_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    code = run(monkeypatch, {"VIEW_BIND": "192.0.2.10", "VIEW_ACCESS_KEY": KEY}, "--check")

    assert code == EXIT_OK
