"""The plane addresses come from the site's `AGENT_LAN_ADDRESS`, an explicit
value still wins, and a missing address stops with the variable's name."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from agent_managerd.cli import EXIT_OK, EXIT_USAGE, main
from agent_managerd.egress import EgressConfig
from agent_managerd.lan import LAN_ADDRESS_ENV, ConfigError, Port, endpoint, url
from managerd_helpers import write_registry

IMAGE = "sha256:deadbeef"


def _address() -> str:
    return os.environ[LAN_ADDRESS_ENV]


def _serve_plan(tmp_path: Path, *extra: str) -> int:
    registry = tmp_path / "registry"
    write_registry(registry)

    return main(
        ["serve", str(registry), "--image", IMAGE, "--state-root", str(tmp_path / "s"), *extra]
    )


def test_an_endpoint_is_the_lan_address_and_the_port() -> None:
    assert endpoint(Port.PEP) == f"{_address()}:{int(Port.PEP)}"
    assert url(Port.LITELLM) == f"http://{_address()}:{int(Port.LITELLM)}"


def test_a_missing_address_names_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(LAN_ADDRESS_ENV)

    with pytest.raises(ConfigError, match=LAN_ADDRESS_ENV):
        endpoint(Port.PEP)


def test_the_planes_are_on_the_lan_address() -> None:
    planes = EgressConfig().allowed(())

    assert planes == (endpoint(Port.LITELLM), endpoint(Port.PEP))


def test_explicit_endpoints_need_no_address(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(LAN_ADDRESS_ENV)

    config = EgressConfig(litellm="litellm.test:4000", pep="pep.test:8300", canaries=())

    assert config.allowed(()) == ("litellm.test:4000", "pep.test:8300")


def test_serve_watches_the_pep_on_the_lan_address(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _serve_plan(tmp_path) == EXIT_OK

    printed = capsys.readouterr().out
    assert f"pep watch: {url(Port.PEP)}," in printed
    assert f"sessiond: {url(Port.SESSIOND)}," in printed


def test_an_explicit_pep_url_still_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(LAN_ADDRESS_ENV)
    socket = str(tmp_path / "sessiond.sock")

    code = _serve_plan(tmp_path, "--pep-url", "http://pep.test:1", "--sessiond-socket", socket)

    assert code == EXIT_OK
    assert "pep watch: http://pep.test:1," in capsys.readouterr().out


def test_serve_with_no_address_names_the_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(LAN_ADDRESS_ENV)

    assert _serve_plan(tmp_path) == EXIT_USAGE
    assert LAN_ADDRESS_ENV in capsys.readouterr().err
