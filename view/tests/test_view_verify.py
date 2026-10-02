"""The component's verify hook (contract 06 §4, `view/component.yaml`)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from agent_view.config import MIN_KEY_BYTES
from agent_view.verify import EXIT_FAILED, EXIT_OK, main

KEY = "k" * MIN_KEY_BYTES


def run(monkeypatch: pytest.MonkeyPatch, values: dict[str, str], *argv: str) -> int:
    monkeypatch.setattr("os.environ", values)
    return main(list(argv))


def env(tmp_path: Path) -> dict[str, str]:
    return {
        "VIEW_BIND": "127.0.0.1",
        "VIEW_STATE_ROOT": str(tmp_path / "state"),
        "VIEW_REGISTRY_DIR": str(tmp_path / "registry"),
    }


def test_a_bad_configuration_fails_with_one_check(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code = run(monkeypatch, {"VIEW_BIND": "192.0.2.10"}, "--json")

    assert code == EXIT_FAILED
    body = json.loads(capsys.readouterr().out)
    assert body["ok"] is False
    assert body["checks"][0]["name"] == "config"


def test_a_missing_directory_fails_and_names_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    code = run(monkeypatch, env(tmp_path), "--json")

    assert code == EXIT_FAILED
    body = json.loads(capsys.readouterr().out)
    failed = [one["name"] for one in body["checks"] if not one["ok"]]
    assert "families" in failed
    assert "healthz" in failed


def test_the_hook_never_prints_the_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Invariant 13. /healthz is keyless, so the hook needs no secret."""
    values = env(tmp_path)
    values["VIEW_ACCESS_KEY"] = KEY

    run(monkeypatch, values, "--json")

    assert KEY not in capsys.readouterr().out


def test_lines_are_the_default_and_json_is_a_flag(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    run(monkeypatch, env(tmp_path))

    printed = capsys.readouterr().out
    assert printed.startswith("PASS config:")
    assert "FAIL families:" in printed


def test_an_env_file_supplies_what_the_process_env_does_not(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Root's executor hands the hook a fixed,
    minimal environment that carries none of the unit's own
    `EnvironmentFile=`. Without `--env-file`, that answers the right
    refusal (an unkeyed LAN bind) for the wrong reason on every release —
    `--env-file` is what lets this check pass with the same key the unit
    itself has."""
    env_file = tmp_path / "view.env"
    # The state root is pointed at a directory that does not exist, so the
    # `families` check fails on EVERY machine. Without that line the hook
    # passed on the host itself (a real state root and a live view), and the
    # exit-code assertion below held only on a machine that is not the host.
    env_file.write_text(
        f"VIEW_BIND=192.0.2.10\nVIEW_ACCESS_KEY={KEY}\nVIEW_STATE_ROOT={tmp_path / 'absent'}\n",
        encoding="utf-8",
    )

    code = run(monkeypatch, {}, "--json", "--env-file", str(env_file))

    assert code == EXIT_FAILED
    checks = {str(one["name"]): one for one in json.loads(capsys.readouterr().out)["checks"]}
    assert checks["config"]["ok"] is True
    assert checks["families"]["ok"] is False


def test_a_missing_env_file_fails_closed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A file the manifest promised and the host does not have is a hook
    failure, never a silent fall-back to the (wrong) hardcoded default."""
    code = run(monkeypatch, {}, "--json", "--env-file", str(tmp_path / "absent.env"))

    assert code == EXIT_FAILED
    checks = json.loads(capsys.readouterr().out)["checks"]
    assert checks == [{"name": "env-file", "ok": False, "detail": checks[0]["detail"]}]
    assert str(tmp_path / "absent.env") in str(checks[0]["detail"])


def test_a_healthy_service_passes_every_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The one call the hook makes, answered by a stand-in on loopback."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from view_helpers import make_registry, make_state_root

    class Healthz(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok": true}')

        def log_message(self, format: str, *args: object) -> None:
            """Silent. The stdlib's default writes every request to stderr."""
            return

    state = make_state_root(tmp_path)
    (state / "tokens").mkdir(parents=True, exist_ok=True)
    (state / "tokens" / "view-ro.token").write_text("v" * 40, encoding="utf-8")
    registry = make_registry(tmp_path)
    server = HTTPServer(("127.0.0.1", 0), Healthz)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    try:
        code = run(
            monkeypatch,
            {
                "VIEW_BIND": "127.0.0.1",
                "VIEW_PORT": str(server.server_port),
                "VIEW_STATE_ROOT": str(state),
                "VIEW_REGISTRY_DIR": str(registry),
            },
            "--json",
        )
    finally:
        server.shutdown()

    assert code == EXIT_OK
