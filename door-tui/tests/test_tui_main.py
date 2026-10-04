"""The command line: `agent-tui <family>` and `--check`.

`--check` is the one path that calls neither `attendance` nor `sbx`, so a bad
token file is caught before the operator is in front of a terminal that will not
open.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from agent_door_tui.__main__ import main, parse_args, request_of
from agent_door_tui.app import Want
from agent_door_tui.attendance import Takeover
from agent_door_tui.config import ENV_FAMILIES_DIR, ENV_TOKEN_FILE
from agent_door_tui.errors import Exit

TOKEN = "door-tui-token-" + "t" * 32


def with_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "door-tui.token"
    path.write_text(TOKEN + "\n", encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setenv(ENV_TOKEN_FILE, str(path))
    monkeypatch.setenv(ENV_FAMILIES_DIR, str(tmp_path / "families"))

    return path


def test_check_validates_and_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with_token(tmp_path, monkeypatch)

    assert main(["--check"]) == int(Exit.OK)

    out = capsys.readouterr().out
    assert "config OK" in out
    assert TOKEN not in out, "the token must never reach a line anyone reads"


def test_check_refuses_a_missing_token_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(ENV_TOKEN_FILE, str(tmp_path / "nothing.token"))

    assert main(["--check"]) == int(Exit.BAD_USAGE)
    assert "refusing to start" in capsys.readouterr().err


def test_check_refuses_a_token_file_that_is_not_utf8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "binary.token"
    path.write_bytes(b"\xff\xfe" * 32)
    monkeypatch.setenv(ENV_TOKEN_FILE, str(path))

    assert main(["--check"]) == int(Exit.BAD_USAGE)

    err = capsys.readouterr().err
    assert err.startswith("agent-tui: refusing to start: ")
    assert err.count("\n") == 1


def test_no_family_and_no_check_says_what_to_do(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with_token(tmp_path, monkeypatch)

    assert main([]) == int(Exit.BAD_USAGE)
    assert "--help" in capsys.readouterr().err


def test_session_and_new_together_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_token(tmp_path, monkeypatch)

    with pytest.raises(SystemExit):
        main(["chat", "--session", "tui-01JB", "--new"])


def test_an_unknown_family_refuses_without_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No status document means no family. The door says so and exits 66."""
    with_token(tmp_path, monkeypatch)

    assert main(["nosuchfamily"]) == int(Exit.NO_SANDBOX)
    assert "nosuchfamily" in capsys.readouterr().err


def test_a_bad_family_name_never_builds_a_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with_token(tmp_path, monkeypatch)

    assert main(["../../etc"]) == int(Exit.BAD_USAGE)
    assert "family name" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("argv", "want"),
    [
        (["chat"], Want.PICK),
        (["chat", "--new"], Want.NEW),
        (["chat", "--session", "tui-01JB"], Want.NAMED),
    ],
)
def test_the_command_line_picks_the_right_want(argv: list[str], want: Want) -> None:
    request = request_of(parse_args(argv))

    assert request.want is want
    assert request.family == "chat"
    assert request.takeover is Takeover.POLITE


def test_force_reaches_the_request_only_when_typed() -> None:
    assert request_of(parse_args(["chat", "--force"])).takeover is Takeover.FORCE
    assert request_of(parse_args(["chat"])).takeover is Takeover.POLITE
