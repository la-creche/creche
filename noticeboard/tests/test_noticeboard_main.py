"""`--check`, and the two starts the service refuses."""

from __future__ import annotations

from pathlib import Path

import pytest
from noticeboard.__main__ import EXIT_BAD_CONFIG, EXIT_OK, main
from noticeboard.config import MIN_KEY_BYTES
from noticeboard.strictjson import Rule

from noticeboard import __main__ as entry
from noticeboard import notices

KEY = "k" * MIN_KEY_BYTES


def env(**extra: str) -> dict[str, str]:
    body = {"NOTICEBOARD_BIND": "127.0.0.1"}
    body.update(extra)
    return body


def run(monkeypatch: pytest.MonkeyPatch, values: dict[str, str], *argv: str) -> int:
    monkeypatch.setattr("os.environ", values)
    return main(list(argv))


def test_check_reports_and_exits(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    code = run(monkeypatch, env(NOTICEBOARD_STATE_ROOT=str(tmp_path)), "--check")

    assert code == EXIT_OK
    printed = capsys.readouterr().out
    assert "bind          127.0.0.1:8370" in printed
    assert "MISSING" in printed


def test_check_never_prints_the_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Invariant 13. It says whether a key is set, never what it is."""
    code = run(monkeypatch, env(NOTICEBOARD_ACCESS_KEY=KEY), "--check")

    assert code == EXIT_OK
    printed = capsys.readouterr().out
    assert KEY not in printed
    assert "access key    set" in printed


def test_a_lan_bind_with_no_key_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unkeyed LAN bind would be an open admin surface. No flag makes it legal."""
    code = run(monkeypatch, {"NOTICEBOARD_BIND": "192.0.2.10"}, "--check")

    assert code == EXIT_BAD_CONFIG
    assert "refusing to serve an unkeyed admin surface" in capsys.readouterr().err


def test_a_wildcard_bind_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code = run(
        monkeypatch, {"NOTICEBOARD_BIND": "0.0.0.0", "NOTICEBOARD_ACCESS_KEY": KEY}, "--check"
    )

    assert code == EXIT_BAD_CONFIG
    assert "never a wildcard" in capsys.readouterr().err


def test_a_key_file_that_is_not_text_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The start ends with the exit code of a bad config and one line. It
    does not end with a traceback."""
    path = tmp_path / "noticeboard.key"
    path.write_bytes(b"\xff" * MIN_KEY_BYTES)

    code = run(monkeypatch, env(NOTICEBOARD_ACCESS_KEY_FILE=str(path)), "--check")

    assert code == EXIT_BAD_CONFIG
    assert "NOTICEBOARD_ACCESS_KEY_FILE cannot be read" in capsys.readouterr().err


def test_a_lan_bind_with_a_long_key_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    code = run(
        monkeypatch, {"NOTICEBOARD_BIND": "192.0.2.10", "NOTICEBOARD_ACCESS_KEY": KEY}, "--check"
    )

    assert code == EXIT_OK


@pytest.fixture
def no_server(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """The start runs to the server and starts none. The state of the notice
    module goes back after the test."""
    started: list[object] = []
    monkeypatch.setattr(entry.uvicorn, "run", lambda *args, **kwargs: started.append(args))
    monkeypatch.setattr(notices, "_settings", None)

    return started


def test_a_start_gives_the_notice_file_of_the_state_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, no_server: list[object]
) -> None:
    code = run(monkeypatch, env(NOTICEBOARD_STATE_ROOT=str(tmp_path)))

    assert code == EXIT_OK
    assert len(no_server) == 1
    # The start writes no file. The first refusal writes the notice file.
    assert list(tmp_path.iterdir()) == []

    notices.record("status.document", Rule.SYNTAX, "chat")

    written = (tmp_path / "notices" / "noticeboard.json").read_text(encoding="utf-8")
    assert '"service": "noticeboard"' in written
    assert '"push": "status.document:syntax:' in written


def test_a_start_with_a_state_root_that_cannot_hold_a_directory_still_serves(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    no_server: list[object],
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = tmp_path / "root"
    root.write_bytes(b"")

    code = run(monkeypatch, env(NOTICEBOARD_STATE_ROOT=str(root)))
    notices.record("status.document", Rule.SYNTAX)

    assert code == EXIT_OK
    assert len(no_server) == 1
    assert "json_not_strict noticeboard status.document syntax" in caplog.messages
    assert root.read_bytes() == b""


def test_check_gives_no_notice_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    no_server: list[object],
    caplog: pytest.LogCaptureFixture,
) -> None:
    code = run(monkeypatch, env(NOTICEBOARD_STATE_ROOT=str(tmp_path)), "--check")
    notices.record("status.document", Rule.SYNTAX)

    assert code == EXIT_OK
    assert no_server == []
    assert "json_not_strict unset status.document syntax" in caplog.messages
    assert list(tmp_path.iterdir()) == []
