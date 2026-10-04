"""What the noticeboard reads from its environment.

Every root is configurable so a test drives the whole service inside
`tmp_path`. Two rules here are not style choices.

1. **The bind is never `0.0.0.0`.** Services on this host bind the LAN
   address or a Unix socket, and assuming otherwise has already cost one
   same-day patch release.
2. **A LAN bind with no access key refuses to start.** An empty key would
   turn a LAN admin surface into an open one. There is no flag that makes
   this legal.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

#: Free on the host: contract 02 §3 rule 9 lists the ports already taken.
DEFAULT_PORT: Final = 8370

#: Services on this host bind the LAN IP (`noticeboard/AGENTS.md` rule 2). The
#: default bind is the site's, from the unit's
#: `EnvironmentFile=/etc/creche/site.env`. `VIEW_BIND` overrides it.
LAN_ADDRESS_ENV: Final = "AGENT_LAN_ADDRESS"

DEFAULT_STATE_ROOT: Final = "/srv/agents/state/rework"
DEFAULT_REGISTRY_DIR: Final = "/srv/agents/registry"

#: Contract 02 §3 rule 1: the socket lives on the production state root,
#: because a hardened unit cannot reach `/run/user/<uid>`.
DEFAULT_ATTENDANCE_SOCKET: Final = "/srv/agents/state/rework/sock/sessiond.sock"

#: A Unix-socket client still needs a base URL for the path and the Host
#: header. The name is never resolved.
SOCKET_BASE_URL: Final = "http://sessiond"

ENV_PREFIX: Final = "VIEW_"

#: Contract 02 §3 rule 7's floor, applied to this service's own key. A
#: one-character key on a LAN bind is not meaningfully better than none.
MIN_KEY_BYTES: Final = 32

#: An address that answers only on this machine. A short key, or none at
#: all, is allowed here so a developer can run the pages locally.
_LOOPBACK: Final = frozenset({"127.0.0.1", "::1", "localhost"})

#: An address that publishes the service on every interface.
_WILDCARD: Final = frozenset({"0.0.0.0", "::", "*", ""})

_MIN_PORT: Final = 1
_MAX_PORT: Final = 65535

#: How many audit lines and sessions one page shows before it pages.
DEFAULT_PAGE_SIZE: Final = 50
_MAX_PAGE_SIZE: Final = 500


class ConfigError(Exception):
    """The environment does not describe a service that may start."""


@dataclass(slots=True, frozen=True)
class Config:
    """Everything `noticeboard` reads from its environment."""

    bind: str
    port: int
    state_root: Path
    registry_dir: Path
    attendance_socket: Path | None
    attendance_url: str
    page_size: int
    #: `Secure` on the CSRF cookie. The proxy terminates TLS, so a browser
    #: only ever sees https. Set `VIEW_COOKIE_SECURE=0` for a plain-HTTP
    #: dev run: with it on, the browser sends no cookie over http and
    #: every form post is refused for a missing token.
    cookie_secure: bool = True
    #: Never logged, never printed by `--check`, never put in a URL
    #: (invariant 13). `security.py` is the only reader.
    access_key: str = ""

    @property
    def on_loopback(self) -> bool:
        return self.bind in _LOOPBACK

    @property
    def audit_dir(self) -> Path:
        """Contract 04 §6: the PEP's daily JSONL files."""
        return self.state_root / "audit"

    @property
    def families_dir(self) -> Path:
        """Contract 05 §2 rule 6: a reader lists this directory to find every
        family. No index file exists, so no index can go stale."""
        return self.state_root / "families"

    @property
    def outcomes_dir(self) -> Path:
        """Contract 02 §13.1, contract 05 §8."""
        return self.state_root / "outcomes"

    @property
    def view_token_file(self) -> Path:
        """Contract 02 §3 rule 5: the `view-ro` bearer. `attendance` mints it;
        this service only reads it."""
        return self.state_root / "tokens" / "view-ro.token"


