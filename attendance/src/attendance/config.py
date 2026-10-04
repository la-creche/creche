"""The service configuration, read from the environment.

Every root is configurable so a test drives the whole service inside
`tmp_path`. Nothing here holds a secret: tokens live in files that `auth.py`
reads, and the family key and PEP token never reach this process at all
(contract 03 §12).
"""

from __future__ import annotations

import ipaddress
import math
import os
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .exec_channel import DEFAULT_COMMAND
from .wire import LOCK_POLL_S, LOCK_STALE_S

# Contract 02 §3 rule 1: a Unix socket is the default and is enough, because
# all four doors run on the same host. `attendance` is a systemd USER unit, so
# its own runtime directory sits under `/run/user/<uid>`, which the hardened
# PEP unit cannot reach -- the socket lives on the production state root
# instead. `__main__.py` makes `sock/` setgid and the socket itself 0660
# after bind, so the PEP's group can connect.
DEFAULT_SOCKET = "/srv/agents/state/rework/sock/sessiond.sock"

# Contract 02 §3 rule 2: TCP binds the LAN address and nothing else. Loopback
# answers nothing on this host, and 0.0.0.0 would publish the service. The
# address is the site's: the unit's `EnvironmentFile=/etc/creche/site.env`
# sets it, and `SESSIOND_LAN_ADDRESS` overrides it. No default: a default
# would be somebody's host. Contract 02 §3 rule 9 gives the optional LAN
# port 8350.
LAN_ADDRESS_ENV = "AGENT_LAN_ADDRESS"
DEFAULT_LAN_PORT = 8350

DEFAULT_SESSIONS_ROOT = "/srv/agents/sessions"
DEFAULT_STATE_ROOT = "/srv/agents/state/rework"
DEFAULT_WORK_ROOT = "/srv/agents/work"
DEFAULT_LOG_DIR = "/var/log/sessiond"

# Contract 02 §10.4. One file, mode 0600, beside every other credential this
# host keeps. `SESSIOND_OWUI_URL` is what turns the feature on.
DEFAULT_OWUI_KEY_FILE = "/srv/agents/state/rework/tokens/owui-api.key"

ENV_PREFIX = "SESSIOND_"

_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
_FALSE_WORDS = frozenset({"0", "false", "no", "off"})

_NUL = "\x00"

# Two texts that a listener or a URL writer reads as each interface, and
# that are not an IP address.
_EACH_INTERFACE_WORDS = frozenset({"*", "[::]"})

# What starts the zone of an IPv6 address, as in `::%1`.
_ZONE_SEPARATOR = "%"

_IPV4_SEPARATOR = "."
_IPV4_PARTS_MAX = 4

# One number of an IPv4 address that is zero: decimal, octal or hexadecimal.
_ZERO_NUMBER = re.compile(r"0+|0[xX]0*")


class Bind(Enum):
    """Whether the service also binds TCP (contract 02 §3 rule 2)."""

    SOCKET_ONLY = "socket_only"
    SOCKET_AND_LAN = "socket_and_lan"


class ConfigError(Exception):
    """The environment does not describe a service that may start."""


@dataclass(slots=True, frozen=True)
class Config:
    """Everything the service reads from its environment."""

    sessions_root: Path
    state_root: Path
    work_root: Path
    socket_path: Path
    bind: Bind
    lan_address: str
    lan_port: int
    channel_command: str
    log_dir: Path
    # Contract 02 §10.4 rule 4. With either of these empty the Open WebUI
    # copy is off and nothing else changes. The key is in a file, never in
    # the environment: invariant 13 keeps a secret off argv and out of a
    # process listing.
    owui_url: str = ""
    owui_key_file: Path = Path(DEFAULT_OWUI_KEY_FILE)
    owui_folder_id: str = ""
    # Contract 03 §11.4 rule 4's two numbers. They are here so a test can watch
    # the lock in fractions of a second, and so an operator can widen the
    # window on a host where the sandbox writes its beat more slowly.
    lock_stale_s: float = LOCK_STALE_S
    lock_poll_s: float = LOCK_POLL_S

    @property
    def binds_lan(self) -> bool:
        return self.bind is Bind.SOCKET_AND_LAN

    def playpen_log(self, family: str) -> Path:
        """Where one family's playpen stderr lands (contract 03 §1 rule 4)."""
        return self.log_dir / f"playpen-{family}.log"


def from_env(environ: dict[str, str] | None = None) -> Config:
    """Build the config. Raises ConfigError on a value that cannot work."""
    source = environ if environ is not None else dict(os.environ)

    return Config(
        sessions_root=_path(source, "SESSIONS_ROOT", DEFAULT_SESSIONS_ROOT),
        state_root=_path(source, "STATE_ROOT", DEFAULT_STATE_ROOT),
        work_root=_path(source, "WORK_ROOT", DEFAULT_WORK_ROOT),
        socket_path=_path(source, "SOCKET", DEFAULT_SOCKET),
        bind=_bind(source),
        lan_address=_lan_address(source),
        lan_port=_port(source),
        channel_command=_text(source, "CHANNEL_COMMAND", DEFAULT_COMMAND),
        log_dir=_path(source, "LOG_DIR", DEFAULT_LOG_DIR),
        owui_url=_text(source, "OWUI_URL", ""),
        owui_key_file=_path(source, "OWUI_KEY_FILE", DEFAULT_OWUI_KEY_FILE),
        owui_folder_id=_text(source, "OWUI_FOLDER_ID", ""),
        lock_stale_s=_seconds(source, "LOCK_STALE_S", LOCK_STALE_S),
        lock_poll_s=_seconds(source, "LOCK_POLL_S", LOCK_POLL_S),
    )


