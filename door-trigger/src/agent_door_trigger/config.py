"""Configuration for both of this door's processes.

`fire` makes two calls to `attendance` and exits; `serve` also watches the
registry and the webhook token directory. Fail closed, the same floor
contract 02 §3 rule 7 sets for every door's own credential: a missing,
empty or short token file refuses to start rather than serving with no
authentication (`ConfigError`). An empty key would turn a LAN admin
surface into an open one, and this rule exists so that cannot happen.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

#: Contract 02 §3 rule 7's floor, applied here to this door's own
#: `attendance` token the same way every other door applies it.
MIN_TOKEN_BYTES = 32

#: Contract 02 §3 rule 1. `attendance` is a systemd USER unit, so its own
#: runtime directory sits under `/run/user/<uid>`, which the hardened PEP
#: unit cannot reach — the socket lives on the state root instead, as in
#: `attendance/src/attendance/config.py` and door-owui's and door-tui's
#: own `DEFAULT_ATTENDANCE_SOCKET`.
DEFAULT_ATTENDANCE_SOCKET = "/srv/agents/state/rework/sock/sessiond.sock"
#: gate-1a.md's token layout.
DEFAULT_ATTENDANCE_TOKEN_FILE = "/srv/agents/state/rework/tokens/door-trigger.token"

#: Contract 05 §2's status document directory.
DEFAULT_FAMILIES_DIR = "/srv/agents/state/rework/families"

#: The registry root `creche-caregiver.service` itself is started with
#: (`systemd/creche-caregiver.service`). Not a contract value: this door
#: reads the registry only for declared webhook names (`README.md`'s
#: Known gaps).
DEFAULT_REGISTRY_ROOT = "/srv/agents/registry"

#: Contract 05 §6.4's directory for each webhook's own bearer token,
#: under the shared rework state root.
DEFAULT_WEBHOOKS_DIR = "/srv/agents/state/rework/triggers/webhooks"

#: The shared rework state root: `attendance`'s outcome records, the PEP's
#: audit and the quiet check's own records all sit under it.
DEFAULT_STATE_ROOT = "/srv/agents/state/rework"

#: The PEP's port on the LAN address (`PEP_BIND`). The quiet check calls
#: it as the family (contract 01 §3.15 rule 4).
PEP_PORT = 8300

#: The host's LAN address, from the site file the unit names
#: (`/etc/creche/site.env`). The webhook listener must be reachable
#: from Node-RED and Home Assistant automations elsewhere on the LAN,
#: unlike a door whose only caller lives on this same host.
ENV_LAN_ADDRESS = "AGENT_LAN_ADDRESS"

#: `docs/rework/spec.md` §3.6's port. Contract 02 §3 rule 9 lists the ports
#: the host already uses, and 8360 is not one of them.
DEFAULT_WEBHOOK_PORT = 8360

#: Contract 05 §2 rule 4: `caregiver` rewrites its status document "at
#: least every 30 seconds". Refreshing this door's route table on the
#: same cadence means a family's status is never staler here than it
#: already is at the source.
DEFAULT_REFRESH_S = 30.0

#: A Unix socket has no host, but httpx still needs a base URL to build a
#: request line against. Nothing resolves this name.
UDS_BASE_URL = "http://sessiond"

ENV_ATTENDANCE_SOCKET = "DOOR_TRIGGER_SESSIOND_SOCKET"
ENV_ATTENDANCE_URL = "DOOR_TRIGGER_SESSIOND_URL"
ENV_ATTENDANCE_TOKEN_FILE = "DOOR_TRIGGER_SESSIOND_TOKEN_FILE"
ENV_BIND = "DOOR_TRIGGER_BIND"
ENV_FAMILIES_DIR = "DOOR_TRIGGER_FAMILIES_DIR"
ENV_REGISTRY_ROOT = "DOOR_TRIGGER_REGISTRY_ROOT"
ENV_WEBHOOKS_DIR = "DOOR_TRIGGER_WEBHOOKS_DIR"
ENV_REFRESH_S = "DOOR_TRIGGER_REFRESH_S"
ENV_STATE_ROOT = "DOOR_TRIGGER_STATE_ROOT"
ENV_PEP_URL = "DOOR_TRIGGER_PEP_URL"

_WILDCARD_HOSTS = frozenset({"0.0.0.0", "::", "[::]", "*", ""})


class ConfigError(Exception):
    """This process will not start. The message names which setting to fix."""


@dataclass(frozen=True)
class AttendanceTarget:
    """Where `attendance` is, and the bearer token this door presents to it."""

    url: str
    socket: Path | None
    token: str


@dataclass(frozen=True)
class FireConfig:
    """Everything `agent-trigger fire` needs. All but `attendance` serve the
    quiet check (contract 01 §3.15)."""

    attendance: AttendanceTarget
    pep_url: str
    registry_root: Path
    families_dir: Path
    state_root: Path

    def describe(self) -> str:
        """A one-line summary for `--check`. Carries no secret."""
        target = self.attendance.socket or self.attendance.url
        return (
            f"attendance={target} chaperone={self.pep_url} registry={self.registry_root} "
            f"families={self.families_dir} state={self.state_root}"
        )


@dataclass(frozen=True)
class ServeConfig:
    """Everything `agent-trigger serve` needs, on top of a `FireConfig`'s
    own settings: the webhook listener also fires jobs once a call
    authenticates."""

    attendance: AttendanceTarget
    bind_host: str
    bind_port: int
    families_dir: Path
    registry_root: Path
    webhooks_dir: Path
    refresh_s: float

    def describe(self) -> str:
        target = self.attendance.socket or self.attendance.url
        return (
            f"bind={self.bind_host}:{self.bind_port} attendance={target} "
            f"families={self.families_dir} registry={self.registry_root} "
            f"webhooks={self.webhooks_dir} refresh={self.refresh_s}s"
        )


def fire_config_from_env(environ: dict[str, str] | None = None) -> FireConfig:
    """Build `fire`'s config, or raise `ConfigError` naming what to fix."""
    env = environ if environ is not None else dict(os.environ)
    return FireConfig(
        attendance=_attendance_target(env),
        pep_url=_pep_url(env),
        registry_root=Path(env.get(ENV_REGISTRY_ROOT, DEFAULT_REGISTRY_ROOT)),
        families_dir=Path(env.get(ENV_FAMILIES_DIR, DEFAULT_FAMILIES_DIR)),
        state_root=Path(env.get(ENV_STATE_ROOT, DEFAULT_STATE_ROOT)),
    )


