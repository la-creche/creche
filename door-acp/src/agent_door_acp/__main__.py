"""Entry point: `agent-door-acp`, and `python -m agent_door_acp`.

Config comes from the environment (`config.py` lists every variable).
`--check` validates it and the key file, then exits. It binds no port, so a
bad key file or a wide bind stops the unit before the door starts.
"""

from __future__ import annotations

import argparse
import logging
import sys

import uvicorn

from .app import create_app
from .config import ConfigError, from_env

#: The `websockets` sans-I/O protocol of uvicorn. It supports the denial
#: response, so a refusal is an HTTP status before the upgrade.
_WS_PROTOCOL = "websockets-sansio"


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    args = _parse_args(argv)

    try:
        config = from_env()
    except ConfigError as exc:
        print(f"agent-door-acp: refusing to start: {exc}", file=sys.stderr)
        return 1

    if args.check:
        print(f"agent-door-acp: config OK: {config.describe()}")
        return 0

    # from_env() already refuses a wide bind, so nothing here can widen it.
    uvicorn.run(
        create_app(config),
        host=config.bind_host,
        port=config.bind_port,
        ws=_WS_PROTOCOL,
        server_header=False,
    )
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="agent-door-acp")
    parser.add_argument(
        "--check", action="store_true", help="validate the config and exit, without a bind"
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