def _seconds(source: dict[str, str], name: str, fallback: float) -> float:
    raw = source.get(ENV_PREFIX + name, "").strip()

    if not raw:
        return fallback

    try:
        value = float(raw)
    except ValueError as error:
        raise ConfigError(f"{ENV_PREFIX}{name} is {raw!r}, which is not a number") from error

    # NaN is not below zero, so the range check alone lets it pass. With NaN
    # or an infinity, the lock of a playpen never goes stale.
    if not math.isfinite(value) or value <= 0:
        raise ConfigError(f"{ENV_PREFIX}{name} is {value}, which is not a positive number")

    return value


def _text(source: dict[str, str], name: str, fallback: str) -> str:
    value = source.get(ENV_PREFIX + name, "").strip()
    return value if value else fallback


def _path(source: dict[str, str], name: str, fallback: str) -> Path:
    text = _text(source, name, fallback)

    # No path holds a NUL byte. The first use of such a path raises an
    # error that no caller expects.
    if _NUL in text:
        raise ConfigError(f"{ENV_PREFIX}{name} holds a NUL byte, which no path holds")

    return Path(text)


def _lan_address(source: dict[str, str]) -> str:
    """`SESSIOND_LAN_ADDRESS` when set, else the site's `AGENT_LAN_ADDRESS`."""
    own = _text(source, "LAN_ADDRESS", "")
    name = ENV_PREFIX + "LAN_ADDRESS" if own else LAN_ADDRESS_ENV
    value = own or source.get(LAN_ADDRESS_ENV, "").strip()

    if not value:
        raise ConfigError(f"{LAN_ADDRESS_ENV} is not set (/etc/creche/site.env)")

    if _is_each_interface(value):
        raise ConfigError(f"{name} is the address of each interface. Give the LAN address")

    return value


def _is_each_interface(text: str) -> bool:
    """True for each spelling of the address that stands for each interface.

    A listener on that address answers on each interface of the host. The
    resolver reads more spellings than `0.0.0.0` and `::` as that address.

    CONTRACT-QUESTION: contract 02 §3 rule 2 says never `0.0.0.0` and gives
    the address no grammar. This reading refuses each spelling of the address
    of each interface and no other text: the stricter reading of the rule. A
    grammar for the address costs each deployment that names its host.
    """
    plain = _resolver_text(text).partition(_ZONE_SEPARATOR)[0]

    if plain in _EACH_INTERFACE_WORDS:
        return True

    try:
        address = ipaddress.ip_address(plain)
    except ValueError:
        return _is_zero_ipv4(plain)

    mapped = address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) else None

    return int(address) == 0 or (mapped is not None and int(mapped) == 0)


def _resolver_text(text: str) -> str:
    """The text that the resolver gets for a host.

    The socket module makes a host that is not ASCII an ASCII text first,
    with IDNA. That step makes a digit and a dot of another script an ASCII
    digit and an ASCII dot, and it removes some characters. A text that has
    no IDNA form does not resolve, so it stays as it is.
    """
    try:
        return text.encode("idna").decode("ascii")
    except UnicodeError:
        return text


def _is_zero_ipv4(text: str) -> bool:
    """True for a short or a prefixed form of `0.0.0.0`, for example `0`.

    The resolver reads one to four numbers, each one decimal, octal or
    hexadecimal. The text is the address of each interface when each number
    is zero.
    """
    parts = text.split(_IPV4_SEPARATOR)

    return len(parts) <= _IPV4_PARTS_MAX and all(_ZERO_NUMBER.fullmatch(part) for part in parts)


def _bind(source: dict[str, str]) -> Bind:
    """`bind_lan` is off unless the environment turns it on, explicitly."""
    raw = source.get(ENV_PREFIX + "BIND_LAN", "").strip().lower()

    if not raw or raw in _FALSE_WORDS:
        return Bind.SOCKET_ONLY

    if raw in _TRUE_WORDS:
        return Bind.SOCKET_AND_LAN

    raise ConfigError(f"{ENV_PREFIX}BIND_LAN is {raw!r}, which is not a yes or a no")


def _port(source: dict[str, str]) -> int:
    raw = source.get(ENV_PREFIX + "LAN_PORT", "").strip()

    if not raw:
        return DEFAULT_LAN_PORT

    try:
        port = int(raw)
    except ValueError as error:
        raise ConfigError(f"{ENV_PREFIX}LAN_PORT is {raw!r}, which is not a number") from error

    if not 1 <= port <= 65535:
        raise ConfigError(f"{ENV_PREFIX}LAN_PORT is {port}, outside 1 to 65535")

    return port
