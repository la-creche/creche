"""The CLI: argument parsing, `--check`, exit codes and the one output
line `fire` prints — everything `main()` itself is responsible for.
`execute_fire`'s own attendance call is exercised directly against
`FakeAttendance`; the real wire shape is `test_trigger_fire.py`'s job."""

from __future__ import annotations

from pathlib import Path

import pytest
from agent_door_trigger.attendance import AcceptedTurn
from agent_door_trigger.cli import execute_fire, main
from agent_door_trigger.config import (
    ENV_ATTENDANCE_TOKEN_FILE,
    ENV_REGISTRY_ROOT,
    MIN_TOKEN_BYTES,
)
from agent_door_trigger.errors import AttendanceError, ExitCode
from trigger_fake_attendance import FakeAttendance

GOOD_TOKEN = "t" * MIN_TOKEN_BYTES


def _env(tmp_path: Path, **overrides: str) -> dict[str, str]:
    token_file = tmp_path / "door-trigger.token"
    token_file.write_text(GOOD_TOKEN, encoding="utf-8")
    env = {ENV_ATTENDANCE_TOKEN_FILE: str(token_file)}
    env.update(overrides)
    return env


# --- execute_fire: the core, against FakeAttendance ---


def test_a_cron_firing_prints_session_and_state(capsys: pytest.CaptureFixture[str]) -> None:
    attendance = FakeAttendance(accepted=AcceptedTurn(turn="01T", state="queued", journal_seq=1))

    code = execute_fire(attendance, "scrum-lead", None, None)

    assert code == ExitCode.ACCEPTED
    out = capsys.readouterr().out
    assert out.strip().endswith(" queued")
    assert out.strip().startswith("auto-")


def test_a_running_turn_is_also_accepted(capsys: pytest.CaptureFixture[str]) -> None:
    attendance = FakeAttendance(accepted=AcceptedTurn(turn="01T", state="running", journal_seq=1))

    code = execute_fire(attendance, "scrum-lead", None, None)

    assert code == ExitCode.ACCEPTED
    assert "running" in capsys.readouterr().out


def test_a_webhook_style_fire_passes_the_trigger_name() -> None:
    attendance = FakeAttendance()

    execute_fire(attendance, "ha-review", "deploy-notify", None)

    assert attendance.requests[0].labels["trigger_name"] == "deploy-notify"


def test_a_refusal_prints_to_stderr_and_exits_refused(capsys: pytest.CaptureFixture[str]) -> None:
    attendance = FakeAttendance(turn_error=AttendanceError("queue_full", "queue is full", 429))

    code = execute_fire(attendance, "scrum-lead", None, None)

    assert code == ExitCode.REFUSED
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "queue_full" in captured.err
    assert "queue is full" in captured.err


def test_forbidden_is_also_a_refusal_not_a_crash(capsys: pytest.CaptureFixture[str]) -> None:
    attendance = FakeAttendance(ensure_error=AttendanceError("forbidden", "not autonomous", 403))

    code = execute_fire(attendance, "chat", None, None)

    assert code == ExitCode.REFUSED
    assert "forbidden" in capsys.readouterr().err


# --- main(): argument parsing, --check, and the exit code it returns ---


def test_fire_check_validates_config_and_never_calls_attendance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for key, value in _env(tmp_path).items():
        monkeypatch.setenv(key, value)

    code = main(["fire", "chat", "--check"])

    assert code == ExitCode.ACCEPTED
    assert "config OK" in capsys.readouterr().out


def test_fire_with_no_token_file_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("DOOR_TRIGGER_SESSIOND_TOKEN_FILE", raising=False)
    monkeypatch.setenv("DOOR_TRIGGER_SESSIOND_TOKEN_FILE", "/no/such/file/anywhere")

    code = main(["fire", "chat", "--check"])

    assert code == ExitCode.USAGE
    assert "refusing to start" in capsys.readouterr().err


@pytest.mark.parametrize("command", [["fire", "chat", "--check"], ["serve", "--check"]])
def test_a_token_file_that_is_not_utf8_prints_one_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: list[str],
) -> None:
    binary = tmp_path / "binary.token"
    binary.write_bytes(b"\xff\xfe" * MIN_TOKEN_BYTES)
    monkeypatch.setenv(ENV_ATTENDANCE_TOKEN_FILE, str(binary))

    code = main(command)

    assert code == ExitCode.USAGE
    err = capsys.readouterr().err
    assert err.startswith("agent-trigger: refusing to start: ")
    assert err.count("\n") == 1


def test_fire_with_a_bad_payload_file_is_a_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for key, value in _env(tmp_path).items():
        monkeypatch.setenv(key, value)
    bad_payload = tmp_path / "payload.json"
    bad_payload.write_text("{not json", encoding="utf-8")

    code = main(["fire", "chat", "--payload-file", str(bad_payload)])

    assert code == ExitCode.USAGE
    assert "refusing to fire" in capsys.readouterr().err


def test_fire_with_an_oversized_payload_file_is_a_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for key, value in _env(tmp_path).items():
        monkeypatch.setenv(key, value)
    huge_payload = tmp_path / "payload.json"
    huge_payload.write_text('{"pad": "' + "x" * 300_000 + '"}', encoding="utf-8")

    code = main(["fire", "chat", "--payload-file", str(huge_payload)])

    assert code == ExitCode.USAGE
    captured = capsys.readouterr()
    assert "refusing to fire" in captured.err
    assert "over the" in captured.err


def test_fire_cannot_reach_attendance_is_a_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for key, value in _env(tmp_path).items():
        monkeypatch.setenv(key, value)
    # No attendance is listening on the default socket inside a test sandbox.
    monkeypatch.setenv("DOOR_TRIGGER_SESSIOND_SOCKET", str(tmp_path / "no-such.sock"))

    code = main(["fire", "chat"])

    assert code == ExitCode.USAGE
    assert "cannot reach attendance" in capsys.readouterr().err


def test_serve_check_validates_config_and_never_binds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for key, value in _env(tmp_path).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv(ENV_REGISTRY_ROOT, str(tmp_path / "registry"))

    code = main(["serve", "--check"])

    assert code == ExitCode.ACCEPTED
    out = capsys.readouterr().out
    assert "config OK" in out
    assert "bind=" in out


def test_no_subcommand_is_a_usage_error() -> None:
    with pytest.raises(SystemExit):
        main([])
