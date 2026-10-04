"""The PEP's site values: its bind, TEI and Home Assistant.

The unit's `EnvironmentFile=/etc/creche/site.env` sets
`AGENT_LAN_ADDRESS` and `AGENT_HA_URL`. An explicit `PEP_BIND` or `HA_URL`
still wins. There is no default address: a missing one stops the PEP with
`os.EX_CONFIG` and a line that names the variable and the file.

A bind that the PEP does not take stops it in the same way: a text that is
not `host:port`, a port that no listener binds, no host, a host that Python
cannot give to the resolver, or a host that stands for each interface of the
host. The process then ends with one line and no traceback.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest

from chaperone import __main__ as entry
from chaperone import site

ADDRESS: Final = "192.0.2.10"
HA: Final = "http://192.0.2.20:8123"

#: A start that is still running after this has not stopped.
START_TIMEOUT_S: Final = 30

#: A `PEP_BIND` whose port part is not a port that a listener binds.
NOT_A_PORT: Final = (
    pytest.param("127.0.0.1", id="no-port"),
    pytest.param("127.0.0.1:", id="empty-port"),
    pytest.param("127.0.0.1:http", id="a-word"),
    pytest.param("127.0.0.1:8300/tcp", id="a-number-and-a-word"),
    pytest.param("127.0.0.1:-1", id="below-zero"),
    pytest.param("127.0.0.1:65536", id="past-the-largest-port"),
    pytest.param("127.0.0.1:" + "9" * 5000, id="more-digits-than-a-number-takes"),
)

#: A host that stands for each interface of the host, in each spelling.
EACH_INTERFACE: Final = (
    pytest.param("0.0.0.0", id="ipv4"),
    pytest.param("0", id="ipv4-one-number"),
    pytest.param("0.0", id="ipv4-two-numbers"),
    pytest.param("00.000.0.0", id="ipv4-octal"),
    pytest.param("0x0.0.0.0", id="ipv4-hexadecimal"),
    pytest.param("0X00", id="ipv4-one-hexadecimal-number"),
    pytest.param("::", id="ipv6"),
    pytest.param("[::]", id="ipv6-in-brackets"),
    pytest.param("0:0:0:0:0:0:0:0", id="ipv6-in-full"),
    pytest.param("::0.0.0.0", id="ipv6-with-an-ipv4-end"),
    pytest.param("::ffff:0.0.0.0", id="ipv4-in-ipv6"),
    pytest.param("[::FFFF:0:0]", id="ipv4-in-ipv6-in-brackets"),
    pytest.param("::%1", id="ipv6-with-a-zone"),
    pytest.param("\uff10.\uff10.\uff10.\uff10", id="ipv4-full-width-digits"),
    pytest.param("0\u30020\u30020\u30020", id="ipv4-full-width-dots"),
    pytest.param("\uff1a\uff1a", id="ipv6-full-width-colons"),
    pytest.param("0.0.0.0 x", id="ipv4-then-white-space"),
    pytest.param("*", id="the-star"),
)

#: A `PEP_BIND` that the PEP takes, and the host and the port that it binds.
TAKEN: Final = (
    pytest.param("127.0.0.1:9999", ("127.0.0.1", 9999), id="loopback"),
    pytest.param("192.0.2.10:8300", ("192.0.2.10", 8300), id="lan-address"),
    pytest.param("localhost:18300", ("localhost", 18300), id="host-name"),
    pytest.param("::1:18300", ("::1", 18300), id="ipv6"),
    pytest.param("127.0.0.1:0", ("127.0.0.1", 0), id="a-port-that-the-system-selects"),
    pytest.param("127.0.0.1:65535", ("127.0.0.1", 65535), id="the-largest-port"),
    pytest.param("0.0.0.1:8300", ("0.0.0.1", 8300), id="not-all-zero"),
    pytest.param("10.0.0.0:8300", ("10.0.0.0", 8300), id="zero-at-the-end"),
    pytest.param("zero.example:8300", ("zero.example", 8300), id="a-name"),
    pytest.param("[::1]:8300", ("[::1]", 8300), id="ipv6-in-brackets"),
    pytest.param("[ ]:8300", ("[ ]", 8300), id="white-space-in-brackets"),
    pytest.param("b\u00fccher.example:8300", ("b\u00fccher.example", 8300), id="a-name-not-ascii"),
)

#: A host that Python cannot give to the resolver: `socket.getaddrinfo`
#: raises on it before it asks the system.
NO_RESOLVER_TEXT: Final = (
    pytest.param("a..b", id="an-empty-label"),
    pytest.param(".a", id="an-empty-first-label"),
    pytest.param("x" * 64 + ".example", id="a-label-of-64-characters"),
)


def test_the_bind_is_the_lan_address_on_the_pep_port() -> None:
    assert site.bind({"AGENT_LAN_ADDRESS": ADDRESS}) == f"{ADDRESS}:8300"


def test_an_explicit_bind_still_wins() -> None:
    env = {"AGENT_LAN_ADDRESS": ADDRESS, "PEP_BIND": "127.0.0.1:9999"}

    assert site.bind(env) == "127.0.0.1:9999"


def test_no_lan_address_and_no_bind_names_the_variable() -> None:
    with pytest.raises(site.ConfigError) as caught:
        site.bind({})

    assert "AGENT_LAN_ADDRESS" in str(caught.value)
    assert "/etc/creche/site.env" in str(caught.value)


@pytest.mark.parametrize(("text", "listener"), TAKEN)
def test_a_bind_that_the_pep_takes(text: str, listener: tuple[str, int]) -> None:
    assert site.bind({"PEP_BIND": text}) == text
    assert site.listener({"PEP_BIND": text}) == listener


def test_the_listener_of_the_site_is_the_lan_address_on_the_pep_port() -> None:
    assert site.listener({"AGENT_LAN_ADDRESS": ADDRESS}) == (ADDRESS, 8300)


@pytest.mark.parametrize("text", NOT_A_PORT)
def test_a_bind_with_no_port_that_a_listener_binds_is_refused(text: str) -> None:
    for reader in (site.bind, site.listener):
        with pytest.raises(site.ConfigError) as caught:
            reader({"AGENT_LAN_ADDRESS": ADDRESS, "PEP_BIND": text})

        assert str(caught.value).startswith("PEP_BIND ")
        assert "host:port" in str(caught.value)


def test_a_bind_with_no_host_is_refused() -> None:
    """Not loopback in its place: the PEP has no default address."""
    for reader in (site.bind, site.listener):
        with pytest.raises(site.ConfigError) as caught:
            reader({"AGENT_LAN_ADDRESS": ADDRESS, "PEP_BIND": ":8300"})

        assert str(caught.value).startswith("PEP_BIND ")
        assert "no host" in str(caught.value)


@pytest.mark.parametrize("host", NO_RESOLVER_TEXT)
def test_a_host_that_python_cannot_give_to_the_resolver_is_refused(host: str) -> None:
    for reader in (site.bind, site.listener):
        with pytest.raises(site.ConfigError) as caught:
            reader({"AGENT_LAN_ADDRESS": ADDRESS, "PEP_BIND": f"{host}:8300"})

        assert str(caught.value).startswith("PEP_BIND ")
        assert "no resolver" in str(caught.value)


@pytest.mark.parametrize("host", EACH_INTERFACE)
def test_a_bind_on_each_interface_is_refused(host: str) -> None:
    for reader in (site.bind, site.listener):
        with pytest.raises(site.ConfigError) as caught:
            reader({"AGENT_LAN_ADDRESS": ADDRESS, "PEP_BIND": f"{host}:8300"})

        assert str(caught.value).startswith("PEP_BIND ")
        assert "each interface" in str(caught.value)


@pytest.mark.parametrize("address", ["0.0.0.0", "0", "::"])
def test_a_lan_address_of_each_interface_is_refused_as_a_bind(address: str) -> None:
    """The error names the variable that holds the address."""
    for reader in (site.bind, site.listener):
        with pytest.raises(site.ConfigError) as caught:
            reader({"AGENT_LAN_ADDRESS": address})

        assert str(caught.value).startswith("AGENT_LAN_ADDRESS ")
        assert "each interface" in str(caught.value)


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

    with caplog.at_level(logging.ERROR, logger="chaperone"):
        code = entry.main()

    assert code == os.EX_CONFIG
    lines = [one.getMessage() for one in caplog.records]
    assert len(lines) == 1, lines
    assert "AGENT_LAN_ADDRESS" in lines[0]
    assert "/etc/creche/site.env" in lines[0]


def _start(tmp_path: Path, **pep_env: str) -> subprocess.CompletedProcess[str]:
    """`python -m chaperone` as the unit starts it, with only `pep_env` set
    of the variables of the PEP and of the site."""
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("PEP_", "AGENT_")) and name != "HA_URL"
    }
    env |= {
        "PEP_REWORK_DIR": str(tmp_path / "rework"),
        "PEP_AUDIT_DIR": str(tmp_path / "audit"),
        **pep_env,
    }

    return subprocess.run(
        [sys.executable, "-m", "chaperone"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=START_TIMEOUT_S,
    )


@pytest.mark.slow
@pytest.mark.parametrize(
    "bind",
    [
        pytest.param("127.0.0.1", id="no-port"),
        pytest.param("127.0.0.1:http", id="a-word-for-a-port"),
        pytest.param("127.0.0.1:65536", id="past-the-largest-port"),
        pytest.param(":0", id="no-host"),
        pytest.param("0.0.0.0:0", id="each-interface"),
        pytest.param("a..b:0", id="a-host-with-an-empty-label"),
    ],
)
def test_the_pep_stops_with_ex_config_and_one_line_on_a_bind_it_does_not_take(
    tmp_path: Path, bind: str
) -> None:
    """The real entry point in a child, because the journal gets the whole
    stderr of the process: the line, and a traceback if one is written."""
    done = _start(tmp_path, AGENT_LAN_ADDRESS=ADDRESS, PEP_BIND=bind)

    assert done.returncode == os.EX_CONFIG, done.stderr
    assert "Traceback" not in done.stderr
    lines = done.stderr.splitlines()
    assert len(lines) == 1, lines
    assert "PEP_BIND" in lines[0]
    assert "the PEP stops" in lines[0]
