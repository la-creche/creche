"""The PEP's site values: its bind, TEI and Home Assistant.

The unit's `EnvironmentFile=/etc/agent-control/site.env` sets
`AGENT_LAN_ADDRESS` and `AGENT_HA_URL`. An explicit `PEP_BIND` or `HA_URL`
still wins. There is no default address: a missing one stops the PEP with
`os.EX_CONFIG` and a line that names the variable and the file.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Final

import pytest
from agent_pep import __main__ as entry
from agent_pep import site

ADDRESS: Final = "192.0.2.10"
HA: Final = "http://192.0.2.20:8123"


def test_the_bind_is_the_lan_address_on_the_pep_port() -> None:
    assert site.bind({"AGENT_LAN_ADDRESS": ADDRESS}) == f"{ADDRESS}:8300"


def test_an_explicit_bind_still_wins() -> None:
    env = {"AGENT_LAN_ADDRESS": ADDRESS, "PEP_BIND": "127.0.0.1:9999"}

    assert site.bind(env) == "127.0.0.1:9999"


def test_no_lan_address_and_no_bind_names_the_variable() -> None:
    with pytest.raises(site.ConfigError) as caught:
        site.bind({})

    assert "AGENT_LAN_ADDRESS" in str(caught.value)
    assert "/etc/agent-control/site.env" in str(caught.value)


def test_tei_answers_on_the_lan_address() -> None:
    assert site.tei_url({"AGENT_LAN_ADDRESS": ADDRESS}) == f"http://{ADDRESS}:8085"


def test_with_no_lan_address_there_is_no_tei_url() -> None:
    """Only a PEP started with `PEP_BIND` and no site gets here. `embed`
    then refuses, rather than dial a relative URL."""
    assert site.tei_url({}) == ""


def test_home_assistant_is_the_sites() -> None:
    assert site.ha_url({"AGENT_HA_URL": HA}) == HA


def test_an_explicit_ha_url_still_wins() -> None:
    env = {"AGENT_HA_URL": HA, "HA_URL": "http://192.0.2.30:8123"}

    assert site.ha_url(env) == "http://192.0.2.30:8123"


def test_a_site_with_no_home_assistant_has_no_ha_url() -> None:
    assert site.ha_url({}) == ""


def test_the_pep_stops_with_ex_config_when_the_site_names_no_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Exactly as `_unparsable` stops: one line, `status=78/CONFIG`."""
    monkeypatch.setenv("PEP_REWORK_DIR", str(tmp_path / "rework"))
    monkeypatch.setenv("PEP_AUDIT_DIR", str(tmp_path / "audit"))
    for name in ("AGENT_LAN_ADDRESS", "PEP_BIND", "PEP_SECRETS", "PEP_SECRETS_DIR"):
        monkeypatch.delenv(name, raising=False)

    with caplog.at_level(logging.ERROR, logger="agent_pep"):
        code = entry.main()

    assert code == os.EX_CONFIG
    lines = [one.getMessage() for one in caplog.records]
    assert len(lines) == 1, lines
    assert "AGENT_LAN_ADDRESS" in lines[0]
    assert "/etc/agent-control/site.env" in lines[0]
