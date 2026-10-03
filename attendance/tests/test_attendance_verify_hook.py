"""The component's verify hook (contract 06 §4, `attendance/component.yaml`)."""

from __future__ import annotations

import json
import socket
from pathlib import Path

import httpx
import pytest
from attendance.verify import EXIT_FAILED, EXIT_OK, HTTP_UNAUTHORIZED, main

SOCKET_NAME = "sessiond.sock"


def bound_socket(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A real Unix socket, so the `socket` check sees what it will on the
    host. Nothing listens on it: the transport is faked above it.

    The bind uses the RELATIVE name. macOS caps an `AF_UNIX` path at 104
    bytes and pytest's `tmp_path` is longer than that on its own."""
    monkeypatch.chdir(tmp_path)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCKET_NAME)
    server.close()

    return tmp_path / SOCKET_NAME


def replying(status: int) -> object:
    def transport(**_: object) -> httpx.MockTransport:
        return httpx.MockTransport(lambda _request: httpx.Response(status))

    return transport


def refusing() -> object:
    def transport(**_: object) -> httpx.MockTransport:
        def fail(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        return httpx.MockTransport(fail)

    return transport


def run(monkeypatch: pytest.MonkeyPatch, socket_path: Path, transport: object) -> int:
    monkeypatch.setenv("SESSIOND_SOCKET", str(socket_path))
    monkeypatch.setattr("attendance.verify.httpx.HTTPTransport", transport)

    return main(["--json"])


def body(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    return json.loads(capsys.readouterr().out)


def failed(capsys: pytest.CaptureFixture[str]) -> list[str]:
    checks: list[dict[str, object]] = body(capsys)["checks"]  # type: ignore[assignment]

    return [str(one["name"]) for one in checks if not one["ok"]]


def test_a_running_service_that_fails_closed_passes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    code = run(monkeypatch, bound_socket(monkeypatch, tmp_path), replying(HTTP_UNAUTHORIZED))

    assert code == EXIT_OK
    assert body(capsys)["ok"] is True


def test_an_anonymous_read_that_succeeds_fails_the_hook(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A 200 means the door opened to anyone who reaches the socket."""
    code = run(monkeypatch, bound_socket(monkeypatch, tmp_path), replying(200))

    assert code == EXIT_FAILED
    assert failed(capsys) == ["anonymous read"]


def test_a_missing_socket_fails_with_one_check(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    code = run(monkeypatch, tmp_path / "absent.sock", replying(HTTP_UNAUTHORIZED))

    assert code == EXIT_FAILED
    assert failed(capsys) == ["socket"]


def test_a_plain_file_where_the_socket_belongs_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    planted = tmp_path / "sessiond.sock"
    planted.write_text("not a socket", encoding="utf-8")

    code = run(monkeypatch, planted, replying(HTTP_UNAUTHORIZED))

    assert code == EXIT_FAILED
    assert failed(capsys) == ["socket"]


def test_an_env_file_supplies_the_socket_path(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Root's executor hands the hook a fixed,
    minimal environment that carries none of `creche-attendance.service`'s own
    `EnvironmentFile=`. `--env-file` is the same file the unit's own
    `EnvironmentFile=` names, read by this hook itself — never the process
    env alone."""
    monkeypatch.delenv("SESSIOND_SOCKET", raising=False)
    socket_path = bound_socket(monkeypatch, tmp_path)
    env_file = tmp_path / "sessiond.env"
    env_file.write_text(f"SESSIOND_SOCKET={socket_path}\n", encoding="utf-8")
    monkeypatch.setattr("attendance.verify.httpx.HTTPTransport", replying(HTTP_UNAUTHORIZED))

    code = main(["--json", "--env-file", str(env_file)])

    assert code == EXIT_OK
    assert body(capsys)["ok"] is True


def test_a_missing_env_file_fails_closed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A file the manifest promised and the host does not have is a hook
    failure, never a silent fall-back to `DEFAULT_SOCKET`."""
    monkeypatch.delenv("SESSIOND_SOCKET", raising=False)

    code = main(["--json", "--env-file", str(tmp_path / "absent.env")])

    assert code == EXIT_FAILED
    assert failed(capsys) == ["env-file"]


def test_a_dead_service_behind_a_live_socket_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    code = run(monkeypatch, bound_socket(monkeypatch, tmp_path), refusing())

    assert code == EXIT_FAILED
    assert failed(capsys) == ["anonymous read"]
