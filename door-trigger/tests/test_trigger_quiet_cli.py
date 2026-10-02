"""The quiet check inside `agent-trigger fire` (contract 01 §3.15): what
the journal line says, what reaches `sessiond`, and what the gate keeps."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest
from agent_door_trigger.cli import execute_fire, main
from agent_door_trigger.config import (
    ENV_LAN_ADDRESS,
    ENV_SESSIOND_TOKEN_FILE,
    MIN_TOKEN_BYTES,
    PEP_PORT,
)
from agent_door_trigger.errors import ExitCode, SessiondError
from agent_door_trigger.quiet.records import Ending
from agent_door_trigger.sessiond import HttpSessiond, SessiondTarget
from trigger_fake_sessiond import FakeSessiond
from trigger_quiet_fakes import FAMILY, Rig


def test_a_quiet_firing_starts_nothing_and_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    rig = Rig()
    rig.gate().record("auto-FIRST", rig.gate().check())
    rig.records.endings["auto-FIRST"] = Ending.OK
    sessiond = FakeSessiond()

    code = execute_fire(sessiond, FAMILY, None, None, rig.gate())

    assert code == ExitCode.ACCEPTED
    assert sessiond.requests == []
    assert capsys.readouterr().out.startswith(f"quiet: {FAMILY}: nothing changed since ")


def test_a_woken_firing_fires_and_keeps_its_session(capsys: pytest.CaptureFixture[str]) -> None:
    rig = Rig()
    sessiond = FakeSessiond()

    code = execute_fire(sessiond, FAMILY, None, None, rig.gate())

    assert code == ExitCode.ACCEPTED
    wake_line, fire_line = capsys.readouterr().out.splitlines()
    assert wake_line == f"wake: {FAMILY}: no good wake on record"
    session = fire_line.split()[0]
    assert rig.state.fired is not None
    assert rig.state.fired.session == session
    assert sessiond.ensured == [(FAMILY, session)]


def test_a_refused_firing_keeps_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    rig = Rig()
    sessiond = FakeSessiond(turn_error=SessiondError("queue_full", "queue is full", 429))

    code = execute_fire(sessiond, FAMILY, None, None, rig.gate())

    assert code == ExitCode.REFUSED
    assert rig.state.fired is None
    assert "queue_full" in capsys.readouterr().err


def test_force_is_a_fire_flag(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    token = tmp_path / "door-trigger.token"
    token.write_text("t" * MIN_TOKEN_BYTES, encoding="utf-8")

    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(ENV_SESSIOND_TOKEN_FILE, str(token))
        code = main(["fire", FAMILY, "--force", "--check"])

    assert code == ExitCode.ACCEPTED
    assert f"pep=http://{os.environ[ENV_LAN_ADDRESS]}:{PEP_PORT}" in capsys.readouterr().out


# --- HttpSessiond.any_live: contract 02 §5.2, as the gate asks it ---


def _sessiond(handler: Any) -> HttpSessiond:
    client = httpx.Client(base_url="http://sessiond", transport=httpx.MockTransport(handler))
    target = SessiondTarget(url="http://sessiond", socket=None, token="secret-token")
    return HttpSessiond(target, client=client)


def test_a_listed_session_is_live() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = dict(request.url.params)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"sessions": [{"session": "auto-A"}], "next_cursor": None})

    assert _sessiond(handler).any_live(FAMILY) is True
    assert seen == {
        "path": "/v1/sessions",
        "query": {"family": FAMILY, "limit": "1"},
        "auth": "Bearer secret-token",
    }


def test_no_listed_session_is_not_live() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps({"sessions": [], "next_cursor": None}))

    assert _sessiond(handler).any_live(FAMILY) is False


def test_a_refused_list_is_unknown() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"code": "forbidden", "message": "no"}})

    assert _sessiond(handler).any_live(FAMILY) is None
