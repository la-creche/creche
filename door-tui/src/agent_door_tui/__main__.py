"""Entry point: `agent-tui <family>`, or `python -m agent_door_tui`.

    agent-tui chat                       pick a session from a numbered list
    agent-tui chat --session tui-01JB…   attach to that one
    agent-tui chat --new --title "Boiler"
    agent-tui chat --session owui-3f2a… --force     take an idle lease
    agent-tui --check                    validate the config and exit

Config comes from the environment (`config.py` lists every `DOOR_TUI_*`
variable). `--check` touches neither `sessiond` nor `sbx`, so a bad token
file is caught before the operator is in front of a terminal that will not open.
"""

from __future__ import annotations

import argparse
import logging
import sys

from .app import Request, TuiDoor, Want
from .config import ConfigError, TuiConfig, from_env
from .errors import DoorError, Exit
from .launch import Terminal as SbxTerminal
from .picker import RealTerminal
from .sessiond import HttpSessiond, Takeover
from .status import StatusFiles

PROGRAM = "agent-tui"


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    # httpx logs every request URL at INFO, and the sessiond token rides on
    # every one of these calls (invariant 13).
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    args = parse_args(argv)

    try:
        config = from_env()
    except ConfigError as exc:
        return _refuse(f"refusing to start: {exc}", Exit.BAD_USAGE)

    if args.check:
        print(f"{PROGRAM}: config OK: {config.describe()}")
        return int(Exit.OK)

    if args.family is None:
        return _refuse("name a family, or pass --check. Try --help.", Exit.BAD_USAGE)

    return _run(config, request_of(args))


def _run(config: TuiConfig, request: Request) -> int:
    door = HttpSessiond(config)
    tui = TuiDoor(
        door,
        StatusFiles(config.families_dir),
        RealTerminal(),
        SbxTerminal(),
        config.door_instance,
        config.sbx,
        config.pi_launch,
    )

    try:
        return tui.run(request)
    except DoorError as exc:
        return _refuse(exc.message, exc.code)
    except KeyboardInterrupt:
        # Ctrl-C before pi owns the terminal. `tui.run` already released the
        # lease in its own `finally`.
        return int(Exit.NO_CHOICE)
    finally:
        door.close()


def request_of(args: argparse.Namespace) -> Request:
    """One parsed command line as the request `TuiDoor.run` takes."""
    family = str(args.family)
    takeover = Takeover.FORCE if args.force else Takeover.POLITE

    if args.session:
        return Request(family, Want.NAMED, session=str(args.session), takeover=takeover)

    if args.new:
        return Request(family, Want.NEW, title=str(args.title or ""), takeover=takeover)

    return Request(family, Want.PICK, takeover=takeover)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """The command line. `--session` and `--new` are mutually exclusive."""
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description="Put a terminal on a session, inside that family's sandbox.",
    )
    parser.add_argument("family", nargs="?", help="the family to work in, for example `chat`")
    parser.add_argument("--session", help="attach to this session id, skipping the picker")
    parser.add_argument("--new", action="store_true", help="start a new session")
    parser.add_argument("--title", help="the new session's title")
    parser.add_argument(
        "--force",
        action="store_true",
        help="take the writer lease from an idle holder. Never from a running turn.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate the config and exit, without calling sessiond or sbx",
    )
    args = parser.parse_args(argv)

    if args.session and args.new:
        parser.error("pass --session or --new, not both")

    return args


def _refuse(message: str, code: Exit) -> int:
    print(f"{PROGRAM}: {message}", file=sys.stderr)

    return int(code)


if __name__ == "__main__":
    sys.exit(main())
