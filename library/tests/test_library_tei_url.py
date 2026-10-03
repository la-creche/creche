"""Where `index-scope` finds TEI: `TEI_URL`, else the site's LAN address."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from library.__main__ import (
    LAN_ADDRESS_ENV,
    TEI_PORT,
    TEI_URL_ENV,
    ConfigError,
    main,
    tei_url,
)


def test_the_url_is_built_from_the_lan_address() -> None:
    address = os.environ[LAN_ADDRESS_ENV]

    assert tei_url({LAN_ADDRESS_ENV: address}) == f"http://{address}:{TEI_PORT}"


def test_an_explicit_tei_url_still_wins() -> None:
    assert tei_url({TEI_URL_ENV: "http://tei.test:1"}) == "http://tei.test:1"


def test_a_missing_lan_address_names_the_variable() -> None:
    with pytest.raises(ConfigError, match=LAN_ADDRESS_ENV):
        tei_url({})


def test_main_stops_before_any_call_without_an_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(LAN_ADDRESS_ENV)
    monkeypatch.delenv(TEI_URL_ENV, raising=False)
    monkeypatch.setattr("sys.argv", ["index-scope", str(tmp_path), str(tmp_path / "index")])

    assert main() == 2
    assert LAN_ADDRESS_ENV in capsys.readouterr().err
