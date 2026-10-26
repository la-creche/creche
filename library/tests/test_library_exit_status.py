"""The exit status of `index-scope` for a start that it refuses.

A command line that the program refuses ends with status 2. A start with no
address of TEI ends with status 78, the status of a config that is not
valid. The program reads its command line first, so a refused command line
ends with status 2 with an address and with none.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest
from library.__main__ import LAN_ADDRESS_ENV, TEI_URL_ENV, main

#: The status of a command line that the program refuses.
EXIT_USAGE = 2

#: The status of a config that is not valid: `EX_CONFIG` of `sysexits.h`.
EXIT_CONFIG = 78

PROGRAM = "index-scope"

#: An address that no test dials: each refused start ends before a call.
URL = "http://tei.test:1"

#: The two environments of each refused command line: with an address of
#: TEI, and with none.
with_and_without_address = pytest.mark.parametrize("address", [URL, None], ids=["address", "none"])


@pytest.fixture(autouse=True)
def _no_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test of a refused start that makes an HTTP client."""

    def client(**options: float) -> httpx.Client:
        raise AssertionError("a refused start makes no call")

    monkeypatch.setattr("library.__main__.httpx.Client", client)


def run(monkeypatch: pytest.MonkeyPatch, words: list[str], address: str | None) -> int:
    """The status of `index-scope <words>` with that address of TEI."""
    monkeypatch.delenv(LAN_ADDRESS_ENV, raising=False)
    monkeypatch.delenv(TEI_URL_ENV, raising=False)
    if address is not None:
        monkeypatch.setenv(TEI_URL_ENV, address)

    monkeypatch.setattr("sys.argv", [PROGRAM, *words])

    return main()


def test_ex_config_of_this_platform_is_78() -> None:
    assert os.EX_CONFIG == EXIT_CONFIG


@pytest.mark.parametrize("profile", [[], ["vault"]], ids=["default", "vault"])
def test_no_address_of_tei_ends_with_status_78(
    profile: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    words = [str(tmp_path), str(tmp_path / "index"), *profile]

    assert run(monkeypatch, words, None) == EXIT_CONFIG
    assert LAN_ADDRESS_ENV in capsys.readouterr().err
    assert not (tmp_path / "index").exists()


@pytest.mark.parametrize("value", ["", "  "], ids=["empty", "blank"])
def test_an_address_with_no_character_is_no_address(
    value: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(LAN_ADDRESS_ENV, value)
    monkeypatch.setenv(TEI_URL_ENV, value)
    monkeypatch.setattr("sys.argv", [PROGRAM, str(tmp_path), str(tmp_path / "index")])

    assert main() == EXIT_CONFIG


@with_and_without_address
@pytest.mark.parametrize("count", [0, 1, 4])
def test_a_wrong_count_of_words_ends_with_status_2(
    count: int,
    address: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    words = [str(tmp_path), str(tmp_path / "index"), "vault", "more"][:count]

    assert run(monkeypatch, words, address) == EXIT_USAGE
    assert "usage: index-scope" in capsys.readouterr().err


@with_and_without_address
@pytest.mark.parametrize("profile", ["prose", "VAULT", "", "code"])
def test_an_unknown_profile_ends_with_status_2(
    profile: str,
    address: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    words = [str(tmp_path), str(tmp_path / "index"), profile]

    assert run(monkeypatch, words, address) == EXIT_USAGE
    assert "unknown profile" in capsys.readouterr().err


@with_and_without_address
@pytest.mark.parametrize("kind", ["absent", "file"])
def test_a_scope_that_is_no_directory_ends_with_status_2(
    kind: str,
    address: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scope = tmp_path / kind
    if kind == "file":
        scope.write_text("a file is no corpus")

    assert run(monkeypatch, [str(scope), str(tmp_path / "index")], address) == EXIT_USAGE
    assert "not a directory" in capsys.readouterr().err
