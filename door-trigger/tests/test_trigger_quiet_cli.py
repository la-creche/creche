"""The quiet check inside `agent-trigger fire` (contract 01 §3.15): what
the journal line says, what reaches `attendance`, and what the gate keeps."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest
from agent_door_trigger.attendance import JOBS_LIMIT, AttendanceTarget, HttpAttendance
from agent_door_trigger.cli import execute_fire, main
from agent_door_trigger.config import (
    ENV_ATTENDANCE_TOKEN_FILE,
    ENV_LAN_ADDRESS,
    MIN_TOKEN_BYTES,
    PEP_PORT,
)
from agent_door_trigger.errors import AttendanceError, ExitCode
from agent_door_trigger.quiet.records import Ending
from trigger_fake_attendance import FakeAttendance
from trigger_quiet_fakes import FAMILY, Rig


def test_a_quiet_firing_starts_nothing_and_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    rig = Rig()
    rig.gate().record("auto-FIRST", rig.gate().check())
    rig.records.endings["auto-FIRST"] = Ending.OK
    attendance = FakeAttendance()

    code = execute_fire(attendance, FAMILY, None, None, rig.gate())

    assert code == ExitCode.ACCEPTED
    assert attendance.jobs == []
    assert capsys.readouterr().out.startswith(f"quiet: {FAMILY}: nothing changed since ")


def test_a_woken_firing_fires_and_keeps_its_session(capsys: pytest.CaptureFixture[str]) -> None:
    rig = Rig()
    attendance = FakeAttendance()

    code = execute_fire(attendance, FAMILY, None, None, rig.gate())

    assert code == ExitCode.ACCEPTED
    wake_line, fire_line = capsys.readouterr().out.splitlines()
    assert wake_line == f"wake: {FAMILY}: no good wake on record"
    session = fire_line.split()[0]
    assert rig.state.fired is not None
    assert rig.state.fired.session == session
    assert [job.family for job in attendance.jobs] == [FAMILY]


def test_a_refused_firing_keeps_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    rig = Rig()
    attendance = FakeAttendance(error=AttendanceError("queue_full", "queue is full", 429))

    code = execute_fire(attendance, FAMILY, None, None, rig.gate())

    assert code == ExitCode.REFUSED
    assert rig.state.fired is None
    assert "queue_full" in capsys.readouterr().err


def test_force_is_a_fire_flag(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    token = tmp_path / "door-trigger.token"
    token.write_text("t" * MIN_TOKEN_BYTES, encoding="utf-8")

    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(ENV_ATTENDANCE_TOKEN_FILE, str(token))
        code = main(["fire", FAMILY, "--force", "--check"])

    assert code == ExitCode.ACCEPTED
    assert f"chaperone=http://{os.environ[ENV_LAN_ADDRESS]}:{PEP_PORT}" in capsys.readouterr().out


# --- HttpAttendance.any_live: the trigger jobs of the family, as the gate asks ---


def _attendance(handler: Any) -> HttpAttendance:
    client = httpx.Client(base_url="http://sessiond", transport=httpx.MockTransport(handler))
    target = AttendanceTarget(url="http://sessiond", socket=None, token="secret-token")
    return HttpAttendance(target, client=client)


def _jobs(*statuses: str) -> dict[str, object]:
    return {"jobs": [{"session": f"auto-{n}", "status": one} for n, one in enumerate(statuses)]}


def test_a_running_job_is_live() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=_jobs("ended", "running"))

    assert _attendance(handler).any_live(FAMILY) is True
    assert seen == {
        "path": "/trigger/jobs",
        "body": {"family": FAMILY, "limit": JOBS_LIMIT},
        "auth": "Bearer secret-token",
    }


def test_a_queued_job_is_live() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_jobs("queued"))

    assert _attendance(handler).any_live(FAMILY) is True


def test_only_ended_jobs_are_not_live() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_jobs("ended", "ended"))

    assert _attendance(handler).any_live(FAMILY) is False


def test_a_refused_list_is_unknown() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"code": "forbidden", "message": "no"}})

    assert _attendance(handler).any_live(FAMILY) is None
