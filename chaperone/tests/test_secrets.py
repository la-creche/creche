"""A decrypted secret value that is not a string (a mistyped YAML
scalar — a bare number, bool, or null) is dropped rather than silently
becoming a "missing secret" later; the drop is logged, naming its line and
never its key (a stray colon makes a value a key)."""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from chaperone.secrets import SecretsError, SecretsFormatError, load_sops_secrets

from chaperone import secrets as secrets_module


def _fake_sops(stdout: str, returncode: int = 0) -> Callable[..., subprocess.CompletedProcess[str]]:
    def run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=(), returncode=returncode, stdout=stdout, stderr="")

    return run


def test_string_values_pass_through(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(subprocess, "run", _fake_sops("kagi_api_key: abc123\n"))
    assert load_sops_secrets(tmp_path / "s.enc.yaml") == {"kagi_api_key": "abc123"}


def test_non_string_value_is_dropped_and_logged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(subprocess, "run", _fake_sops("smtp_password: 12345\nkagi_api_key: abc\n"))
    with caplog.at_level(logging.WARNING, logger=secrets_module.log.name):
        result = load_sops_secrets(tmp_path / "s.enc.yaml")

    assert result == {"kagi_api_key": "abc"}
    assert "line 1: dropping an entry" in caplog.text
    assert "smtp_password" not in caplog.text


def test_non_mapping_content_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(subprocess, "run", _fake_sops("- just\n- a\n- list\n"))
    with pytest.raises(SecretsFormatError, match="mapping"):
        load_sops_secrets(tmp_path / "s.enc.yaml")


def test_sops_failure_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(subprocess, "run", _fake_sops("", returncode=1))
    with pytest.raises(SecretsError):
        load_sops_secrets(tmp_path / "s.enc.yaml")
