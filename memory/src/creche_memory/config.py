"""What the memory service reads from its environment.

The unit reads two files: `/etc/creche/site.env` for the site values, and
the service's own mode-0600 env file for the LiteLLM key. A value that is
missing or bad stops the start. No setting has a silent fallback that could
point the service at another vault or another store."""

import os
import pwd
from dataclasses import dataclass, field
from pathlib import Path

from .store import StoreSettings

LAN_ADDRESS_ENV = "AGENT_LAN_ADDRESS"
LITELLM_URL_ENV = "AGENT_LITELLM_BASE_URL"
TEI_URL_ENV = "TEI_URL"
KEY_ENV = "MEMORY_LITELLM_KEY"
VAULT_ENV = "MEMORY_VAULT_ROOT"
STORE_ENV = "MEMORY_STORE_ROOT"
SOCKET_ENV = "MEMORY_SOCKET"
PEER_ENV = "MEMORY_PEER_USER"

#: The ports of the catalog defaults of `env/src/creche_env/default.env`, for
#: a site file that sets only the LAN address.
LITELLM_PORT = 4000
TEI_PORT = 8085

DEFAULT_VAULT = "/srv/agents/vault"
DEFAULT_STORE = "/srv/agents/state/memory"
DEFAULT_SOCKET = "/var/lib/creche-memory/memory.sock"
DEFAULT_PEER = "mcp-memory"

#: The Hugging Face hub cache of the tokenizer, inside the store.
TOKENIZER_CACHE = Path("hf") / "hub"


class ConfigError(Exception):
    """The environment does not describe a service that may start."""


@dataclass(frozen=True)
class Places:
    """The folders, the socket and the peer. The verify hook reads only these."""

    vault_root: Path
    store_root: Path
    socket_path: Path
    peer_uid: int

    @property
    def tokenizer_cache(self) -> Path:
        return self.store_root / TOKENIZER_CACHE


@dataclass(frozen=True)
class Config:
    places: Places
    store: StoreSettings = field(repr=False)


def _base_url(source: dict[str, str], name: str, port: int) -> str:
    explicit = source.get(name, "").strip()
    if explicit:
        return explicit

    address = source.get(LAN_ADDRESS_ENV, "").strip()
    if not address:
        raise ConfigError(f"set {name} or {LAN_ADDRESS_ENV} (/etc/creche/site.env)")

    return f"http://{address}:{port}"


def _path(source: dict[str, str], name: str, default: str) -> Path:
    path = Path(source.get(name, "").strip() or default)
    if not path.is_absolute():
        raise ConfigError(f"{name} is not an absolute path: {path}")

    return path


def _peer_uid(name: str) -> int:
    try:
        return pwd.getpwnam(name).pw_uid
    except KeyError:
        raise ConfigError(f"no user {name}: create it first (the root steps)") from None


def key_from_env(source: dict[str, str]) -> str:
    key = source.get(KEY_ENV, "").strip()
    if not key:
        raise ConfigError(f"{KEY_ENV} is not set (the env file of the unit)")

    return key


def places_from_env(source: dict[str, str]) -> Places:
    return Places(
        vault_root=_path(source, VAULT_ENV, DEFAULT_VAULT),
        store_root=_path(source, STORE_ENV, DEFAULT_STORE),
        socket_path=_path(source, SOCKET_ENV, DEFAULT_SOCKET),
        peer_uid=_peer_uid(source.get(PEER_ENV, "").strip() or DEFAULT_PEER),
    )


def from_env(environ: dict[str, str] | None = None) -> Config:
    source = environ if environ is not None else dict(os.environ)
    key = key_from_env(source)
    places = places_from_env(source)
    try:
        store = StoreSettings(
            store_root=places.store_root,
            llm_base_url=_base_url(source, LITELLM_URL_ENV, LITELLM_PORT),
            llm_key=key,
            embedding_base_url=_base_url(source, TEI_URL_ENV, TEI_PORT),
            tokenizer_cache=places.tokenizer_cache,
        )
    except ValueError as error:
        raise ConfigError(str(error)) from None

    return Config(places=places, store=store)


def check(places: Places) -> list[str]:
    """What stops a start on this host. An empty list means none."""
    problems: list[str] = []
    if not places.vault_root.is_dir():
        problems.append(f"no vault folder at {places.vault_root}")

    if not places.socket_path.parent.is_dir():
        problems.append(f"no socket folder at {places.socket_path.parent} (the root steps)")

    if not places.tokenizer_cache.is_dir():
        problems.append(f"no tokenizer cache at {places.tokenizer_cache}")

    return problems
