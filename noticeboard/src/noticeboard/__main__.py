"""Start the noticeboard, or check its configuration and exit.

`--check` reads the environment, proves the bind is legal and the key is
long enough, names every directory it would read, and exits. It opens no
socket, so a deploy can run it before it swaps a unit.

It never prints the access key, the `view-ro` token or any part of
either. It prints whether the file exists and whether it is empty, which
is what a deploy needs to know (invariant 13).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import uvicorn

from .app import build_app
from .config import Config, ConfigError, from_env
from .sessiondhttp import HttpTransport, build_client
from .sessions import SessionReader

EXIT_OK = 0
EXIT_BAD_CONFIG = 2

_LOG = logging.getLogger("noticeboard")


def main(argv: list[str] | None = None) -> int:
    """The entry point. Returns the process exit code."""
    parsed = _parse_args(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    try:
        config = from_env()
    except ConfigError as error:
        # The refusals worth naming here are "bind is a wildcard" and
        # "the key is missing on a LAN bind". Either one would open a LAN
        # admin surface.
        print(f"noticeboard: {error}", file=sys.stderr)
        return EXIT_BAD_CONFIG

    if parsed.check:
        _report(config)
        return EXIT_OK

    _serve(config)

    return EXIT_OK


def _serve(config: Config) -> None:
    reader = SessionReader(
        transport=HttpTransport(build_client(config.sessiond_socket, config.sessiond_url)),
        token_file=config.view_token_file,
    )
    _LOG.info("noticeboard on %s:%s", config.bind, config.port)
    uvicorn.run(build_app(config, reader), host=config.bind, port=config.port, access_log=False)


def _report(config: Config) -> None:
    """What `--check` prints. No secret, and no part of one."""
    print(f"bind          {config.bind}:{config.port}")
    print(f"loopback      {'yes' if config.on_loopback else 'no'}")
    print(f"access key    {_key_state(config)}")
    print(f"cookie secure {'yes' if config.cookie_secure else 'no'}")
    print(f"state root    {_where(config.state_root)}")
    print(f"families      {_where(config.families_dir)}")
    print(f"audit         {_where(config.audit_dir)}")
    print(f"outcomes      {_where(config.outcomes_dir)}")
    print(f"registry      {_where(config.registry_dir)}")
    print(f"view-ro token {_where(config.view_token_file)}")
    print(f"sessiond      {_sessiond(config)}")


def _key_state(config: Config) -> str:
    if config.access_key:
        return "set"

    return "empty (allowed: this bind is loopback)" if config.on_loopback else "empty"


def _where(path: Path) -> str:
    return f"{path} ({'present' if path.exists() else 'MISSING'})"


def _sessiond(config: Config) -> str:
    if config.sessiond_socket is None:
        return config.sessiond_url

    return _where(config.sessiond_socket)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="noticeboard", description="The noticeboard (invariant 20)"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate the environment, print what would be served, and exit",
    )

    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
