"""The component's verify hook (contract 06 §4, `managerd/component.yaml`)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from agent_managerd import paths
from agent_managerd.clock import rfc3339
from agent_managerd.status import STALE_AFTER_S
from agent_managerd.verify import EXIT_FAILED, EXIT_OK, main

NOW = datetime(2026, 9, 19, 12, 0, 0, tzinfo=UTC)


def state_root(tmp_path: Path) -> Path:
    root = tmp_path / "rework"
    (root / paths.FAMILIES_DIR).mkdir(parents=True)

    return root


def write_status(root: Path, family: str, written_at: datetime) -> None:
    path = paths.status_path(root, family)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"family": family, "written_at": rfc3339(written_at)}
    path.write_text(json.dumps(body), encoding="utf-8")


def run(monkeypatch: pytest.MonkeyPatch, root: Path) -> int:
    monkeypatch.setenv("MANAGERD_STATE_ROOT", str(root))
    monkeypatch.setattr("agent_managerd.verify.datetime", _FixedClock)

    return main(["--json"])


class _FixedClock(datetime):
    """`datetime.now` pinned, so an age is arithmetic and never a race."""

    @classmethod
    def now(cls, tz: object = None) -> datetime:  # type: ignore[override]
        return NOW


def body(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    return json.loads(capsys.readouterr().out)


def failed(capsys: pytest.CaptureFixture[str]) -> list[str]:
    checks: list[dict[str, object]] = body(capsys)["checks"]  # type: ignore[assignment]

    return [str(one["name"]) for one in checks if not one["ok"]]


def test_a_fresh_heartbeat_passes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = state_root(tmp_path)
    write_status(root, "chat", NOW - timedelta(seconds=5))

    assert run(monkeypatch, root) == EXIT_OK
    assert body(capsys)["ok"] is True


def test_a_stale_document_fails_and_names_the_family(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Contract 05 §2 rule 5: past the window the reconciler is not running."""
    root = state_root(tmp_path)
    write_status(root, "chat", NOW - timedelta(seconds=STALE_AFTER_S + 1))

    assert run(monkeypatch, root) == EXIT_FAILED
    assert failed(capsys) == ["heartbeat"]


def test_one_stale_family_among_fresh_ones_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = state_root(tmp_path)
    write_status(root, "chat", NOW - timedelta(seconds=1))
    write_status(root, "vault-oracle", NOW - timedelta(seconds=STALE_AFTER_S + 60))

    assert run(monkeypatch, root) == EXIT_FAILED
    checks: list[dict[str, object]] = body(capsys)["checks"]  # type: ignore[assignment]
    assert "vault-oracle" in str(checks[1]["detail"])
    assert "chat" not in str(checks[1]["detail"])


def test_a_host_with_no_family_passes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A release must not be blocked because nothing is declared yet."""
    assert run(monkeypatch, state_root(tmp_path)) == EXIT_OK
    assert body(capsys)["ok"] is True


def test_an_env_file_supplies_the_state_root(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Fault 2: root's executor hands the hook a fixed,
    minimal environment that carries none of `agent-managerd.service`'s
    own `EnvironmentFile=`. `--env-file` is the same file the unit's own
    `EnvironmentFile=` names, read by this hook itself — never the
    process env alone."""
    monkeypatch.delenv("MANAGERD_STATE_ROOT", raising=False)
    monkeypatch.setattr("agent_managerd.verify.datetime", _FixedClock)
    root = state_root(tmp_path)
    write_status(root, "chat", NOW - timedelta(seconds=5))
    env_file = tmp_path / "managerd.env"
    env_file.write_text(f"MANAGERD_STATE_ROOT={root}\n", encoding="utf-8")

    code = main(["--json", "--env-file", str(env_file)])

    assert code == EXIT_OK
    assert body(capsys)["ok"] is True


def test_a_missing_env_file_fails_closed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A file the manifest promised and the host does not have is a hook
    failure, never a silent fall-back to `paths.STATE_ROOT`."""
    monkeypatch.delenv("MANAGERD_STATE_ROOT", raising=False)

    code = main(["--json", "--env-file", str(tmp_path / "absent.env")])

    assert code == EXIT_FAILED
    assert failed(capsys) == ["env-file"]


def test_a_missing_state_root_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    assert run(monkeypatch, tmp_path / "absent") == EXIT_FAILED
    assert failed(capsys) == ["state root"]


def test_an_unreadable_stamp_counts_as_stale(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = state_root(tmp_path)
    path = paths.status_path(root, "chat")
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")

    assert run(monkeypatch, root) == EXIT_FAILED
    assert failed(capsys) == ["heartbeat"]