def from_env(environ: dict[str, str] | None = None) -> Config:
    """Build the config. Raises `ConfigError` on a value that cannot work."""
    source = environ if environ is not None else dict(os.environ)
    bind = _bind(source)
    key = _access_key(source)

    _refuse_wildcard(bind)
    _refuse_open_on_lan(bind, key)

    socket_path = _text(source, "SESSIOND_SOCKET", DEFAULT_ATTENDANCE_SOCKET)
    url = _text(source, "SESSIOND_URL", "")

    return Config(
        bind=bind,
        port=_port(source),
        state_root=_path(source, "STATE_ROOT", DEFAULT_STATE_ROOT),
        registry_dir=_path(source, "REGISTRY_DIR", DEFAULT_REGISTRY_DIR),
        # A URL wins: it is how an operator points the noticeboard at a TCP
        # `attendance` (contract 02 §3 rule 2) instead of the socket.
        attendance_socket=None if url else Path(socket_path),
        attendance_url=url or SOCKET_BASE_URL,
        page_size=_page_size(source),
        cookie_secure=_flag(source, "COOKIE_SECURE"),
        access_key=key,
    )


def _bind(source: dict[str, str]) -> str:
    """`VIEW_BIND` when set, else the site's LAN address. No default: a
    default would be somebody's host."""
    bind = _text(source, "BIND", source.get(LAN_ADDRESS_ENV, "").strip())
    if bind:
        return bind

    raise ConfigError(f"{LAN_ADDRESS_ENV} is not set (/etc/creche/site.env)")


def _flag(source: dict[str, str], name: str) -> bool:
    """A switch that is on unless it is turned off."""
    return _text(source, name, "1").lower() not in ("0", "false", "no")


def _refuse_wildcard(bind: str) -> None:
    if bind not in _WILDCARD:
        return

    raise ConfigError(
        f"{ENV_PREFIX}BIND is {bind!r}: bind the LAN address or a loopback "
        f"address, never a wildcard"
    )


def _refuse_open_on_lan(bind: str, key: str) -> None:
    """A LAN bind needs a full-length key: without one it is an open admin surface."""
    if bind in _LOOPBACK:
        return

    if len(key.encode("utf-8")) >= MIN_KEY_BYTES:
        return

    raise ConfigError(
        f"{ENV_PREFIX}BIND is {bind!r}, which is not loopback, and the access key is missing "
        f"or under {MIN_KEY_BYTES} bytes: refusing to serve an unkeyed admin surface on the LAN"
    )


def _access_key(source: dict[str, str]) -> str:
    """The file wins over the literal value: a mode-0600 file keeps the key
    out of a process listing and out of a unit's `Environment=` line."""
    named = _text(source, "ACCESS_KEY_FILE", "")

    if not named:
        return source.get(ENV_PREFIX + "ACCESS_KEY", "").strip()

    try:
        return Path(named).read_text(encoding="utf-8").strip()
    except OSError as error:
        raise ConfigError(f"{ENV_PREFIX}ACCESS_KEY_FILE cannot be read: {named}") from error


def _text(source: dict[str, str], name: str, fallback: str) -> str:
    value = source.get(ENV_PREFIX + name, "").strip()
    return value if value else fallback


def _path(source: dict[str, str], name: str, fallback: str) -> Path:
    return Path(_text(source, name, fallback))


def _port(source: dict[str, str]) -> int:
    return _bounded(source, "PORT", DEFAULT_PORT, _MIN_PORT, _MAX_PORT)


def _page_size(source: dict[str, str]) -> int:
    return _bounded(source, "PAGE_SIZE", DEFAULT_PAGE_SIZE, 1, _MAX_PAGE_SIZE)


def _bounded(source: dict[str, str], name: str, fallback: int, low: int, high: int) -> int:
    raw = source.get(ENV_PREFIX + name, "").strip()

    if not raw:
        return fallback

    try:
        value = int(raw)
    except ValueError as error:
        raise ConfigError(f"{ENV_PREFIX}{name} is {raw!r}, which is not a number") from error

    if not low <= value <= high:
        raise ConfigError(f"{ENV_PREFIX}{name} is {value}, outside {low} to {high}")

    return value
