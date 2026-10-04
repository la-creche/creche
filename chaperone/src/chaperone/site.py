"""The site's values the PEP reads: its bind, TEI and Home Assistant.

The address is the site's: the unit's
`EnvironmentFile=/etc/creche/site.env` sets `AGENT_LAN_ADDRESS`, and
`AGENT_HA_URL` when the site has a Home Assistant. The ports are this
code's. An explicit `PEP_BIND` or `HA_URL` still wins.

    AGENT_LAN_ADDRESS=192.0.2.10   ->   bind(env)    == "192.0.2.10:8300"
                                        tei_url(env) == "http://192.0.2.10:8085"

There is no default address: a default would be somebody's host, so a
missing one raises `ConfigError` naming the variable. So does a bind that
the PEP does not take (`listener`). Every function takes
the environment it reads, so `chaperone-verify` can hand it the merged one, and
nothing is read at import: a module-level read would break every import in
a test. Stdlib only, because `chaperone-verify` imports it.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping
from enum import IntEnum
from typing import Final

SITE_FILE: Final = "/etc/creche/site.env"
LAN_ADDRESS_ENV: Final = "AGENT_LAN_ADDRESS"
SITE_HA_URL_ENV: Final = "AGENT_HA_URL"

#: The explicit overrides. Each wins over the value built from the site.
BIND_ENV: Final = "PEP_BIND"
HA_URL_ENV: Final = "HA_URL"


class Port(IntEnum):
    """What answers on the LAN address, and where."""

    PEP = 8300
    #: TEI: stateless LAN compute. Embeddings route through the PEP, so a
    #: sandbox needs no egress hole of its own.
    TEI = 8085


#: The largest port that a listener binds. Port 0 lets the system select a
#: port, and the tests of this package start the PEP on it.
PORT_MAX: Final = 65535

#: What separates the host from the port. The bind splits at the last one,
#: so an IPv6 host needs no brackets.
PORT_SEPARATOR: Final = ":"

#: One number that is zero, as the C resolver reads a number of an IPv4
#: address: decimal, octal or hexadecimal.
_ZERO: Final = r"(?:0[xX]0*|0+)"

#: An IPv4 text that the C resolver reads as `0.0.0.0`: one to four numbers,
#: and each one is zero. `0` and `0x0.0` are such texts.
_IPV4_EACH_INTERFACE: Final = re.compile(rf"{_ZERO}(?:\.{_ZERO}){{0,3}}")

#: The word for each interface in the config of some services. No resolver
#: reads it, so a bind on it is a start that fails.
_EACH_INTERFACE_WORD: Final = "*"


class ConfigError(RuntimeError):
    """The environment names no LAN address, or a bind that the PEP does
    not take."""


def lan_address(env: Mapping[str, str]) -> str:
    """`AGENT_LAN_ADDRESS`, or `ConfigError` naming it and the site file."""
    address = env.get(LAN_ADDRESS_ENV, "").strip()
    if not address:
        raise ConfigError(f"{LAN_ADDRESS_ENV} is not set ({SITE_FILE})")

    return address


def bind(env: Mapping[str, str]) -> str:
    """`host:port` the PEP listens on: `PEP_BIND`, else the LAN address.
    `ConfigError` for a bind that `listener` refuses."""
    variable, text = _bind_text(env)
    _split(variable, text)

    return text


def listener(env: Mapping[str, str]) -> tuple[str, int]:
    """The host and the port of `bind(env)`, as the server takes them.

    `ConfigError`, naming the variable, for a bind that the PEP does not
    take:

    1. A text that is not `host:port`, or a port that is not a number from
       0 to `PORT_MAX`. The server cannot bind it.
    2. No host. The PEP has no default address.
    3. A host that Python cannot give to the resolver. The server raises on
       it.
    4. A host that stands for each interface of the host. The PEP binds the
       LAN address, or loopback in a test.

    Each other host goes to the resolver as it is.
    """
    return _split(*_bind_text(env))


def _bind_text(env: Mapping[str, str]) -> tuple[str, str]:
    """The variable that gives the bind, and the bind as text."""
    explicit = env.get(BIND_ENV, "").strip()
    if explicit:
        return BIND_ENV, explicit

    return LAN_ADDRESS_ENV, f"{lan_address(env)}{PORT_SEPARATOR}{int(Port.PEP)}"


def _split(variable: str, text: str) -> tuple[str, int]:
    host, separator, port_text = text.rpartition(PORT_SEPARATOR)
    port = _port(port_text) if separator else None
    if port is None:
        raise ConfigError(
            f"{variable} gives the bind {text!r}, which is not host:port "
            f"with a port from 0 to {PORT_MAX}"
        )

    if not host:
        raise ConfigError(f"{variable} gives the bind {text!r}, which names no host")

    resolver_text = _resolver_text(host)
    if resolver_text is None:
        raise ConfigError(f"{variable} gives the bind {text!r}, whose host no resolver takes")

    if _is_each_interface(resolver_text):
        raise ConfigError(
            f"{variable} gives the bind {text!r}, which names each interface of the host"
        )

    return host, port


def _port(text: str) -> int | None:
    """The port that `text` names. None for a text that `int` does not
    read, and for a number that is not 0 to `PORT_MAX`."""
    try:
        port = int(text)
    except ValueError:
        return None

    return port if 0 <= port <= PORT_MAX else None


def _resolver_text(host: str) -> str | None:
    """The host as the resolver gets it. `socket.getaddrinfo` encodes the
    text as IDNA first, and that step makes an ASCII digit from a
    full-width digit.

    None for a text with no such encoding: a host name with an empty label,
    or with a label of more than 63 characters. `socket.getaddrinfo` raises
    on that text."""
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return None


def _is_each_interface(text: str) -> bool:
    """Whether a listener on the host `text` answers on each interface of
    the host: the IPv4 address of all zeros, the IPv6 address of all zeros,
    or that IPv4 address as an IPv6 address, in each spelling. `text` is
    the host as the resolver gets it.

    Some resolvers stop at white space, so only the text before the first
    white space counts. Brackets around the text do not count.
    """
    bare = text[1:-1] if text.startswith("[") and text.endswith("]") else text
    address_text = bare.split(maxsplit=1)[0] if bare.strip() else bare
    if address_text == _EACH_INTERFACE_WORD or _IPV4_EACH_INTERFACE.fullmatch(address_text):
        return True

    try:
        address = ipaddress.IPv6Address(address_text)
    except ValueError:
        return False

    mapped = address.ipv4_mapped

    return int(address) == 0 or (mapped is not None and int(mapped) == 0)


def tei_url(env: Mapping[str, str]) -> str:
    """TEI's base URL, or `""` when the site names no LAN address. Only a
    PEP started with `PEP_BIND` and no site gets the empty answer, and its
    `embed` then fails closed."""
    address = env.get(LAN_ADDRESS_ENV, "").strip()
    if not address:
        return ""

    return f"http://{address}:{int(Port.TEI)}"


def ha_url(env: Mapping[str, str]) -> str:
    """Home Assistant's base URL: `HA_URL`, else `AGENT_HA_URL`. A site with
    no Home Assistant sets neither, and the answer is `""`."""
    for name in (HA_URL_ENV, SITE_HA_URL_ENV):
        value = env.get(name, "").strip()
        if value:
            return value

    return ""
