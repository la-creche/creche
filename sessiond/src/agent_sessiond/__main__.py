"""Start the session service, or check its configuration and exit.

The host has no `node` on its PATH, so nothing on this side of the channel
may need it. This is a Python service and it stays one (contract 03 §14.1
rule 6).

`--check` validates the environment and every token file, prints what the
service would bind, and exits. It opens no socket and touches no sandbox, so
a deploy can run it before it swaps a unit.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import socket
import stat
import sys
from pathlib import Path

import uvicorn

from .api import build_app
from .auth import Principal, TokenBook, TokenError
from .config import Config, ConfigError, from_env
from .service import SessionService

EXIT_OK = 0
EXIT_BAD_CONFIG = 2

# Contract 02 §3 rule 1. The unit's umask
# would otherwise leave a fresh socket at 0750, which gives the group no
# write, and `connect(2)` needs write.
_SOCKET_MODE = 0o660

# Same rule: `sock/`'s directory is setgid so a socket bound inside it
# keeps group `agents` rather than this process's own default group. Owner
# rwx, group r-x (traverse only, no write needed to connect), no world bit.
_SOCKET_DIR_MODE = 0o2750

_LOG = logging.getLogger("sessiond")


def main(argv: list[str] | None = None) -> int:
    """The entry point. Returns the process exit code."""
    parsed = _parse_args(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    try:
        config = from_env()
        tokens = TokenBook(config.state_root)
        tokens.load()
    except (ConfigError, TokenError) as error:
        print(f"sessiond: {error}", file=sys.stderr)
        return EXIT_BAD_CONFIG

    if parsed.check:
        _report(config)
        return EXIT_OK

    asyncio.run(serve(config, tokens))
    return EXIT_OK


async def serve(config: Config, tokens: TokenBook) -> None:
    """Run the service until a signal asks it to stop."""
    service = SessionService(config)
    service.start()
    service.start_upkeep()
    app = build_app(service, tokens)
    servers = [
        _Server(setting, socket_path=config.socket_path if setting.uds else None)
        for setting in _uvicorn_configs(config, app)
    ]

    _install_signals(tokens, servers)

    try:
        await asyncio.gather(*(server.serve() for server in servers))
    finally:
        await service.close()


def _uvicorn_configs(config: Config, app: object) -> list[uvicorn.Config]:
    """A Unix socket always, plus the LAN address when the config says so.

    Never loopback and never 0.0.0.0 (contract 02 §3 rule 2). Both services
    on this host bind the LAN address, and assuming otherwise has already
    cost one same-day patch release.
    """
    _prepare_socket_dir(config.socket_path.parent)
    _clear_stale_socket(config.socket_path)

    settings = [
        uvicorn.Config(
            app,  # pyright: ignore[reportArgumentType]
            uds=str(config.socket_path),
            log_level="info",
            access_log=False,
        )
    ]

    if config.binds_lan:
        settings.append(
            uvicorn.Config(
                app,  # pyright: ignore[reportArgumentType]
                host=config.lan_address,
                port=config.lan_port,
                log_level="info",
                access_log=False,
            )
        )

    return settings


def _clear_stale_socket(path: Path) -> None:
    """A socket file left by a killed process would refuse the new bind."""
    if path.is_socket():
        path.unlink(missing_ok=True)


def _prepare_socket_dir(path: Path) -> None:
    """Make the socket's directory and leave its setgid bit alone.

    Setgid is what hands a socket bound here the directory's group (`agents`)
    instead of this process's own default group (contract 02 §3 rule 1).
    The mode is written only when it is wrong, because a chmod by a process
    outside that group silently strips setgid, and a bit lost that way is
    reported, not silently repeated.
    """
    path.mkdir(parents=True, exist_ok=True)

    if stat.S_IMODE(path.stat().st_mode) == _SOCKET_DIR_MODE:
        return

    try:
        path.chmod(_SOCKET_DIR_MODE)
    except OSError as error:
        _LOG.warning("could not set mode on %s: %s", path, error)
        return

    if not stat.S_IMODE(path.stat().st_mode) & stat.S_ISGID:
        _LOG.warning(
            "%s lost its setgid bit (this process is not in the directory's "
            "group); run: chmod g+s %s -- until then the PEP cannot connect "
            "for cold starts",
            path,
            path,
        )


def _publish_socket_mode(path: Path) -> None:
    """After bind: the unit's umask leaves a fresh socket at 0750, which
    gives the group no write, and `connect(2)` needs write (contract 02 §3
    rule 1)."""
    path.chmod(_SOCKET_MODE)


class _Server(uvicorn.Server):
    """A listener that leaves the signals to this module, and fixes a bound
    Unix socket's mode before any request can reach it.

    Two listeners can serve one app. If each installed its own handlers, the
    second would replace the first and only one would ever stop.
    """

    def __init__(self, config: uvicorn.Config, socket_path: Path | None = None) -> None:
        super().__init__(config)
        # None for the LAN listener, which has no socket file to fix.
        self._socket_path = socket_path

    def install_signal_handlers(self) -> None:
        return

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        """uvicorn creates the socket file inside the parent call, so the
        mode fix has to run right after it, before `main_loop` accepts
        anything."""
        await super().startup(sockets=sockets)

        if self._socket_path is not None:
            _publish_socket_mode(self._socket_path)


def _install_signals(tokens: TokenBook, servers: list[_Server]) -> None:
    """SIGHUP reloads the token files. It drops no session and no turn."""
    loop = asyncio.get_running_loop()

    def reload_tokens() -> None:
        try:
            tokens.reload()
            _LOG.info("token files reloaded")
        except TokenError as error:
            # The live set stays. A bad file must not disarm the service.
            _LOG.error("token reload refused: %s", error)

    def stop() -> None:
        for server in servers:
            server.should_exit = True

    with contextlib.suppress(NotImplementedError):
        loop.add_signal_handler(signal.SIGHUP, reload_tokens)
        loop.add_signal_handler(signal.SIGTERM, stop)
        loop.add_signal_handler(signal.SIGINT, stop)


def _report(config: Config) -> None:
    """What `--check` prints. It names no token and no secret."""
    print(f"sessiond: sessions root {config.sessions_root}")
    print(f"sessiond: state root    {config.state_root}")
    print(f"sessiond: socket        {config.socket_path}")

    if config.binds_lan:
        print(f"sessiond: tcp           {config.lan_address}:{config.lan_port}")
    else:
        print("sessiond: tcp           off")

    print(f"sessiond: channel       {config.channel_command}")
    print(f"sessiond: tokens        {len(Principal)} files present and long enough")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="sessiond", description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate the configuration and the token files, then exit",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
