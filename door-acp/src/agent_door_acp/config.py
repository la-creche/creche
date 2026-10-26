"""The door's configuration, read from the environment, checked at start.

Fail closed. The door refuses to start when its key file is missing, short,
not ASCII, or readable by a user other than its owner. It binds one IP
address, never every interface and never a multicast group. An empty key
would turn a LAN surface into an open one (contract 02 §3 rule 7).

No secret reaches argv, a URL or a log line (invariant 13). The key arrives
as a path to a file and stays in memory as bytes.
"""

from __future__ import annotations

import ipaddress
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

#: Contract 02 §3 rule 7's floor, the same floor as every other door.
MIN_KEY_BYTES = 32

#: The bits that let a user other than the owner touch the key file. The
#: same rule as `attendance`'s token files and `door-trigger`'s webhook
#: tokens: mode 0600 or tighter.
_NOT_OWNER_BITS = stat.S_IRWXG | stat.S_IRWXO

#: The door's port on the host's LAN address. Contract 02 §3 rule 9 lists
#: the ports the host uses, and 8390 is not one of them.
DEFAULT_PORT = 8390

ENV_LAN_ADDRESS = "AGENT_LAN_ADDRESS"
ENV_BIND = "DOOR_ACP_BIND"
ENV_KEY_FILE = "DOOR_ACP_KEY_FILE"

_PORT = re.compile(r"[0-9]{1,5}")


class ConfigError(Exception):
    """The door will not start. The message names the setting to fix."""


@dataclass(frozen=True)
class DoorConfig:
    """Everything the door needs. `door_key` is a secret and is never logged."""

    bind_host: str
    bind_port: int
    door_key: bytes

    def describe(self) -> str:
        """A one-line summary for `--check`. Carries no secret."""
        return f"bind={self.bind_host}:{self.bind_port}"


def from_env(environ: dict[str, str] | None = None) -> DoorConfig:
    """Build the config, or raise `ConfigError` naming what to fix."""
    env = environ if environ is not None else dict(os.environ)

    bind = env.get(ENV_BIND)
    if bind is None:
        bind = f"{_lan_address(env)}:{DEFAULT_PORT}"

    host, port = _bind(bind)
    return DoorConfig(bind_host=host, bind_port=port, door_key=_read_key(env))


def _lan_address(env: dict[str, str]) -> str:
    """The site's LAN address. No default: a default would be somebody's host."""
    address = env.get(ENV_LAN_ADDRESS, "").strip()
    if not address:
        raise ConfigError(f"set {ENV_BIND}, or {ENV_LAN_ADDRESS} (/etc/creche/site.env).")

    return address


def _bind(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")

    try:
        address = ipaddress.ip_address(host.removeprefix("[").removesuffix("]"))
    except ValueError as exc:
        raise ConfigError(f"{ENV_BIND}={value} does not start with an IP address.") from exc

    if address.is_unspecified:
        raise ConfigError(
            f"{ENV_BIND}={value} binds every interface. Bind the LAN address, never 0.0.0.0."
        )
    if address.is_multicast:
        raise ConfigError(f"{ENV_BIND}={value} is a multicast address. Bind the LAN address.")

    if _PORT.fullmatch(port) is None or not 1 <= int(port) <= 65535:
        raise ConfigError(f"{ENV_BIND}={value} is not host:port.")

    return str(address), int(port)


def _read_key(env: dict[str, str]) -> bytes:
    name = env.get(ENV_KEY_FILE, "").strip()
    if not name:
        raise ConfigError(f"{ENV_KEY_FILE} is not set. The door needs its key file.")

    path = Path(name)
    try:
        mode = path.stat().st_mode
        raw = path.read_bytes()
    except OSError as exc:
        # The path is not a secret. The content is, and it is not in the text.
        raise ConfigError(f"{ENV_KEY_FILE}: cannot read {path} ({exc.strerror}).") from exc
    except ValueError as exc:
        # A path that the system refuses before the read: a NUL byte.
        raise ConfigError(f"{ENV_KEY_FILE}: cannot read {name!r} ({exc}).") from exc

    if mode & _NOT_OWNER_BITS:
        raise ConfigError(
            f"{ENV_KEY_FILE}: {path} is not mode 0600. Remove the group and other bits."
        )

    value = raw.strip()
    if not value.isascii():
        raise ConfigError(f"{ENV_KEY_FILE}: the key in {path} is not ASCII text.")
    if len(value) < MIN_KEY_BYTES:
        raise ConfigError(
            f"{ENV_KEY_FILE}: the key in {path} is shorter than {MIN_KEY_BYTES} bytes. "
            "An empty or short key is how a LAN surface becomes an open one."
        )

    return value
