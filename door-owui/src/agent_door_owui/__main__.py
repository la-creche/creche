"""Entry point: `python -m agent_door_owui`.

Config comes from the environment (`config.py` lists every `DOOR_OWUI_*`
variable). `--check` validates it and exits without binding a port or
touching `sessiond`, so a bad key file or a `0.0.0.0` bind is caught before
the unit is enabled, not after (config.py §"fail closed").
"""

from __future__ import annotations

import argparse
import logging
import sys

import uvicorn

from .app import create_app
from .config import ConfigError, DoorConfig, from_env
from .families import StatusFiles
from .sessiond import HttpSessiond


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    # httpx logs every request URL at INFO, and a header carrying the
    # sessiond token rides on every one of these calls (invariant 13).
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    args = _parse_args(argv)

    try:
        config = from_env()
    except ConfigError as exc:
        print(f"agent-door-owui: refusing to start: {exc}", file=sys.stderr)
        return 1

    if args.check:
        print(f"agent-door-owui: config OK: {config.describe()}")
        return 0

    _serve(config)
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="agent-door-owui")
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate config and exit, without binding a port or calling sessiond",
    )
    return parser.parse_args(argv)


def _serve(config: DoorConfig) -> None:
    families = StatusFiles(config.families_dir)
    app = create_app(config, HttpSessiond(config), families)
    # config.from_env() already refuses a 0.0.0.0 bind, so nothing here can
    # widen it back out.
    uvicorn.run(app, host=config.bind_host, port=config.bind_port)


if __name__ == "__main__":
    sys.exit(main())