def serve_config_from_env(environ: dict[str, str] | None = None) -> ServeConfig:
    """Build `serve`'s config, or raise `ConfigError` naming what to fix."""
    env = environ if environ is not None else dict(os.environ)
    # An explicit bind wins, even an empty one: `_bind` refuses that.
    bind = env.get(ENV_BIND)
    if bind is None:
        bind = f"{_lan_address(env)}:{DEFAULT_WEBHOOK_PORT}"

    host, port = _bind(bind)

    return ServeConfig(
        attendance=_attendance_target(env),
        bind_host=host,
        bind_port=port,
        families_dir=Path(env.get(ENV_FAMILIES_DIR, DEFAULT_FAMILIES_DIR)),
        registry_root=Path(env.get(ENV_REGISTRY_ROOT, DEFAULT_REGISTRY_ROOT)),
        webhooks_dir=Path(env.get(ENV_WEBHOOKS_DIR, DEFAULT_WEBHOOKS_DIR)),
        refresh_s=_refresh_s(env),
    )


def _attendance_target(env: dict[str, str]) -> AttendanceTarget:
    url, socket = _attendance_endpoint(env)
    return AttendanceTarget(url=url, socket=socket, token=_read_token(env))


def _attendance_endpoint(env: dict[str, str]) -> tuple[str, Path | None]:
    """A Unix socket by default. A URL only when `attendance` binds the LAN."""
    url = env.get(ENV_ATTENDANCE_URL, "").strip()
    socket = env.get(ENV_ATTENDANCE_SOCKET, "").strip()
    if url and socket:
        raise ConfigError(f"set {ENV_ATTENDANCE_URL} or {ENV_ATTENDANCE_SOCKET}, not both.")
    if url:
        if not url.startswith(("http://", "https://")):
            raise ConfigError(f"{ENV_ATTENDANCE_URL} must start with http:// or https://.")
        return url.rstrip("/"), None

    return UDS_BASE_URL, Path(socket or DEFAULT_ATTENDANCE_SOCKET)


def _lan_address(env: dict[str, str]) -> str:
    """The site's LAN address. No default: a default would be somebody's host."""
    address = env.get(ENV_LAN_ADDRESS, "").strip()
    if not address:
        raise ConfigError(f"{ENV_LAN_ADDRESS} is not set (/etc/creche/site.env).")

    return address


def _pep_url(env: dict[str, str]) -> str:
    url = env.get(ENV_PEP_URL, "").strip() or f"http://{_lan_address(env)}:{PEP_PORT}"
    if not url.startswith(("http://", "https://")):
        raise ConfigError(f"{ENV_PEP_URL} must start with http:// or https://.")

    return url.rstrip("/")


def _read_token(env: dict[str, str]) -> str:
    path = env.get(ENV_ATTENDANCE_TOKEN_FILE, "").strip() or DEFAULT_ATTENDANCE_TOKEN_FILE

    try:
        value = Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigError(
            f"{ENV_ATTENDANCE_TOKEN_FILE}: cannot read {path} ({exc.strerror})."
        ) from exc
    except UnicodeDecodeError:
        value = None
    except ValueError as exc:
        # A path that the system refuses before the read: a NUL byte.
        raise ConfigError(f"{ENV_ATTENDANCE_TOKEN_FILE}: cannot read {path!r} ({exc}).") from exc

    if value is None:
        # The decode error holds bytes of the token. The refusal starts after
        # the handler, so that it holds no decode error.
        raise ConfigError(f"{ENV_ATTENDANCE_TOKEN_FILE}: the token in {path} is not UTF-8 text.")

    if len(value.encode("utf-8")) < MIN_TOKEN_BYTES:
        raise ConfigError(
            f"{ENV_ATTENDANCE_TOKEN_FILE}: the token in {path} is shorter than "
            f"{MIN_TOKEN_BYTES} bytes. An empty or short token is how an admin "
            "surface becomes an open one."
        )

    return value


def _bind(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    if host.lower() in _WILDCARD_HOSTS:
        raise ConfigError(
            f"{ENV_BIND}={value} binds every interface. Bind the LAN address, never 0.0.0.0."
        )

    try:
        number = int(port)
    except ValueError as exc:
        raise ConfigError(f"{ENV_BIND}={value} is not host:port.") from exc
    if not 1 <= number <= 65535:
        raise ConfigError(f"{ENV_BIND}={value} is not a port.")

    return host.strip("[]"), number


def _refresh_s(env: dict[str, str]) -> float:
    raw = env.get(ENV_REFRESH_S, "").strip()
    if not raw:
        return DEFAULT_REFRESH_S

    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{ENV_REFRESH_S}={raw!r} is not a number.") from exc
    if value <= 0:
        raise ConfigError(f"{ENV_REFRESH_S}={raw!r} must be greater than 0.")

    return value
