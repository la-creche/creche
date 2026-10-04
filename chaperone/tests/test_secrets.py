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


#: A value that the decrypted file holds. It must reach no error text.
LEAK = "LEAK-5d1e77a0"

#: The pairs of the mapping that `_merged` merges, and the count of merges
#: that copies as many pairs as the reader takes.
PAIRS = 256
MERGES_AT_THE_LIMIT = 256


#: Where `_merged` puts its merge key: in the file itself, or in the value
#: of one name. `{merge}` stands for the merge key and its value.
IN_THE_FILE = "{merge}\n"
IN_A_VALUE = "other: {{{merge}}}\n"


def _merged(merges: int, place: str) -> str:
    """A monolith that merges one mapping of `PAIRS` names `merges` times,
    at `place`."""
    names = ", ".join(f"k{n}: {LEAK}" for n in range(PAIRS))
    aliases = ", ".join(["*a"] * merges)

    return f"base: &a {{{names}}}\n" + place.format(merge=f"<<: [{aliases}]")


def test_merge_keys_at_the_limit_read(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(subprocess, "run", _fake_sops(_merged(MERGES_AT_THE_LIMIT, IN_THE_FILE)))

    found = load_sops_secrets(tmp_path / "s.enc.yaml")

    assert found == {f"k{n}": LEAK for n in range(PAIRS)}


@pytest.mark.parametrize("place", [IN_THE_FILE, IN_A_VALUE], ids=["in-the-file", "in-a-value"])
def test_merge_keys_past_the_limit_are_a_file_that_will_not_parse(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, place: str
) -> None:
    """The error names a line and nothing of the content, as each other
    error of this reader does."""
    text = _merged(MERGES_AT_THE_LIMIT + 1, place)
    monkeypatch.setattr(subprocess, "run", _fake_sops(text))

    with pytest.raises(SecretsFormatError) as caught:
        load_sops_secrets(tmp_path / "s.enc.yaml")

    assert "merge keys past a limit at line 1, column 7" in str(caught.value)
    assert LEAK not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__
