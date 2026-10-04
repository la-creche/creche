"""The door's configuration, read from the environment, checked at start.

Fail closed. The door refuses to start when its `attendance` token file is
missing, empty or under 32 bytes (contract 02 §3 rule 7). An empty key would
turn a LAN admin surface into an open one, and this rule exists so that
cannot happen.

No secret reaches argv, a URL or a message (invariant 13). The token arrives
as a path to a file and stays in memory.

This door binds nothing. It is a program the operator types, not a service, so it
has no bind address and no key of its own.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Contract 02 §3 rule 7's floor.
MIN_TOKEN_BYTES = 32

DEFAULT_ATTENDANCE_SOCKET = "/srv/agents/state/rework/sock/sessiond.sock"
DEFAULT_TOKEN_FILE = "/srv/agents/state/rework/tokens/door-tui.token"
DEFAULT_FAMILIES_DIR = "/srv/agents/state/rework/families"

#: Contract 03 §7.6's command. `sbx` resolves on PATH, the launcher does not.
DEFAULT_SBX = "sbx"
DEFAULT_PI_LAUNCH = "/opt/agent-supervisor/agent-pi-launch.js"

# A Unix socket has no host, but httpx still needs a base URL to build a
# request line against. Nothing resolves this name.
UDS_BASE_URL = "http://sessiond"

ENV_TOKEN_FILE = "DOOR_TUI_SESSIOND_TOKEN_FILE"
ENV_SOCKET = "DOOR_TUI_SESSIOND_SOCKET"
ENV_URL = "DOOR_TUI_SESSIOND_URL"
ENV_FAMILIES_DIR = "DOOR_TUI_FAMILIES_DIR"
ENV_SBX = "DOOR_TUI_SBX"
ENV_PI_LAUNCH = "DOOR_TUI_PI_LAUNCH"


class ConfigError(Exception):
    """The door will not start. The message says which setting to fix."""


@dataclass(frozen=True)
class TuiConfig:
    """Everything the door needs. One field is a secret and is never printed."""

    attendance_token: str
    attendance_url: str
    attendance_socket: Path | None
    families_dir: Path
    sbx: str
    pi_launch: str
    door_instance: str

    def describe(self) -> str:
        """A one-line summary for `--check`. Carries no secret."""
        target = self.attendance_socket or self.attendance_url

        return (
            f"attendance={target} families={self.families_dir} sbx={self.sbx} "
            f"launcher={self.pi_launch} instance={self.door_instance} "
            f"token_bytes={len(self.attendance_token)}"
        )


def from_env(environ: dict[str, str] | None = None) -> TuiConfig:
    """Build the config, or raise `ConfigError` naming what to fix."""
    env = environ if environ is not None else dict(os.environ)
    url, socket = _attendance_target(env)

    return TuiConfig(
        attendance_token=_read_token(env),
        attendance_url=url,
        attendance_socket=socket,
        families_dir=Path(env.get(ENV_FAMILIES_DIR, DEFAULT_FAMILIES_DIR)),
        sbx=env.get(ENV_SBX, DEFAULT_SBX).strip() or DEFAULT_SBX,
        pi_launch=env.get(ENV_PI_LAUNCH, DEFAULT_PI_LAUNCH).strip() or DEFAULT_PI_LAUNCH,
        door_instance=door_instance(),
    )


def door_instance() -> str:
    """This terminal's name for the writer lease (contract 02 §7.1).

    One TUI process is one human writer, so each names itself. Two terminals
    on one session must NOT share a lease: two pi writers on one session store
    cross-contaminate context, orphan a branch and both report success with no
    error anywhere (contract 02 §7, measured). §7.1's "two processes of one
    door share a lease" is the safe direction for a server door, which serves
    many chats from one pool. It is the unsafe direction here.

    The pid is an identifier, never a secret.
    """
    return f"tui.{os.getpid()}"


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


def _read_token(env: dict[str, str]) -> str:
    path = env.get(ENV_TOKEN_FILE, DEFAULT_TOKEN_FILE).strip() or DEFAULT_TOKEN_FILE

    try:
        value = Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        # The path is not a secret. The content is, and it is not in the text.
        raise ConfigError(f"{ENV_TOKEN_FILE}: cannot read {path} ({exc.strerror}).") from exc
    except UnicodeDecodeError:
        # The decode error holds bytes of the token, so it is not the cause.
        raise ConfigError(f"{ENV_TOKEN_FILE}: the token in {path} is not UTF-8 text.") from None
    except ValueError as exc:
        # A path that the system refuses before the read: a NUL byte.
        raise ConfigError(f"{ENV_TOKEN_FILE}: cannot read {path!r} ({exc}).") from exc

    if len(value.encode("utf-8")) < MIN_TOKEN_BYTES:
        raise ConfigError(
            f"{ENV_TOKEN_FILE}: the token in {path} is shorter than {MIN_TOKEN_BYTES} bytes. "
            "An empty or short token is how an admin surface becomes an open one."
        )

    return value
