"""The host's LAN address, and the plane endpoints on it.

The address is the site's: the unit's
`EnvironmentFile=/etc/creche/site.env` sets `AGENT_LAN_ADDRESS`.
The ports are this code's. There is no default address: a default would be
somebody's host, so a missing one stops with the variable's name.

    AGENT_LAN_ADDRESS=192.0.2.10   ->   endpoint(Port.PEP) == "192.0.2.10:8300"

Read when a value is built, never at import: a module-level read would
break every import in a test.
"""

from __future__ import annotations

import os
from enum import IntEnum
from typing import Final

LAN_ADDRESS_ENV: Final = "AGENT_LAN_ADDRESS"


class Port(IntEnum):
    """What answers on the LAN address, and where."""

    #: Contract 01 §3.7 rule 4.
    LITELLM = 4000
    PEP = 8300
    #: Contract 02 §3 rule 9: `attendance`'s optional LAN port.
    ATTENDANCE = 8350
    #: Open WebUI: a LAN neighbour no sandbox has business reaching.
    OPEN_WEBUI = 8181


class ConfigError(RuntimeError):
    """The environment names no LAN address."""


def lan_address() -> str:
    """`AGENT_LAN_ADDRESS`, or `ConfigError` naming it."""
    address = os.environ.get(LAN_ADDRESS_ENV, "").strip()
    if not address:
        raise ConfigError(f"{LAN_ADDRESS_ENV} is not set (/etc/creche/site.env)")

    return address


def endpoint(port: Port) -> str:
    """`host:port`, the form an egress allow names."""
    return f"{lan_address()}:{int(port)}"


def url(port: Port) -> str:
    """`http://host:port`, the form a client dials."""
    return f"http://{endpoint(port)}"
