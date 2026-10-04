"""The door's configuration, read from the environment, checked at start.

Fail closed. The door refuses to start when a key file is missing, empty or
short, and it never binds `0.0.0.0`. An empty key would turn a LAN admin
surface into an open one (contract 02 §3 rule 7). This module exists so that
cannot happen.

No secret reaches argv, a URL or a log line (invariant 13). A key arrives as
a path to a file and stays in memory.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Contract 02 §3 rule 7's floor, applied to this door's own key as well.
MIN_KEY_BYTES = 32

# Contract 02 §3 rule 9 gives this door 8340. config overrides this default;
# from_env refuses 0.0.0.0 regardless.
DEFAULT_BIND = "127.0.0.1:8340"
DEFAULT_ATTENDANCE_SOCKET = "/srv/agents/state/rework/sock/sessiond.sock"
DEFAULT_TOKEN_FILE = "/srv/agents/state/rework/tokens/door-owui.token"
DEFAULT_FAMILIES_DIR = "/srv/agents/state/rework/families"

# A Unix socket has no host, but httpx still needs a base URL to build a
# request line against. Nothing resolves this name.
UDS_BASE_URL = "http://sessiond"

ENV_BIND = "DOOR_OWUI_BIND"
ENV_KEY_FILE = "DOOR_OWUI_KEY_FILE"
ENV_TOKEN_FILE = "DOOR_OWUI_SESSIOND_TOKEN_FILE"
ENV_SOCKET = "DOOR_OWUI_SESSIOND_SOCKET"
ENV_URL = "DOOR_OWUI_SESSIOND_URL"
ENV_FAMILIES_DIR = "DOOR_OWUI_FAMILIES_DIR"

_WILDCARD_HOSTS = frozenset({"0.0.0.0", "::", "[::]", "*", ""})


class ConfigError(Exception):
    """The door will not start. The message says which setting to fix."""


@dataclass(frozen=True)
class DoorConfig:
    """Everything the door needs. Two fields are secrets and never logged."""

    bind_host: str
    bind_port: int
    door_key: str
    attendance_token: str
    attendance_url: str
    attendance_socket: Path | None
    families_dir: Path

    def describe(self) -> str:
        """A one-line summary for `--check`. Carries no secret."""
        target = self.attendance_socket or self.attendance_url
        return (
            f"bind={self.bind_host}:{self.bind_port} attendance={target} "
            f"families={self.families_dir} key_bytes={len(self.door_key)}"
        )


def from_env(environ: dict[str, str] | None = None) -> DoorConfig:
    """Build the config, or raise `ConfigError` naming what to fix."""
    env = environ if environ is not None else dict(os.environ)

    host, port = _bind(env.get(ENV_BIND, DEFAULT_BIND))
    url, socket = _attendance_target(env)

    return DoorConfig(
        bind_host=host,
        bind_port=port,
        door_key=_read_key(env, ENV_KEY_FILE, required=True),
        attendance_token=_read_key(env, ENV_TOKEN_FILE, required=True),
        attendance_url=url,
        attendance_socket=socket,
        families_dir=Path(env.get(ENV_FAMILIES_DIR, DEFAULT_FAMILIES_DIR)),
    )


def _bind(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    if host.lower() in _WILDCARD_HOSTS:
        raise ConfigError(
            f"{ENV_BIND}={value} binds every interface. "
            "Bind the LAN address or loopback, never 0.0.0.0."
        )

    try:
        number = int(port)
    except ValueError as exc:
        raise ConfigError(f"{ENV_BIND}={value} is not host:port.") from exc
    if not 1 <= number <= 65535:
        raise ConfigError(f"{ENV_BIND}={value} is not a port.")

    return host.strip("[]"), number


def _attendance_target(env: dict[str, str]) -> tuple[str, Path | None]:
    """A Unix socket by default. A URL only when `attendance` binds the LAN."""
    url = env.get(ENV_URL, "").strip()
    socket = env.get(ENV_SOCKET, "").strip()
    if url and socket:
        raise ConfigError(f"set {ENV_URL} or {ENV_SOCKET}, not both.")
    if url:
        if not url.startswith(("http://", "https://")):
            raise ConfigError(f"{ENV_URL} must start with http:// or https://.")
        return url.rstrip("/"), None

    return UDS_BASE_URL, Path(socket or DEFAULT_ATTENDANCE_SOCKET)


def _read_key(env: dict[str, str], name: str, *, required: bool) -> str:
    path = env.get(name, DEFAULT_TOKEN_FILE if name == ENV_TOKEN_FILE else "").strip()
    if not path:
        if not required:
            return ""
        raise ConfigError(f"{name} is not set. The door needs its key file.")

    try:
        value = Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        # The path is not a secret. The content is, and it is not in the text.
        raise ConfigError(f"{name}: cannot read {path} ({exc.strerror}).") from exc
    except UnicodeDecodeError:
        # The decode error holds bytes of the key, so it is not the cause.
        raise ConfigError(f"{name}: the key in {path} is not UTF-8 text.") from None
    except ValueError as exc:
        # A path that the system refuses before the read: a NUL byte.
        raise ConfigError(f"{name}: cannot read {path!r} ({exc}).") from exc

    if len(value.encode("utf-8")) < MIN_KEY_BYTES:
        raise ConfigError(
            f"{name}: the key in {path} is shorter than {MIN_KEY_BYTES} bytes. "
            "An empty or short key is how an admin surface becomes an open one."
        )

    return value
